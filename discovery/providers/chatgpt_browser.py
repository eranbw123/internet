"""ChatGPT without an API key: drives chatgpt.com inside an already-authenticated
Chrome tab over the DevTools Protocol.

This is the ChatGPT twin of claude_chat.py. It rides the owner's logged-in
chatgpt.com session -- the same mechanism the sibling `ai` repo's
chatgpt_export_cdp.py uses to read conversation history -- instead of an
OpenAI API key. Everything runs as fetch() inside the real page, so the
browser supplies auth and passes chatgpt.com's edge protection that rejects
bare HTTP clients.

One ChatGPT message is meaningfully harder to send than a Claude one, because
chatgpt.com gates its send endpoint behind a "sentinel" proof-of-work the read
endpoints don't require. A full round trip, all inside the tab:

  GET  /api/auth/session                        -> { accessToken }
  POST /backend-api/sentinel/chat-requirements  -> { token, proofofwork{seed,difficulty,required} }
       (solve the proof-of-work: find a payload whose SHA3-512(seed+payload)
        hex-prefix clears `difficulty`; the answer token rides the next request)
  POST /backend-api/conversation                -> text/event-stream (SSE) reply
  PATCH /backend-api/conversation/{id}           -> {is_visible:false}  (best-effort tidy-up)

Model + reasoning: the default model spec is the sentinel `latest-high`, which
the send resolves live from GET /backend-api/models -- always the newest
version at its High preset (the maximum-reasoning "thinking" model), so a new
model generation is picked up with no code change. A concrete `DISCOVERY_MODEL`
still pins explicitly: a bare slug, or `slug:effort` to also fix the thinking
effort. The current latest High preset is a *thinking* model, and thinking
models no longer stream their answer inline -- the POST returns only a
`stream_handoff` frame -- so when the send comes back with a conversation id but
no text, the answer is read back by polling GET /backend-api/conversation/{id}
until the assistant message is finished. Non-thinking models still stream
inline and skip the poll.

The proof-of-work hash is SHA3-512, which the browser's SubtleCrypto cannot do,
so a compact pure-JS keccak is embedded below (SHA3_JS). It was cross-checked
byte-for-byte against Python's hashlib.sha3_512 on several vectors before
shipping; the server only checks the answer's hash prefix, so the config array
fed to the hash is just entropy, not a browser fingerprint that has to match.

Same honest caveats as claude_chat: these are undocumented internal endpoints
that can drift, and heavy automated use sits uneasily with chatgpt.com's terms
-- the per-cycle score budget keeps volume modest. And the same consequences:
  * No structured outputs: complete_json() prompts for strict JSON, extracts
    the first {...} block, validates it against the caller's schema, and
    retries once before giving up (shared with claude_chat).
  * No usage metering: the SSE stream carries no token counts, so only call
    counts are recorded.
  * Web search is ChatGPT's own search tool, requested per message via
    system_hints=["search"]; search_json() cannot cap the number of searches,
    so max_searches becomes a prompt instruction rather than a hard limit.
  * Reasoning cost: latest-high runs a thinking model at extended effort for
    every call, scoring included, which is slower and heavier on the account's
    thinking quota than a non-thinking model -- pin DISCOVERY_MODEL to a bare
    slug (no effort) to opt a deployment out.

Needs: Chrome launched with --remote-debugging-port (CHATGPT_BROWSER_PORT, or
CLAUDE_BROWSER_PORT, default 9222) and a chatgpt.com tab logged in inside it.
No org id, no API key.
"""
import json
import os
import time

from . import cdp
from .base import LLMProvider, ProviderError, parse_json_array, parse_json_object
from .claude_chat import _extract_object, _validate  # same "JSON out of a chat" helpers

DEFAULT_PORT = 9222
PREFLIGHT_TIMEOUT_SECONDS = 5  # a port that accepts but never answers must not hang the free check
ORIGIN = "https://chatgpt.com"

# "always the newest model at max reasoning" -- resolved live per send, so a new
# generation needs no code change (see module docstring / _js_completion).
LATEST_HIGH = "latest-high"
# Only used if GET /backend-api/models can't be read at send time -- the
# last-known newest thinking slug + its High-preset effort. Refresh when a newer
# generation ships; the live resolve makes this a rarely-hit safety net.
FALLBACK_MODEL = "gpt-5-6-thinking"
FALLBACK_EFFORT = "extended"

# Thinking models answer on a handed-off stream, so the send returns a
# conversation id but no inline text; poll the conversation until the assistant
# message is finished. Interval/timeout are generous -- a missed poll just costs
# one more read, and a truly stuck turn falls back to a normal ProviderError.
POLL_INTERVAL_SECONDS = 3

SETUP_HINT = (
    "launch Chrome with --remote-debugging-port={port} and log into "
    "chatgpt.com in that window"
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


class ChatGPTBrowserProvider(LLMProvider):
    name = "chatgpt_browser"

    def __init__(self, model, port=None, connect=None):
        super().__init__(model)
        if port is None:
            port = os.environ.get("CHATGPT_BROWSER_PORT") or os.environ.get(
                "CLAUDE_BROWSER_PORT", DEFAULT_PORT
            )
        self.port = int(port)
        # `connect` returns something with .evaluate(js, timeout=) / .close();
        # injectable so tests never open a socket.
        self._connect = connect or self._connect_chrome
        self._connection = None

    # --- preflight ------------------------------------------------------------

    def preflight(self):
        """Free, local check: is there a chatgpt.com tab in the Chrome on this
        port? Login itself can't be confirmed without spending a call, so this
        matches claude_chat -- tab present is as far as a free check goes."""
        try:
            tab = cdp.find_chatgpt_tab(self.port, timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except (OSError, ValueError) as e:
            return False, f"no Chrome DevTools endpoint on port {self.port} ({e})"
        if not tab:
            return False, f"no open chatgpt.com tab in the Chrome on port {self.port}"
        return True, ""

    # --- the two provider methods -------------------------------------------

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        # max_tokens is part of the contract but chatgpt.com's endpoint has no
        # output cap parameter; accepted and ignored.
        base = f"{system}\n\n{prompt}" + STRICT_JSON_SUFFIX.format(schema=json.dumps(schema))
        last_error = None
        for attempt in range(2):
            text = self._completion(base if attempt == 0 else base + RETRY_SUFFIX, search=False)
            try:
                data = parse_json_object(_extract_object(text))
                _validate(data, schema)
                return data
            except ProviderError as e:
                last_error = ProviderError(f"chatgpt.com reply attempt {attempt + 1}: {e}")
        raise last_error

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        # No hard search cap on this endpoint -- state it in the prompt instead.
        full = f"{prompt}\n\nUse web search (at most {max_searches} searches)."
        text = self._completion(full, search=True, timeout=420)
        return parse_json_array(text)

    # --- one chatgpt.com round trip -----------------------------------------

    def _completion(self, prompt, search, timeout=240):
        """One message round trip; text of the reply. A dropped Chrome
        connection gets one reconnect-and-retry (the tab survives, the
        websocket dies); everything else -- JS exceptions, HTTP errors from
        chatgpt.com, an empty reply -- is a ProviderError the pipeline treats
        like any other scoring failure."""
        try:
            return self._attempt(prompt, search, timeout)
        except (ConnectionError, OSError):
            self._reset()
            try:
                return self._attempt(prompt, search, timeout)
            except (ConnectionError, OSError) as e2:
                self._reset()
                raise ProviderError(f"chatgpt.com connection failed twice: {e2}") from e2

    def _attempt(self, prompt, search, timeout):
        conn = self._conn()
        try:
            raw = conn.evaluate(_js_completion(prompt, self.model, search), timeout=timeout)
        except RuntimeError as e:
            # JS exception inside the tab (chatgpt.com HTTP error, expired
            # session, turnstile demand). CDP connection itself is fine.
            raise ProviderError(f"chatgpt.com call failed: {e}") from e

        self.record_usage()  # calls only; this transport reports no token counts
        if raw is None:
            raise ProviderError(
                "chatgpt.com returned no text -- the Chrome tab was likely "
                "navigated/reloaded mid-request; leave the chatgpt.com tab alone"
            )
        result = raw if isinstance(raw, dict) else json.loads(raw)
        if not isinstance(result, dict):
            raise ProviderError(f"unexpected completion payload from chatgpt.com: {result!r}")

        conv_id = result.get("conversation_id")
        text = result.get("text") or ""
        # Thinking models (the latest-high default) stream on a handed-off
        # channel, so the send returns a stream_handoff and no inline text --
        # read the answer back by polling the conversation until it's finished.
        # Non-thinking models stream inline and already have their text here.
        if not text.strip() and conv_id:
            text = self._poll(conn, conv_id, timeout)

        if conv_id:  # best-effort: hide the scratch conversation, never fatal
            try:
                conn.evaluate(_js_hide_conversation(conv_id), timeout=15)
            except Exception:  # noqa: BLE001
                pass

        if not text.strip():
            raise ProviderError("empty completion from chatgpt.com")
        return text

    def _poll(self, conn, conv_id, timeout):
        """Read a handed-off (thinking-model) answer back from the conversation.
        Loops GET /backend-api/conversation/{id} until the assistant's text
        message reports finished, or `timeout` elapses. A read failure (socket
        drop mid-poll, HTTP error) is not retried with a fresh send -- that
        would double-post; we return whatever text arrived (empty -> the caller
        raises a normal, skippable ProviderError)."""
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                raw = conn.evaluate(_js_poll(conv_id), timeout=30)
            except (RuntimeError, ConnectionError, OSError):
                break
            if raw is None:
                continue
            data = raw if isinstance(raw, dict) else json.loads(raw)
            if not isinstance(data, dict):
                continue
            if data.get("text"):
                text = data["text"]
            if data.get("done"):
                break
        return text

    # --- session --------------------------------------------------------------

    def _conn(self):
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _connect_chrome(self):
        hint = SETUP_HINT.format(port=self.port)
        try:
            tab = cdp.find_chatgpt_tab(self.port)
        except (OSError, ConnectionError) as e:
            raise ProviderError(
                f"no Chrome DevTools endpoint on port {self.port} ({e}) -- {hint}"
            ) from e
        if not tab:
            raise ProviderError(
                f"no open chatgpt.com tab in the Chrome on port {self.port} -- {hint}"
            )
        try:
            return cdp.CDPConnection(tab["webSocketDebuggerUrl"])
        except (OSError, ConnectionError) as e:
            raise ProviderError(f"could not attach to the chatgpt.com tab: {e}") from e

    def _reset(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001
                pass
        self._connection = None


# --- chatgpt.com internal endpoints, run as JS inside the authenticated tab ----

# Compact pure-JS SHA3-512 (keccak-f[1600], rate 72 bytes). BigInt lanes: slower
# than a 32-bit split but far less error surface, and ~18k hashes/s in V8 is
# ample -- a typical chat-requirements difficulty solves in a few hundred
# iterations (<0.2s). Verified byte-for-byte against Python hashlib.sha3_512 on
# '', 'abc', a pangram, and a 200-char string before shipping. `sha3_512_hex`
# takes a string (UTF-8 encoded internally) and returns lowercase hex.
SHA3_JS = r"""
function sha3_512_hex(input) {
  const msg = (typeof input === 'string') ? new TextEncoder().encode(input) : input;
  const RC = [
    0x0000000000000001n,0x0000000000008082n,0x800000000000808an,0x8000000080008000n,
    0x000000000000808bn,0x0000000080000001n,0x8000000080008081n,0x8000000000008009n,
    0x000000000000008an,0x0000000000000088n,0x0000000080008009n,0x000000008000000an,
    0x000000008000808bn,0x800000000000008bn,0x8000000000008089n,0x8000000000008003n,
    0x8000000000008002n,0x8000000000000080n,0x000000000000800an,0x800000008000000an,
    0x8000000080008081n,0x8000000000008080n,0x0000000080000001n,0x8000000080008008n];
  const r = [0,1,62,28,27, 36,44,6,55,20, 3,10,43,25,39, 41,45,15,21,8, 18,2,61,56,14];
  const MASK = (1n<<64n)-1n;
  const rotl = (x,n) => n===0 ? x : (((x<<BigInt(n))|(x>>BigInt(64-n)))&MASK);
  let s = new Array(25).fill(0n);
  const rate = 72;
  const bytes = Array.from(msg); bytes.push(0x06);
  while (bytes.length % rate !== 0) bytes.push(0x00);
  bytes[bytes.length-1] |= 0x80;
  for (let off=0; off<bytes.length; off+=rate) {
    for (let i=0;i<rate/8;i++){let lane=0n;for(let b=0;b<8;b++)lane|=BigInt(bytes[off+i*8+b])<<BigInt(8*b);s[i]^=lane;}
    for (let round=0; round<24; round++) {
      const C=new Array(5);for(let x=0;x<5;x++)C[x]=s[x]^s[x+5]^s[x+10]^s[x+15]^s[x+20];
      const D=new Array(5);for(let x=0;x<5;x++)D[x]=C[(x+4)%5]^rotl(C[(x+1)%5],1);
      for(let x=0;x<5;x++)for(let y=0;y<5;y++)s[x+5*y]^=D[x];
      const B=new Array(25).fill(0n);
      for(let x=0;x<5;x++)for(let y=0;y<5;y++)B[y+5*((2*x+3*y)%5)]=rotl(s[x+5*y],r[x+5*y]);
      for(let x=0;x<5;x++)for(let y=0;y<5;y++)s[x+5*y]=B[x+5*y]^((~B[(x+1)%5+5*y]&MASK)&B[(x+2)%5+5*y]);
      s[0]^=RC[round];
    }
  }
  const out=[];for(let i=0;i<8;i++){let lane=s[i];for(let b=0;b<8;b++)out.push(Number((lane>>BigInt(8*b))&0xffn));}
  return out.map(x=>x.toString(16).padStart(2,'0')).join('');
}
"""


def _js_completion(prompt, model, search):
    """One evaluate() that does the whole send: session token -> resolve the
    model + reasoning effort -> sentinel chat-requirements + proof-of-work ->
    POST conversation -> read the SSE stream. Returns JSON.stringify({text,
    conversation_id, handoff, model, effort}); `handoff` true means the answer
    streams on a handed-off channel and the caller must poll for it. Any
    HTTP/auth failure throws, surfacing as a ProviderError."""
    prompt_json = json.dumps(prompt)
    model_spec_json = json.dumps(model or "")
    latest_high_json = json.dumps(["", "auto", "latest", LATEST_HIGH])
    fallback_model_json = json.dumps(FALLBACK_MODEL)
    fallback_effort_json = json.dumps(FALLBACK_EFFORT)
    hints_json = json.dumps(["search"] if search else [])
    return SHA3_JS + f"""
(async () => {{
  const ORIGIN = {json.dumps(ORIGIN)};
  const b64json = (arr) => btoa(String.fromCharCode.apply(null, new TextEncoder().encode(JSON.stringify(arr))));
  const scr = (() => {{ try {{ return (screen.width||0)+(screen.height||0); }} catch (_) {{ return 3000; }} }})();
  const ua = navigator.userAgent, lang = navigator.language || 'en-US';

  // 1) access token from the logged-in session (same call the ai repo reads with)
  const sess = await fetch(ORIGIN + '/api/auth/session', {{ headers: {{ Accept: 'application/json' }}, credentials: 'include' }});
  if (!sess.ok) throw new Error('session HTTP ' + sess.status);
  const token = (await sess.json()).accessToken;
  if (!token) throw new Error('not logged in to chatgpt.com (no accessToken)');
  const auth = {{ 'Authorization': 'Bearer ' + token }};

  // 1b) resolve the model + reasoning effort. A sentinel spec means "newest
  // version at its High (max-reasoning) preset", read live so a new model
  // generation is used with no code change; 'slug:effort' or a bare slug pins.
  let model = null, effort = null;
  const spec = {model_spec_json};
  if ({latest_high_json}.includes(spec)) {{
    try {{
      const mres = await fetch(ORIGIN + '/backend-api/models?history_and_training_disabled=true', {{
        headers: Object.assign({{ Accept: 'application/json' }}, auth), credentials: 'include' }});
      if (mres.ok) {{
        const mj = await mres.json();
        const latest = (mj.versions || [])[0];   // "Latest" version group is first
        if (latest) {{
          const RANK = {{ standard: 1, extended: 2, heavy: 3 }};
          for (const p of (latest.intelligence_presets || [])) {{
            if (p.lane !== 'thinking') continue;
            if (!model || (RANK[p.thinking_effort] || 0) >= (RANK[effort] || -1)) {{
              model = p.model_slug; effort = p.thinking_effort;
            }}
          }}
        }}
        if (!model) model = mj.default_model_slug || null;
      }}
    }} catch (_) {{ /* fall through to the fallback below */ }}
    if (!model) {{ model = {fallback_model_json}; effort = {fallback_effort_json}; }}
  }} else if (spec.indexOf(':') !== -1) {{
    const ix = spec.indexOf(':'); model = spec.slice(0, ix); effort = spec.slice(ix + 1) || null;
  }} else {{
    model = spec;
  }}

  // 2) sentinel chat-requirements, then the proof-of-work if it demands one
  const pInit = 'gAAAAAC' + b64json([scr, new Date().toString(), 4294705152, 0, ua, '', '', lang]);
  const reqRes = await fetch(ORIGIN + '/backend-api/sentinel/chat-requirements', {{
    method: 'POST', credentials: 'include',
    headers: Object.assign({{ 'Content-Type': 'application/json', Accept: 'application/json' }}, auth),
    body: JSON.stringify({{ p: pInit }})
  }});
  if (!reqRes.ok) throw new Error('chat-requirements HTTP ' + reqRes.status + ': ' + (await reqRes.text()).slice(0,200));
  const req = await reqRes.json();
  const sentinel = {{ 'OpenAI-Sentinel-Chat-Requirements-Token': req.token }};
  // Turnstile: chatgpt.com routinely sets required:true for logged-in web
  // sessions but accepts the challenge value (`dx`) echoed straight back as
  // the turnstile token -- verified live. Forward it rather than refusing; if
  // it were genuinely enforced and rejected, the conversation POST 403s and
  // surfaces as a normal ProviderError.
  if (req.turnstile && req.turnstile.required && req.turnstile.dx) {{
    sentinel['OpenAI-Sentinel-Turnstile-Token'] = req.turnstile.dx;
  }}
  const pow = req.proofofwork;
  if (pow && pow.required) {{
    const now = new Date().toString();
    const CAP = 150000;  // ~8s worst case; real difficulty is far easier
    let answer = null;
    for (let i = 0; i < CAP; i++) {{
      const b64 = b64json([scr, now, 4294705152, i, ua, '', '', lang, '', i % 17]);
      const h = sha3_512_hex(pow.seed + b64);
      if (h.substring(0, pow.difficulty.length) <= pow.difficulty) {{ answer = 'gAAAAAB' + b64; break; }}
    }}
    // give up gracefully like the real client rather than hang: send an
    // unsolved token; the server rejects with 403 and we surface that.
    sentinel['OpenAI-Sentinel-Proof-Token'] = answer || ('gAAAAAB' + b64json([scr, now, 4294705152, 0, ua, '', '', lang]));
  }}

  // 3) the actual message; history disabled so it isn't saved to the account
  const body = {{
    action: 'next',
    messages: [{{ id: crypto.randomUUID(), author: {{ role: 'user' }},
                 content: {{ content_type: 'text', parts: [{prompt_json}] }}, metadata: {{}} }}],
    parent_message_id: crypto.randomUUID(),
    model: model,
    timezone_offset_min: new Date().getTimezoneOffset(),
    history_and_training_disabled: true,
    conversation_mode: {{ kind: 'primary_assistant' }},
    force_paragen: false, force_rate_limit: false,
    system_hints: {hints_json}
  }};
  // High/max reasoning: send the resolved thinking effort when there is one.
  if (effort) body.thinking_effort = effort;
  const res = await fetch(ORIGIN + '/backend-api/conversation', {{
    method: 'POST', credentials: 'include',
    headers: Object.assign({{ 'Content-Type': 'application/json', Accept: 'text/event-stream' }}, auth, sentinel),
    body: JSON.stringify(body)
  }});
  if (!res.ok) throw new Error('conversation HTTP ' + res.status + ': ' + (await res.text()).slice(0,300));

  // 4) accumulate the assistant text across both the full-snapshot and the
  // delta-encoded (v1) SSE shapes chatgpt.com uses.
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '', text = '', convId = null, lastPath = null, handoff = false;
  // Assistant snapshots are cumulative; a trailing empty one (or an early
  // status placeholder) must not wipe the answer, so only overwrite with a
  // non-empty join -- verified against live chatgpt.com frames.
  const setParts = (parts) => {{
    if (!Array.isArray(parts)) return;
    const joined = parts.filter(p => typeof p === 'string').join('');
    if (joined) text = joined;
  }};
  const PARTS = '/message/content/parts/0';
  while (true) {{
    const {{ done, value }} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {{ stream: true }});
    const lines = buf.split('\\n');
    buf = lines.pop();
    for (const line of lines) {{
      if (!line.startsWith('data:')) continue;
      const p = line.slice(5).trim();
      if (!p || p === '[DONE]') continue;
      let e; try {{ e = JSON.parse(p); }} catch (_) {{ continue; }}
      // thinking models hand the answer off to a separate stream: the POST
      // ends here with only a conversation id, and the caller polls for text.
      if (e.type === 'stream_handoff') {{ handoff = true; if (e.conversation_id) convId = e.conversation_id; continue; }}
      if (e.conversation_id) convId = e.conversation_id;
      // full-message snapshot (older format; cumulative, so overwrite)
      if (e.message && e.message.author && e.message.author.role === 'assistant' &&
          e.message.content && e.message.content.content_type === 'text') {{
        setParts(e.message.content.parts); continue;
      }}
      // delta v1 initial add: v carries the whole message tree
      if (e.o === 'add' && e.v && e.v.message) {{
        if (e.v.conversation_id) convId = e.v.conversation_id;
        const m = e.v.message;
        if (m.author && m.author.role === 'assistant' && m.content && m.content.content_type === 'text') setParts(m.content.parts);
        lastPath = PARTS; continue;
      }}
      // delta append to the text part, either explicitly pathed or continuing the last path
      if (typeof e.v === 'string' && (e.p === PARTS || (e.p === undefined && lastPath === PARTS))) {{
        text += e.v; continue;
      }}
      if (e.p !== undefined) lastPath = e.p;
      // batched patch ops in one event
      if (Array.isArray(e.v)) {{
        for (const op of e.v) if (op && typeof op.v === 'string' && op.p === PARTS) text += op.v;
      }}
    }}
  }}
  return JSON.stringify({{ text, conversation_id: convId, handoff, model, effort }});
}})()
"""


def _js_poll(conv_id):
    """Read a handed-off answer back: GET the conversation and return
    JSON.stringify({text, done}) for the assistant's user-facing text message.
    `done` is true only once that message reports finished_successfully, so the
    caller keeps polling while the model is still thinking/streaming. The
    leading /* poll */ marker lets the test seam tell a poll from a send."""
    cid = json.dumps(conv_id)
    return f"""
/* poll */
(async () => {{
  const ORIGIN = {json.dumps(ORIGIN)};
  const sess = await fetch(ORIGIN + '/api/auth/session', {{ headers: {{ Accept: 'application/json' }}, credentials: 'include' }});
  if (!sess.ok) throw new Error('session HTTP ' + sess.status);
  const token = (await sess.json()).accessToken;
  const c = await fetch(ORIGIN + '/backend-api/conversation/' + {cid}, {{
    headers: {{ Accept: 'application/json', 'Authorization': 'Bearer ' + token }}, credentials: 'include' }});
  if (!c.ok) throw new Error('conversation GET HTTP ' + c.status);
  const cj = await c.json();
  let text = '', done = false;
  for (const n of Object.values(cj.mapping || {{}})) {{
    const m = n && n.message; if (!m) continue;
    if (m.author && m.author.role === 'assistant' && m.recipient === 'all' &&
        m.content && m.content.content_type === 'text') {{
      const parts = Array.isArray(m.content.parts) ? m.content.parts.filter(p => typeof p === 'string').join('') : '';
      if (parts) {{ text = parts; done = (m.status === 'finished_successfully'); }}
    }}
  }}
  return JSON.stringify({{ text, done }});
}})()
"""


def _js_hide_conversation(conv_id):
    """PATCH is_visible:false -- tidy the scratch conversation out of history.
    history_and_training_disabled usually stops it being saved at all, so this
    only fires when a conversation_id came back anyway. Best-effort."""
    cid = json.dumps(conv_id)
    return f"""
(async () => {{
  try {{
    const sess = await fetch('{ORIGIN}/api/auth/session', {{ headers: {{ Accept: 'application/json' }}, credentials: 'include' }});
    const token = (await sess.json()).accessToken;
    await fetch('{ORIGIN}/backend-api/conversation/' + {cid}, {{
      method: 'PATCH', credentials: 'include',
      headers: {{ 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token }},
      body: JSON.stringify({{ is_visible: false }})
    }});
  }} catch (e) {{}}
  return true;
}})()
"""
