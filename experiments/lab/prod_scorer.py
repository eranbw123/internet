"""Score items with the PRODUCTION scorer, optionally under a prompt variant.

Experiments must measure against the same judge production uses, so this
adapter calls discovery.scoring.score_candidate itself rather than a lab
reimplementation. Variants temporarily swap the module-level SYSTEM/PROMPT
text; everything else (schema, parsing, interest fallback, final_score
weighting in code) stays exactly the production path.
"""
from contextlib import contextmanager

from lab_common import REPO  # noqa: F401  (imports fix sys.path for discovery.*)

from discovery import scoring  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402
from discovery.providers import get_provider  # noqa: E402


def provider():
    return get_provider(load_cfg())


@contextmanager
def prompt_variant(system=None, prompt=None):
    """Swap scoring.SYSTEM / scoring.PROMPT for the duration of the block.
    Lab-only trick: production never does this, and reusing score_candidate
    beats reimplementing its parsing and fallbacks in the lab."""
    saved = scoring.SYSTEM, scoring.PROMPT
    if system is not None:
        scoring.SYSTEM = system
    if prompt is not None:
        scoring.PROMPT = prompt
    try:
        yield
    finally:
        scoring.SYSTEM, scoring.PROMPT = saved


def score_item(prov, item, interest, match_score=0.5, feedback=()):
    """One production scoring call: the item judged against the single
    interest it was originally matched to."""
    return scoring.score_candidate(prov, item, [(interest, match_score, [])], feedback)
