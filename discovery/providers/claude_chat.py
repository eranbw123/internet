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
from datetime import datetime, timezone

from . import cdp
from .base import LLMProvider, ProviderError, parse_json_array, parse_json_object


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_PORT = 9222

# Bound on preflight()'s reachability check only -- a port that accepts
# connections but never answers (frozen Chrome, an unrelated service
# squatting on 9222) must not hang a "free" check forever. Real completions
# use their own, longer timeouts (see _completion/_attempt).
PREFLIGHT_TIMEOUT_SECONDS = 5

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
        # Numbers model_calls rows within one top-level complete_json()/
        # search_json() call -- reset at the start of each, so a JSON-retry
        # attempt and a connection-level reconnect-and-retry inside
        # _completion() both get their own row, numbered in the order they
        # actually happened.
        self._attempt_seq = 0

    # --- preflight ------------------------------------------------------------

    def preflight(self):
        """A free, local check: no CDP call, no completion. Used by health.py
        to gate a whole run-once before it spends anything. Bounded by
        PREFLIGHT_TIMEOUT_SECONDS (an unresponsive port must not hang this),
        and (OSError, ValueError) covers both a dead/refusing socket and a
        non-CDP listener answering with something that isn't JSON
        (json.JSONDecodeError is a ValueError) -- either way, "not
        reachable", not a crash."""
        if not self.org_id:
            return False, "CLAUDE_ORG_ID is not set"
        try:
            tab = cdp.find_claude_tab(self.port, timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except (OSError, ValueError) as e:
            return False, f"no Chrome DevTools endpoint on port {self.port} ({e})"
        if not tab:
            return False, f"no open claude.ai tab in the Chrome on port {self.port}"
        return True, ""

    # --- the two provider methods -------------------------------------------

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        # max_tokens is part of the provider contract but claude.ai's endpoint
        # has no output cap parameter; it is accepted and ignored.
        base = f"{system}\n\n{prompt}" + STRICT_JSON_SUFFIX.format(schema=json.dumps(schema))
        self._attempt_seq = 0
        self.last_events = None
        last_error = None
        for attempt in range(2):
            text = self._completion(
                base if attempt == 0 else base + RETRY_SUFFIX, tools=[],
                trace_parse=lambda t: _trace_parse_object(t, schema), schema=schema,
            )
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
        self._attempt_seq = 0
        self.last_events = None
        text = self._completion(
            full, tools=WEB_SEARCH_TOOLS, timeout=420,
            trace_parse=lambda t: (parse_json_array(t), "array"),
        )
        return parse_json_array(text)

    # --- one claude.ai round trip -------------------------------------------

    def _completion(self, prompt, tools, timeout=240, trace_parse=None, schema=None):
        """One scratch-conversation round trip; text of the reply.

        A dropped Chrome connection gets one reconnect-and-retry (the tab
        itself usually survives; it is the websocket that dies). Everything
        else -- JS exceptions, HTTP errors from claude.ai, an empty reply --
        is a ProviderError the pipeline treats like any other scoring failure.
        """
        try:
            return self._attempt(prompt, tools, timeout, trace_parse, schema)
        except (ConnectionError, OSError) as e:
            self._reset()
            try:
                return self._attempt(prompt, tools, timeout, trace_parse, schema)
            except (ConnectionError, OSError) as e2:
                self._reset()
                raise ProviderError(f"claude.ai connection failed twice: {e2}") from e2

    def _attempt(self, prompt, tools, timeout, trace_parse=None, schema=None):
        started = _now_iso()
        self._attempt_seq += 1
        attempt_no = self._attempt_seq

        def emit(raw_text=None, error=None, parsed=None, validation=None, events=None):
            self._emit_call(
                None, attempt_no, None, prompt, schema, None,
                raw_text, parsed, validation, error, started, _now_iso(), events=events,
            )

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
            error = f"claude.ai call failed: {e}"
            emit(error=error)
            raise ProviderError(error) from e

        try:
            conn.evaluate(_js_delete_conversation(self.org_id, conv_id), timeout=15)
        except Exception:  # noqa: BLE001 -- best-effort cleanup only
            pass

        self.record_usage()  # calls only; this transport reports no token counts
        if raw is None:
            error = (
                "claude.ai returned no text -- the Chrome tab was likely "
                "navigated/reloaded mid-request; leave the claude.ai tab alone"
            )
            emit(error=error)
            raise ProviderError(error)
        result = raw if isinstance(raw, dict) else json.loads(raw)
        if not isinstance(result, dict):
            error = f"unexpected completion payload from claude.ai: {result!r}"
            emit(error=error)
            raise ProviderError(error)
        text = result.get("text") or ""
        events = result.get("events") or None
        self.last_events = events or []
        if not text.strip():
            error = "empty completion from claude.ai"
            emit(error=error, events=events)
            raise ProviderError(error)

        parsed, validation = None, None
        if trace_parse is not None:
            try:
                parsed, validation = trace_parse(text)
            except Exception:  # noqa: BLE001 -- tracing must never break a real call
                validation = "trace_parse_error"
        emit(raw_text=text, parsed=parsed, validation=validation, events=events)
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

def _trace_parse_object(text, schema):
    """Best-effort parse+validate purely for the trace row -- mirrors what
    complete_json() does right after, but never affects control flow (it is
    called from inside _attempt(), swallowed on any exception by the caller).
    Kept as its own function (not a lambda) so chatgpt_browser.py can reuse
    the same shape via its own schema."""
    try:
        data = parse_json_object(_extract_object(text))
        _validate(data, schema)
        return data, "valid"
    except ProviderError as e:
        return None, f"invalid: {e}"


def _validate(data, schema):
    """Just enough JSON-schema checking to catch a malformed reply before it
    poisons the pipeline: required keys, primitive types, enums. Anything the
    check can't express is left to the callers' own clamping/parsing.

    Type checks apply only to REQUIRED properties. SCORE_SCHEMA/MISSION_SCHEMA
    carry optional debug/deliberation fields that callers (scoring.
    _debug_payload, council._extract_deliberation) already parse tolerantly --
    absent or wrong-shaped becomes an 'unavailable' marker, never fatal.
    Type-checking an optional field here would turn that tolerance into a
    hard ProviderError before the tolerant code ever runs, on claude_chat/
    chatgpt_browser -- the two providers whose only schema enforcement this
    function is. Enum checks stay unconditional (present -> checked) --
    none of the optional debug/deliberation fields declare an enum, only
    fixed-vocabulary production fields do, and those should still reject a
    value outside the vocabulary even when merely present-not-required."""
    required = schema.get("required", [])
    for key in required:
        if key not in data:
            raise ProviderError(f"missing required key '{key}'")
    for key, spec in schema.get("properties", {}).items():
        if key not in data:
            continue
        value = data[key]
        if key in required:
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
  // Best-effort retention of observable search/tool activity in the SSE
  // stream (server_tool_use start, its result block, errors) -- never
  // fabricated: absent from the stream means an empty list, which the
  // Python side turns into one explicit "not exposed by provider" node
  // rather than inventing anything.
  const events = [];
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
      }} else if (evt.type === 'content_block_start' && evt.content_block &&
                  (evt.content_block.type === 'server_tool_use' ||
                   evt.content_block.type === 'web_search_tool_result')) {{
        events.push({{
          type: evt.content_block.type,
          name: evt.content_block.name || null,
          input: evt.content_block.input || null,
        }});
      }} else if (evt.type === 'error') {{
        events.push({{ type: 'error', error: evt.error || evt }});
      }}
    }}
  }}
  return JSON.stringify({{ text, events }});
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
