"""OpenAI provider: JSON-schema structured outputs. No search capability.

Here so the pipeline can be pointed at a second vendor by changing
DISCOVERY_PROVIDER, with no pipeline code aware of either.
"""
import os
from datetime import datetime, timezone

from .base import LLMProvider, ProviderError, UnsupportedCapability, parse_json_object


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        started = _now_iso()
        params = {"max_completion_tokens": max_tokens}
        try:
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
        except Exception as e:  # noqa: BLE001
            self._emit_call(None, 1, system, prompt, schema, params, None, None,
                             None, str(e), started, _now_iso())
            raise
        usage = getattr(response, "usage", None)
        self.record_usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )
        raw_text = response.choices[0].message.content
        try:
            parsed = parse_json_object(raw_text)
            validation, error = "valid", None
        except ProviderError as e:
            parsed, validation, error = None, f"invalid: {e}", str(e)
        self._emit_call(
            None, 1, system, prompt, schema, params, raw_text, parsed, validation, error,
            started, _now_iso(), request_id=getattr(response, "id", None),
            usage={"input_tokens": getattr(usage, "prompt_tokens", None),
                   "output_tokens": getattr(usage, "completion_tokens", None)} if usage else None,
        )
        if error:
            raise ProviderError(error)
        return parsed

    def search_json(self, prompt, max_searches=5, max_tokens=8000):
        # The web_search collector is skipped rather than half-working: this
        # provider has no equivalent of Claude's server-side search tool.
        raise UnsupportedCapability("openai provider has no web search capability")
