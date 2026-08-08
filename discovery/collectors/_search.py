"""One shape for every provider web-search result.

`web_search`, `web`, and `stocks` all ask the model for the same JSON array and
all turned it into CandidateItems in their own near-identical loop. Both halves
live here now: RESULT_SPEC is the prompt fragment that defines the shape, and
`to_items` is the parser that trusts nothing about it.
"""
from ..models import CandidateItem

RESULT_SPEC = """\
Return only the JSON array, no prose around it. Each element:
  {"title": str, "url": str, "summary": str, "author": str|null,
   "published_at": "YYYY-MM-DD"|null}
`summary` is 2-4 sentences of what the item actually says -- enough for a
relevance judgement without opening the link. Use [] if nothing qualifies."""


def to_items(raw_items, source, limit, item_type="article", metadata=None):
    """Shape a provider's JSON array into CandidateItems, skipping anything
    without a url (a model that returns prose or a half-filled row should cost
    that row, not the batch)."""
    items = []
    for raw in raw_items[:limit]:
        if not isinstance(raw, dict):
            continue
        url = (raw.get("url") or "").strip()
        if not url:
            continue
        items.append(
            CandidateItem(
                source=source,
                type=item_type,
                title=raw.get("title") or url,
                url=url,
                text=raw.get("summary") or "",
                author=raw.get("author"),
                published_at=raw.get("published_at"),
                metadata=dict(metadata or {}),
            )
        )
    return items
