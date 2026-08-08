"""Generic web discovery: LLM-generated queries, then provider web search.

Cheap candidate generation, not a crawler -- V0 does not fetch or parse pages
itself. One small `complete_json` call turns the interest's description and
positive signals into a handful of query variations (deliberately not just
the interest title -- see QUERY_PROMPT); each query then gets its own
`search_json` call scoped to a recency window, and the query that found an
item is kept on `metadata["query"]` for inspection via `discover web`.

This is a different shape from web_search.py, which asks the model to plan
and run its own searches in one freeform pass with no visibility into what it
searched for. Both stay behind the same two-method LLMProvider interface --
no new provider, no scraper.

Cross-run repeats are not this module's job: dedup.py already rejects a
previously-seen URL/title/content before it reaches scoring, so nothing here
remembers queries or results between runs.
"""
import sys

from ..models import CandidateItem
from ..providers.base import ProviderError, UnsupportedCapability

DEFAULT_NUM_QUERIES = 4
DEFAULT_RECENCY_DAYS = 14

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["queries"],
}

QUERY_SYSTEM = (
    "You generate web search queries for a personal content-discovery tool. "
    "Return varied, specific queries a researcher would actually type -- not "
    "a restatement of the topic title."
)

QUERY_PROMPT = """\
Interest: {title}
Description: {description}
Worth surfacing: {positive}
Not worth surfacing: {negative}

Generate {num_queries} distinct web search queries that would surface recent,
substantive content for this interest. Vary the angle -- specific mechanisms,
products, people, or industry terms -- rather than repeating the interest
title. Short, search-engine-style queries, not questions.
"""

SEARCH_PROMPT = """\
Search the web for: {query}

Only return results plausibly published within the last {recency_days} days.
Return AT MOST {limit} items as a JSON array. Prefer primary sources (papers,
filings, release notes, original posts) over aggregators.

Return only the JSON array, no prose around it. Each element:
  {{"title": str, "url": str, "summary": str, "author": str|null,
    "published_at": "YYYY-MM-DD"|null}}
`summary` is 2-4 sentences of what the item actually says -- enough for a
relevance judgement without opening the link. Use [] if nothing qualifies.
"""


def collect(interest, cfg, provider):
    opts = interest.source_config.get("web", {})
    num_queries = opts.get("num_queries", DEFAULT_NUM_QUERIES)
    recency_days = opts.get("recency_days", DEFAULT_RECENCY_DAYS)
    limit = opts.get("limit", cfg.max_items_per_source)
    per_query_limit = opts.get("per_query_limit", max(2, limit // max(num_queries, 1)))

    items = []
    seen_urls = set()
    for query in _queries(interest, provider, num_queries):
        try:
            raw_items = provider.search_json(
                SEARCH_PROMPT.format(
                    query=query, recency_days=recency_days, limit=per_query_limit
                ),
                max_searches=1,
            )
        except UnsupportedCapability:
            raise
        except ProviderError as e:
            # One bad query shouldn't sink the others queued for this interest.
            print(f"web collector: query {query!r} failed: {e}", file=sys.stderr)
            continue
        for item in _to_items(raw_items, query, per_query_limit):
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def _queries(interest, provider, num_queries):
    payload = provider.complete_json(
        QUERY_SYSTEM,
        QUERY_PROMPT.format(
            title=interest.title,
            description=interest.description,
            positive=", ".join(interest.positive_signals) or "(unspecified)",
            negative=", ".join(interest.negative_signals) or "(unspecified)",
            num_queries=num_queries,
        ),
        QUERY_SCHEMA,
    )
    queries = [
        q.strip() for q in payload.get("queries", []) if isinstance(q, str) and q.strip()
    ]
    # A model that returns nothing usable still leaves the collector able to
    # search on the interest title rather than yielding zero candidates.
    return queries[:num_queries] or [interest.title]


def _to_items(raw_items, query, limit):
    items = []
    for raw in raw_items[:limit]:
        if not isinstance(raw, dict):
            continue
        url = (raw.get("url") or "").strip()
        if not url:
            continue
        items.append(
            CandidateItem(
                source="web",
                type="article",
                title=raw.get("title") or url,
                url=url,
                text=raw.get("summary") or "",
                author=raw.get("author"),
                published_at=raw.get("published_at"),
                metadata={"query": query},
            )
        )
    return items
