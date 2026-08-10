"""Claude without an API key: drives claude.ai inside an already-authenticated
Chrome tab over the DevTools Protocol.

The mechanism (create a scratch conversation, POST /completion, read the SSE
stream back, delete the conversation) is lifted from the sibling `ai` repo's
council_bot.py "browser" backend, which reverse-engineered the payload shapes
from cyber-wojtek/Claude-API. Undocumented internal endpoints: they may drift,
and long-running automated use sits uneasily with claude.ai's terms -- same
caveat that backend carries.

Consequences the rest of the app should know about:
  * No structured outputs: complete_json() prompts for strict JSON, extracts
    the first {...} block, validates it against the caller's schema itself,
    and retries once with a sterner instruction before giving up.
  * No usage metering: the SSE stream carries no token counts, so only call
    counts are recorded (stats.py labels this provider's spend as covered by
    the claude.ai subscription rather than pricing it).
  * Web search is claude.ai's own web_search tool, enabled per request;
    search_json() cannot cap the number of searches, so max_searches becomes
    a prompt instruction rather than a hard limit.

Needs: Chrome launched with --remote-debugging-port (CLAUDE_BROWSER_PORT,
default 9222), a claude.ai tab logged in inside that Chrome, and CLAUDE_ORG_ID
in the environment / .env.
"""
import json
import os
import uuid

from . import cdp
from .base import LLMProvider, ProviderError, parse_json_array, parse_json_object

DEFAULT_PORT = 9222

# claude.ai's server-side web search tool, as its own web client names it.
WEB_SEARCH_TOOLS = [{"name": "web_search", "type": "web_search_v0"}]

SETUP_HINT = (
    "launch Chrome with --remote-debugging-port={port}, log into claude.ai in "
    "that window, and set CLAUDE_ORG_ID in .env"
)

STRICT_JSON_SUFFIX = (
    "\n\nRespond with ONLY a JSON object that validates against this JSON "
    "schema -- no prose, no markdown fences, nothing before or after the "
    "object:\n{schema}"
)

RETRY_SUFFIX = (
    "\n\nIMPORTANT: your answer must be the bare JSON object itself, with "
    "every required key present, and nothing else."
)

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


class ClaudeChatProvider(LLMProvider):
    name = "claude_chat"

    def __init__(self, model, org_id=None, port=None, connect=None):
        super().__init__(model)
        self.org_id = org_id if org_id is not None else os.environ.get("CLAUDE_ORG_ID", "")
        self.port = int(port if port is not None else os.environ.get("CLAUDE_BROWSER_PORT", DEFAULT_PORT))
        # `connect` returns something with .evaluate(js, timeout=) / .close();
        # injectable so tests never open a socket.
        self._connect = connect or self._connect_chrome
        self._connection = None

    # --- preflight ------------------------------------------------------------

    def preflight(self):
        """A free, local check: no CDP call, no completion. Used by health.py
        to gate a whole run-once before it spends anything."""
        if not self.org_id:
            return False, "CLAUDE_ORG_ID is not set"
        try:
            tab = cdp.find_claude_tab(self.port)
        except OSError as e:
            return False, f"no Chrome DevTools endpoint on port {self.port} ({e})"
        if not tab:
            return False, f"no open claude.ai tab in the Chrome on port {self.port}"
        return True, ""

    # --- the two provider methods -------------------------------------------

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        # max_tokens is part of the provider contract but claude.ai's endpoint
        # has no output cap parameter; it is accepted and ignored.
        base = f"{system}\n\n{prompt}" + STRICT_JSON_SUFFIX.format(schema=json.dumps(schema))
        last_error = None
        for attempt in range(2):
            text = self._completion(base if attempt == 0 else base + RETRY_SUFFIX, tools=[])
            try:
                data = parse_json_object(_extract_object(text))
                _validate(data, schema)
                return data
            except ProviderError as e:
                last_error = ProviderError(f"claude.ai reply attempt {attempt + 1}: {e}")
        raise last_error

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        # No hard search cap on this endpoint -- state it in the prompt instead.
        full = f"{prompt}\n\nUse web search (at most {max_searches} searches)."
        text = self._completion(full, tools=WEB_SEARCH_TOOLS, timeout=420)
        return parse_json_array(text)

    # --- one claude.ai round trip -------------------------------------------

    def _completion(self, prompt, tools, timeout=240):
        """One scratch-conversation round trip; text of the reply.

        A dropped Chrome connection gets one reconnect-and-retry (the tab
        itself usually survives; it is the websocket that dies). Everything
        else -- JS exceptions, HTTP errors from claude.ai, an empty reply --
        is a ProviderError the pipeline treats like any other scoring failure.
        """
        try:
            return self._attempt(prompt, tools, timeout)
        except (ConnectionError, OSError) as e:
            self._reset()
            try:
                return self._attempt(prompt, tools, timeout)
            except (ConnectionError, OSError) as e2:
                self._reset()
                raise ProviderError(f"claude.ai connection failed twice: {e2}") from e2

    def _attempt(self, prompt, tools, timeout):
        conn = self._conn()
        conv_id = str(uuid.uuid4())
        try:
            conn.evaluate(_js_create_conversation(self.org_id, conv_id), timeout=30)
            raw = conn.evaluate(
                _js_send_completion(self.org_id, conv_id, prompt, self.model, tools),
                timeout=timeout,
            )
        except RuntimeError as e:
            # JS exception inside the tab (claude.ai HTTP error, auth expiry).
            # The CDP connection itself is fine, so no reset.
            raise ProviderError(f"claude.ai call failed: {e}") from e

        try:
            conn.evaluate(_js_delete_conversation(self.org_id, conv_id), timeout=15)
        except Exception:  # noqa: BLE001 -- best-effort cleanup only
            pass

        self.record_usage()  # calls only; this transport reports no token counts
        if raw is None:
            raise ProviderError(
                "claude.ai returned no text -- the Chrome tab was likely "
                "navigated/reloaded mid-request; leave the claude.ai tab alone"
            )
        result = raw if isinstance(raw, dict) else json.loads(raw)
        if not isinstance(result, dict):
            raise ProviderError(f"unexpected completion payload from claude.ai: {result!r}")
        text = result.get("text") or ""
        if not text.strip():
            raise ProviderError("empty completion from claude.ai")
        return text

    # --- session --------------------------------------------------------------

    def _conn(self):
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _connect_chrome(self):
        hint = SETUP_HINT.format(port=self.port)
        if not self.org_id:
            raise ProviderError(f"CLAUDE_ORG_ID is not set -- {hint}")
        try:
            tab = cdp.find_claude_tab(self.port)
        except (OSError, ConnectionError) as e:
            raise ProviderError(
                f"no Chrome DevTools endpoint on port {self.port} ({e}) -- {hint}"
            ) from e
        if not tab:
            raise ProviderError(
                f"no open claude.ai tab in the Chrome on port {self.port} -- {hint}"
            )
        try:
            return cdp.CDPConnection(tab["webSocketDebuggerUrl"])
        except (OSError, ConnectionError) as e:
            raise ProviderError(f"could not attach to the claude.ai tab: {e}") from e

    def _reset(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001
                pass
        self._connection = None


# --- schema validation (claude.ai has no structured outputs) -------------------

def _validate(data, schema):
    """Just enough JSON-schema checking to catch a malformed reply before it
    poisons the pipeline: required keys, primitive types, enums. Anything the
    check can't express is left to the callers' own clamping/parsing."""
    for key in schema.get("required", []):
        if key not in data:
            raise ProviderError(f"missing required key '{key}'")
    for key, spec in schema.get("properties", {}).items():
        if key not in data:
            continue
        value = data[key]
        check = _TYPE_CHECKS.get(spec.get("type"))
        if check and not check(value):
            raise ProviderError(f"key '{key}' has wrong type {type(value).__name__}")
        if "enum" in spec and value not in spec["enum"]:
            raise ProviderError(f"key '{key}' value {value!r} not in {spec['enum']}")


def _extract_object(text):
    """Slice the first {...last} span -- claude.ai replies love to narrate or
    fence the payload even when told not to."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return text
    return text[start : end + 1]


# --- claude.ai internal endpoints, run as JS inside the authenticated tab ------
# Payload fields and SSE framing mirror council_bot.py's browser backend.

def _js_create_conversation(org_id, conv_id):
    """400/409 (already exists) is fine."""
    return f"""
(async () => {{
  const res = await fetch('https://claude.ai/api/organizations/{org_id}/chat_conversations', {{
    method: 'POST',
    credentials: 'include',
    headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }},
    body: JSON.stringify({{
      uuid: '{conv_id}',
      name: 'discovery scratch',
      include_conversation_preferences: true,
      is_temporary: false
    }})
  }});
  if (!res.ok && res.status !== 400 && res.status !== 409) {{
    throw new Error('create conversation HTTP ' + res.status + ': ' + await res.text());
  }}
  return true;
}})()
"""


def _js_send_completion(org_id, conv_id, prompt, model, tools):
    prompt_json = json.dumps(prompt)
    model_json = json.dumps(model)
    tools_json = json.dumps(tools)
    return f"""
(async () => {{
  const payload = {{
    attachments: [],
    files: [],
    locale: 'en-US',
    model: {model_json},
    parent_message_uuid: '00000000-0000-4000-8000-000000000000',
    prompt: {prompt_json},
    rendering_mode: 'messages',
    sync_sources: [],
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    tools: {tools_json},
    turn_message_uuids: {{
      human_message_uuid: crypto.randomUUID(),
      assistant_message_uuid: crypto.randomUUID()
    }}
  }};
  const res = await fetch(`https://claude.ai/api/organizations/{org_id}/chat_conversations/{conv_id}/completion`, {{
    method: 'POST',
    credentials: 'include',
    headers: {{ 'Content-Type': 'application/json', Accept: 'text/event-stream' }},
    body: JSON.stringify(payload)
  }});
  if (!res.ok) throw new Error('completion HTTP ' + res.status + ': ' + await res.text());

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let text = '';
  while (true) {{
    const {{ done, value }} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {{ stream: true }});
    const lines = buf.split('\\n');
    buf = lines.pop();
    for (const line of lines) {{
      if (!line.startsWith('data:')) continue;
      const payloadLine = line.slice(5).trim();
      if (!payloadLine || payloadLine === '[DONE]') continue;
      let evt;
      try {{ evt = JSON.parse(payloadLine); }} catch (e) {{ continue; }}
      if (evt.type === 'content_block_delta' && evt.delta && evt.delta.type === 'text_delta') {{
        text += evt.delta.text;
      }} else if (typeof evt.completion === 'string') {{
        text = evt.completion;
      }}
    }}
  }}
  return JSON.stringify({{ text }});
}})()
"""


def _js_delete_conversation(org_id, conv_id):
    return f"""
(async () => {{
  await fetch('https://claude.ai/api/organizations/{org_id}/chat_conversations/{conv_id}', {{
    method: 'DELETE',
    credentials: 'include'
  }});
  return true;
}})()
"""
