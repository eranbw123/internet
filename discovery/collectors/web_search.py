"""General web search, via the provider's search capability.

Reusing the LLM we already depend on means no second API key and no scraper
to maintain: the provider runs the searches, reads the results, and hands back
a JSON list. Providers without web search raise UnsupportedCapability, which
the pipeline treats like any other collector failure.

This is a *collector*: it must not judge relevance. That is scoring.py's job,
against the interest's own bar.
"""
from ..models import CandidateItem

PROMPT = """\
Find recent, substantive web content matching this interest.

Interest: {title}
Description: {description}
Worth surfacing: {positive}
Not worth surfacing: {negative}

Search the web ({max_uses} searches max), then return AT MOST {limit} items as
a JSON array. Prefer primary sources (papers, filings, release notes, original
posts) over aggregators, and recent material over evergreen explainers.

Return only the JSON array, no prose around it. Each element:
  {{"title": str, "url": str, "summary": str, "author": str|null,
    "published_at": "YYYY-MM-DD"|null}}
`summary` is 2-4 sentences of what the item actually says -- enough for a
relevance judgement without opening the link. Use [] if nothing qualifies.
"""


def collect(interest, cfg, provider):
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
        ),
        max_searches=max_uses,
    )
    return _to_items(raw_items, limit)


def _to_items(raw_items, limit):
    items = []
    for raw in raw_items[:limit]:
        if not isinstance(raw, dict):
            continue
        url = (raw.get("url") or "").strip()
        if not url:
            continue
        items.append(
            CandidateItem(
                source="web_search",
                type="article",
                title=raw.get("title") or url,
                url=url,
                text=raw.get("summary") or "",
                author=raw.get("author"),
                published_at=raw.get("published_at"),
            )
        )
    return items
