"""OpenAI provider: JSON-schema structured outputs; server-side web search
via the Responses API's web_search tool.

Here so the pipeline can be pointed at a second vendor by changing
DISCOVERY_PROVIDER, with no pipeline code aware of either.
"""
import os

from .base import LLMProvider, ProviderError, parse_json_array, parse_json_object


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model, client=None):
        super().__init__(model)
        if client is None:
            import openai

            client = openai.OpenAI()
        self.client = client

    def preflight(self):
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY is not set"
        return True, ""

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        response = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            },
        )
        usage = getattr(response, "usage", None)
        self.record_usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )
        return parse_json_object(response.choices[0].message.content)

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        # OpenAI's web_search tool has no max_uses knob, so max_searches only
        # reaches the model through the prompt (the collectors already write it
        # in); the searches actually run are still counted for stats.py.
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            tools=[{"type": "web_search"}],
            max_output_tokens=max_tokens,
        )
        status = getattr(response, "status", "completed")
        if status != "completed":
            raise ProviderError(f"unusable response: status={status}")
        searches = sum(
            1
            for item in getattr(response, "output", None) or []
            if getattr(item, "type", "") == "web_search_call"
        )
        usage = getattr(response, "usage", None)
        self.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            web_searches=searches,
        )
        return parse_json_array(getattr(response, "output_text", "") or "")
