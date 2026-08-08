"""LLM scoring: one call per candidate, judged against its matched interests.

The model returns six 0-1 dimensions plus the structured fields a
notification needs. It does *not* return the final score -- that weighting is
`models.final_score()`, computed here in code so the ranking formula can
change without re-scoring anything, and so a model can't inflate its own
verdict. `specificity` is scored and stored but sits outside the current
weights for the same reason.

The model also picks which interest the item is most relevant to, from the
shortlist matching.py handed it.
"""
from .models import DIMENSIONS, ScoreResult, clamp01, final_score

SYSTEM = """\
You judge whether a piece of content is worth interrupting one specific person \
for. You are strict: their attention is the scarce resource, and a push that \
turns out to be filler costs more than a miss. Judge substance and novelty \
against the stated interests, never general quality or popularity. Rate what \
the item actually contains, not what its headline promises."""

PROMPT = """\
<interests>
{interests}
</interests>
{feedback}
<item>
Source: {source} ({type})
Title: {title}
Author: {author}
Published: {published}
URL: {url}

{text}
</item>

Pick the single interest this item is most relevant to, then rate it 0.0-1.0 \
on each dimension. Use the full range; 0.5 means genuinely middling, not "unsure".

- personal_relevance: how squarely this lands on that interest as described, \
including its stated negative signals
- novelty: how much of this is new rather than a restatement of what someone \
following this interest already knows
- depth: substance and rigour -- primary data, mechanism, methods -- versus \
summary or commentary
- specificity: how concrete it is (named findings, numbers, systems) versus \
general or hedged
- importance: how much this changes what is true or what someone should do
- surprise: how much it cuts against the expected or the consensus

Also return:
- interest_key: the key of the interest you chose, exactly as given above
- confidence: 0.0-1.0, how sure you are of these ratings given what you can \
actually see of the item
- reason: one sentence, in the second person, on why this may interest them, \
naming the specific thing that earned the score
- why_better_than_generic: one sentence on what this has that generic coverage \
of the same topic does not. Empty string if it has nothing.
"""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "interest_key": {"type": "string"},
        **{name: {"type": "number"} for name in DIMENSIONS},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "why_better_than_generic": {"type": "string"},
    },
    "required": ["interest_key", *DIMENSIONS, "confidence", "reason", "why_better_than_generic"],
    "additionalProperties": False,
}


class ScoringError(Exception):
    pass


def score_candidate(provider, item, matches, feedback=()):
    """Score `item` against its matched interests. `matches` is the output of
    matching.match_interests(): [(interest, match_score, terms)]."""
    if not matches:
        raise ScoringError(f"item {item.id} has no matched interests to score against")

    data = provider.complete_json(SYSTEM, _prompt(item, matches, feedback), SCORE_SCHEMA)
    interest = _resolve_interest(data.get("interest_key"), matches)
    dimensions = {name: clamp01(data.get(name)) for name in DIMENSIONS}

    return ScoreResult(
        item_id=item.id,
        interest_id=interest.id,
        interest_key=interest.key,
        dimensions=dimensions,
        final_score=final_score(dimensions),
        confidence=clamp01(data.get("confidence")),
        reason=str(data.get("reason") or "").strip(),
        why_better_than_generic=str(data.get("why_better_than_generic") or "").strip(),
        provider=provider.name,
        model=provider.model,
    )


def _resolve_interest(key, matches):
    """Fall back to the strongest keyword match if the model names an interest
    that isn't on the shortlist -- a bad key shouldn't waste the whole call."""
    for interest, _score, _terms in matches:
        if interest.key == key:
            return interest
    return matches[0][0]


def _prompt(item, matches, feedback):
    return PROMPT.format(
        interests="\n".join(_interest_block(i, s) for i, s, _t in matches),
        feedback=_feedback_block(feedback),
        source=item.source,
        type=item.type,
        title=item.title,
        author=item.author or "unknown",
        published=item.published_at or "unknown",
        url=item.url,
        text=item.text or "(no body text available)",
    )


def _interest_block(interest, match_score):
    return (
        f"<interest key=\"{interest.key}\" keyword_match=\"{match_score:.2f}\">\n"
        f"Title: {interest.title}\n"
        f"Description: {interest.description}\n"
        f"Worth surfacing: {', '.join(interest.positive_signals) or '(unspecified)'}\n"
        f"Not worth surfacing: {', '.join(interest.negative_signals) or '(unspecified)'}\n"
        f"</interest>"
    )


_VERDICT_PHRASING = {
    "fire": "loved",
    "up": "liked",
    "down": "disliked",
    "trash": "rejected as a bad match",
}


def _feedback_block(feedback):
    """Past verdicts, as worked examples. Empty until the user rates something."""
    if not feedback:
        return ""
    lines = [
        f"- {_VERDICT_PHRASING.get(row['verdict'], row['verdict'])}: {row['title']}"
        + (f" ({row['note']})" if row["note"] else "")
        for row in feedback
    ]
    return "\n<past_verdicts>\n" + "\n".join(lines) + "\n</past_verdicts>\n"
