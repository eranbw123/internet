"""Collectors turn one Interest into raw CandidateItems.

The shared interface is deliberately just a function -- no base class, no
plugin loader:

    collect(interest, cfg, provider) -> list[CandidateItem]

Add a module, add it to COLLECTORS, and any interest can name it in its
`sources` list. Per-source knobs come from `interest.source_config[name]`.

Collectors do the minimum: fetch and shape. They do not normalize (normalize.py),
dedup (dedup.py), or judge relevance (scoring.py) -- and one that raises is
logged and skipped, never fatal to the cycle.
"""
from . import stocks, web, web_search, youtube

COLLECTORS = {
    "web_search": web_search.collect,
    "web": web.collect,
    "youtube": youtube.collect,
    "stocks": stocks.collect,
}
