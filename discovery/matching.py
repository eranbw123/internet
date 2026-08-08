"""Interest matching and the cheap pre-filter.

Both stages exist to keep the LLM bill proportional to the interesting mail.
Matching is a keyword heuristic, not a judgement: it decides which interests
an item is *plausibly* about so the scorer gets the right context, and gives a
rough strength used to drop obvious noise. Anything subtler than "these words
never appear anywhere" is the scorer's job.
"""
import re

from .models import clamp01

# An item collected *for* an interest is always considered a match for it, at
# least this strongly -- the collector already had a reason, even if the
# wording doesn't overlap.
ORIGIN_MATCH_FLOOR = 0.5

PHRASE_WEIGHT = 0.5      # a full positive-signal phrase appearing verbatim
TOKEN_WEIGHT = 0.12      # one distinctive word in common
NEGATIVE_TITLE_PENALTY = 0.3

# Sources whose items are legitimately a couple of lines long.
SHORT_FORM_SOURCES = {"stocks"}

STOPWORDS = {
    "about", "actual", "actually", "after", "against", "both", "content",
    "from", "generic", "into", "less", "like", "more", "much", "national",
    "over", "same", "some", "such", "than", "that", "them", "then", "there",
    "these", "they", "thing", "things", "this", "those", "very", "what",
    "when", "which", "while", "with", "without", "worth", "your",
}


def match_interests(item, interests):
    """Rank `interests` by how plausibly this item is about them.

    Returns [(interest, match_score, matched_terms)] sorted strongest first,
    dropping interests with no signal at all.
    """
    matches = []
    for interest in interests:
        score, terms = _match_one(item, interest)
        if interest.key and interest.key == item.origin_interest:
            score = max(score, ORIGIN_MATCH_FLOOR)
            terms = terms or ["(collected for this interest)"]
        if score > 0:
            matches.append((interest, round(score, 3), terms))
    matches.sort(key=lambda m: m[1], reverse=True)
    return matches


def _match_one(item, interest):
    haystack = f"{item.title}\n{item.text}".lower()
    title = item.title.lower()
    score, terms = 0.0, []

    for phrase in (s.lower() for s in interest.positive_signals if " " in s):
        if phrase in haystack:
            score += PHRASE_WEIGHT
            terms.append(phrase)

    shared = _tokens(interest.title + " " + " ".join(interest.positive_signals)) & _tokens(haystack)
    score += TOKEN_WEIGHT * len(shared)
    terms.extend(sorted(shared)[:8])

    # A negative signal in the *title* is a strong steer: it describes what the
    # piece is, not something it mentions in passing.
    for negative in (s.lower() for s in interest.negative_signals):
        if negative in title:
            score -= NEGATIVE_TITLE_PENALTY

    return clamp01(score), terms


def _tokens(text):
    """Distinctive words only -- 4+ chars, not stopwords."""
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in STOPWORDS}


def prefilter(item, matches, cfg):
    """Decide whether this item is worth an LLM call. Returns (ok, reason).

    Cheap and conservative: it only rejects things no scorer could rate well
    (nothing to read, nothing to read it against), so a miss here costs a
    fraction of a cent rather than a missed story.
    """
    if not item.title or not item.url:
        return False, "missing title or url"

    body = f"{item.title} {item.text}".strip()
    if item.source not in SHORT_FORM_SOURCES and len(body) < cfg.min_text_chars:
        return False, f"only {len(body)} chars of text to judge"

    if not matches:
        return False, "matched no interest"

    interest, score, terms = matches[0]
    if score < cfg.min_match_score:
        return False, f"weak match ({score:.2f}) to best interest '{interest.key}'"

    return True, f"matched '{interest.key}' ({score:.2f}) on {', '.join(terms[:4]) or 'origin'}"
