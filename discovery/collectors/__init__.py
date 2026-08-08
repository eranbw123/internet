"""Collectors turn one Interest into raw CandidateItems.

The shared interface is deliberately just a function -- no base class, no
plugin loader:

    collect(interest, cfg, provider, conn=None) -> list[CandidateItem]

Add a module, add it to COLLECTORS, and any interest can name it in its
`sources` list. Per-source knobs come from `interest.source_config[name]`.

Collectors do the minimum: fetch and shape. They do not normalize (normalize.py),
dedup (dedup.py), or judge relevance (scoring.py) -- and one that raises is
logged and skipped, never fatal to the cycle.

`conn` is read-only and optional, for one job: checking `db.seen_dedup_keys`
to skip a candidate the collector would otherwise *pay* to build (an LLM
explanation, a transcript fetch) only for dedup.py to discard it downstream.
It is not a licence to write, score, or filter -- that all still belongs to the
pipeline.
"""
from . import stocks, web, web_search, youtube

COLLECTORS = {
    "web_search": web_search.collect,
    "web": web.collect,
    "youtube": youtube.collect,
    "stocks": stocks.collect,
}
