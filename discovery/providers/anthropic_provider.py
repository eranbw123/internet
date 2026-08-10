"""Claude provider: structured outputs for scoring, server-side web search."""
import os

from .base import LLMProvider, ProviderError, parse_json_array, parse_json_object


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
        self._record(response)
        return parse_json_object(_text(_finished(response)))

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        self._record(response)
        return parse_json_array(_text(response))

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
