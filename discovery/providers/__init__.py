"""LLM providers. Swapping vendors is a config change, not a code change.

    DISCOVERY_PROVIDER=claude_chat  (default) -- claude.ai via an
                                    authenticated Chrome tab; no API key
    DISCOVERY_PROVIDER=anthropic    -- direct Anthropic API; needs
                                    ANTHROPIC_API_KEY
    DISCOVERY_PROVIDER=openai

The pipeline only ever holds an LLMProvider; it never imports a vendor SDK,
so the whole difference between vendors lives in this package.
"""
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider, ProviderError, UnsupportedCapability
from .claude_chat import ClaudeChatProvider
from .openai_provider import OpenAIProvider

PROVIDERS = {
    "claude_chat": ClaudeChatProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def get_provider(cfg):
    try:
        provider_class = PROVIDERS[cfg.provider]
    except KeyError:
        raise ProviderError(
            f"unknown provider '{cfg.provider}' (have: {', '.join(sorted(PROVIDERS))})"
        ) from None
    return provider_class(cfg.model)
