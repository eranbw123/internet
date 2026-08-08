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

    def record_usage(self, input_tokens=0, output_tokens=0, web_searches=0):
        self.usage.update(
            calls=1,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            web_searches=int(web_searches or 0),
        )

    # max_tokens caps thinking + response together on current Claude models
    # (thinking is on by default), so both defaults carry headroom well past
    # the size of the JSON we actually ask for -- a tight cap truncates the
    # response mid-thought and turns every call into a ProviderError.
    def complete_json(self, system, prompt, schema, max_tokens=8000):
        raise NotImplementedError

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        raise UnsupportedCapability(f"{self.name} has no web search capability")


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
