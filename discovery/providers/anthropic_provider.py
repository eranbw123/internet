"""Claude provider: structured outputs for scoring, server-side web search."""
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

    def complete_json(self, system, prompt, schema, max_tokens=2000):
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
        return parse_json_object(_text(_finished(response)))

    def search_json(self, prompt, max_searches=5, max_tokens=8000):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_json_array(_text(response))


def _finished(response):
    """A refusal comes back as HTTP 200 with empty content, and max_tokens
    leaves truncated JSON -- check before reading either."""
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        raise ProviderError(f"unusable response: stop_reason={response.stop_reason}")
    return response


def _text(response):
    return "".join(block.text for block in response.content if block.type == "text")
