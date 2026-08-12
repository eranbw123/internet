"""The provider contract.

Two methods, because the pipeline needs exactly two things from an LLM:

    complete_json(system, prompt, schema) -> dict     structured judgement
    search_json(prompt)                   -> list     web search + extraction

Anything a provider can't do raises UnsupportedCapability, which callers
treat like any other collector/scorer failure: log it, skip, keep going.
Nothing above this module imports a vendor SDK.

Each provider also tallies what it spent in `self.usage`; `db.record_usage`
drains it into `llm_usage` at the end of a command. Kept in the provider (and
not, say, threaded through every call site) because it is the one object every
paid call already passes through.
"""
import json
from collections import Counter


class ProviderError(Exception):
    """The provider ran but returned something unusable."""


class UnsupportedCapability(ProviderError):
    """This provider doesn't offer that capability at all."""


class LLMProvider:
    name = "base"

    def __init__(self, model):
        self.model = model
        self.usage = Counter()
        # Installed by discovery.trace.Tracer (see its `sink` method) when
        # tracing is on; a plain attribute, not a constructor arg, so every
        # existing call site (get_provider(), tests) is untouched. None means
        # "no one is listening" -- _emit_call is then a no-op.
        self.trace_sink = None
        # Best-effort search/tool events retained from the most recent
        # search_json() call (see claude_chat.py/chatgpt_browser.py's
        # _attempt()) -- None means this provider doesn't expose any (the
        # base default, and every non-browser provider); missions.py reads
        # this after each mission execution to write either one trace node
        # per event or a single "not exposed by provider" node.
        self.last_events = None

    def record_usage(self, input_tokens=0, output_tokens=0, web_searches=0):
        self.usage.update(
            calls=1,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            web_searches=int(web_searches or 0),
        )

    def _emit_call(self, call_role, attempt, system, prompt, schema, params,
                    raw_text, parsed, validation, error, started, finished,
                    request_id=None, usage=None, events=None):
        """Forward one provider-call ATTEMPT to the installed trace sink, if
        any. `call_role` is accepted for the sink's own signature but a
        provider never actually knows its caller's role -- that comes from
        the sink's own call-attribution context (see Tracer.calls()); a
        provider always passes None here and the sink fills it in. Never
        raises: a broken sink must not turn a real provider call into a
        failure (the sink itself is already fail-soft, but this is a second,
        cheaper layer since this runs on every call's hot path)."""
        sink = self.trace_sink
        if sink is None:
            return
        try:
            sink(
                call_role=call_role, attempt=attempt, provider=self.name, model=self.model,
                system=system, prompt=prompt, schema=schema, params=params,
                raw_text=raw_text, parsed=parsed, validation=validation, error=error,
                started=started, finished=finished, request_id=request_id,
                usage=usage, events=events,
            )
        except Exception:  # noqa: BLE001 -- tracing must never break a real call
            pass

    # max_tokens caps thinking + response together on current Claude models
    # (thinking is on by default), so both defaults carry headroom well past
    # the size of the JSON we actually ask for -- a tight cap truncates the
    # response mid-thought and turns every call into a ProviderError.
    def complete_json(self, system, prompt, schema, max_tokens=8000):
        raise NotImplementedError

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        raise UnsupportedCapability(f"{self.name} has no web search capability")

    def preflight(self):
        """Cheap reachability check, called by health.py before a single
        collector or LLM call is made. Default: always reachable -- a
        provider with nothing worth checking for free (an API-key vendor's
        actual availability can only be confirmed by spending a call)
        overrides this only for a free, local check (see ClaudeChatProvider,
        which can confirm Chrome/CDP/the claude.ai tab without any spend)."""
        return True, ""


def parse_json_object(text, what="response"):
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"unparseable {what}: {e}") from e
    if not isinstance(parsed, dict):
        raise ProviderError(f"expected a JSON object for {what}, got {type(parsed).__name__}")
    return parsed


def parse_json_array(text):
    """Pull a JSON array out of a reply that may be wrapped in prose or a fence.

    Search turns in particular like to narrate before the payload, so slice
    from the first '[' to the last ']' rather than trusting the whole string.
    """
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
