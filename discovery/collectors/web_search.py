"""General web search, via the provider's search capability.

Reusing the LLM we already depend on means no second API key and no scraper
to maintain: the provider runs the searches, reads the results, and hands back
a JSON list. Providers without web search raise UnsupportedCapability, which
the pipeline treats like any other collector failure.

This is a *collector*: it must not judge relevance. That is scoring.py's job,
against the interest's own bar.
"""
from . import _search

PROMPT = """\
Find recent, substantive web content matching this interest.

Interest: {title}
Description: {description}
Worth surfacing: {positive}
Not worth surfacing: {negative}

Search the web ({max_uses} searches max), then return AT MOST {limit} items as
a JSON array. Prefer primary sources (papers, filings, release notes, original
posts) over aggregators, and recent material over evergreen explainers.

{result_spec}
"""


def collect(interest, cfg, provider, conn=None):
    opts = interest.source_config.get("web_search", {})
    limit = opts.get("limit", cfg.max_items_per_source)
    max_uses = opts.get("max_searches", 5)

    raw_items = provider.search_json(
        PROMPT.format(
            title=interest.title,
            description=interest.description,
            positive=", ".join(interest.positive_signals) or "(unspecified)",
            negative=", ".join(interest.negative_signals) or "(unspecified)",
            limit=limit,
            max_uses=max_uses,
            result_spec=_search.RESULT_SPEC,
        ),
        max_searches=max_uses,
    )
    return _search.to_items(raw_items, "web_search", limit)
