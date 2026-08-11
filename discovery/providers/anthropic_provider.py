"""Claude provider: structured outputs for scoring, server-side web search."""
import os
from datetime import datetime, timezone

from .base import LLMProvider, ProviderError, parse_json_array, parse_json_object


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model, client=None):
        super().__init__(model)
        # Imported lazily so the other provider works without this SDK installed.
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def preflight(self):
        # A key-presence check only -- confirming the key actually works
        # would mean spending a real call, which preflight must never do.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY is not set"
        return True, ""

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        started = _now_iso()
        params = {"max_tokens": max_tokens, "effort": "low"}
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": "low",
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001 -- SDK errors, recorded then re-raised
            self._emit_call(None, 1, system, prompt, schema, params, None, None,
                             None, str(e), started, _now_iso())
            raise
        self._record(response)
        raw_text = _text(response)
        try:
            parsed = parse_json_object(_text(_finished(response)))
            validation = "valid"
            error = None
        except ProviderError as e:
            parsed, validation, error = None, f"invalid: {e}", str(e)
        self._emit_call(
            None, 1, system, prompt, schema, params, raw_text, parsed, validation, error,
            started, _now_iso(), request_id=getattr(response, "id", None),
            usage=_usage_dict(response),
        )
        if error:
            raise ProviderError(error)
        return parsed

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        started = _now_iso()
        params = {"max_tokens": max_tokens, "max_searches": max_searches}
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                tools=[
                    {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}
                ],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001
            self._emit_call(None, 1, None, prompt, None, params, None, None,
                             None, str(e), started, _now_iso())
            raise
        self._record(response)
        raw_text = _text(response)
        parsed = parse_json_array(raw_text)
        self._emit_call(
            None, 1, None, prompt, None, params, raw_text, parsed, "array", None,
            started, _now_iso(), request_id=getattr(response, "id", None),
            usage=_usage_dict(response),
        )
        return parsed

    def _record(self, response):
        """Server-side web searches are billed per request, separately from
        tokens, so they are counted separately too (see stats.py)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        server_tools = getattr(usage, "server_tool_use", None)
        self.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            web_searches=getattr(server_tools, "web_search_requests", 0) if server_tools else 0,
        )


def _finished(response):
    """A refusal comes back as HTTP 200 with empty content, and max_tokens
    leaves truncated JSON -- check before reading either."""
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        raise ProviderError(f"unusable response: stop_reason={response.stop_reason}")
    return response


def _text(response):
    return "".join(block.text for block in response.content if block.type == "text")


def _usage_dict(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
