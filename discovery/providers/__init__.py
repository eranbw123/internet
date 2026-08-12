"""LLM providers. Swapping vendors is a config change, not a code change.

    DISCOVERY_PROVIDER=claude_chat     (default) -- claude.ai via an
                                       authenticated Chrome tab; no API key
    DISCOVERY_PROVIDER=chatgpt_browser -- chatgpt.com via an authenticated
                                       Chrome tab; no API key
    DISCOVERY_PROVIDER=anthropic       -- direct Anthropic API; needs
                                       ANTHROPIC_API_KEY
    DISCOVERY_PROVIDER=openai          -- direct OpenAI API; needs OPENAI_API_KEY

The pipeline only ever holds an LLMProvider; it never imports a vendor SDK,
so the whole difference between vendors lives in this package.
"""
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider, ProviderError, UnsupportedCapability
from .chatgpt_browser import ChatGPTBrowserProvider
from .claude_chat import ClaudeChatProvider
from .fallback import FallbackProvider
from .openai_provider import OpenAIProvider

PROVIDERS = {
    "claude_chat": ClaudeChatProvider,
    "chatgpt_browser": ChatGPTBrowserProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def _provider_class(name):
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ProviderError(
            f"unknown provider '{name}' (have: {', '.join(sorted(PROVIDERS))})"
        ) from None


def get_provider(cfg):
    provider = _provider_class(cfg.provider)(cfg.model)
    fallback_name = getattr(cfg, "provider_fallback", "") or ""
    # Wrapping a provider in itself would just pay for the same failure
    # twice, so an identical fallback is treated as "off".
    if fallback_name and fallback_name != cfg.provider:
        fallback_model = getattr(cfg, "provider_fallback_model", "")
        provider = FallbackProvider(
            provider, _provider_class(fallback_name)(fallback_model)
        )
    return provider
