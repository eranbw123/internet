"""Council-driven research mission planning.

The reasoning architecture -- five independent advisors deliberating in
Stage 1, an anonymized Stage 2 peer review where each advisor ranks all five
responses (including, unknowingly, their own), and a Stage 3 Chairman
synthesis -- is ported verbatim in substance from the sibling `ai` repo's
`council_bot.py` (COUNCIL_INSTRUCTIONS constant). This repo never imports,
execs, path-hacks or shells out to the `ai` application at runtime; only the
instruction text was copied by hand. Dropped from the port: the
`ai`-specific sampled-past-conversation context paragraph and the "THE
CALL:" phone-sized prose output. Replaced: the Chairman's visible output is
now exactly N genuinely distinct research missions, as strict JSON, instead
of one prose answer.

GOODHART FIREWALL: build_context()/render_prompt() must never surface
downstream scoring machinery (min_score, models.WEIGHTS, dimension names,
final_score, confidence, a notification bar) to the Council -- it plans
retrieval, and must never see the target it would be tempted to optimize
for. See test_discovery.py's firewall assertion.
"""
from . import db

COUNCIL_INSTRUCTIONS = """\
You are going to run an "LLM Council" deliberation on a research planning question, entirely on \
your own, by simulating five independent advisors and a chairman. Follow the three stages \
exactly, doing all of the work internally in your reasoning.

THE FIVE ADVISORS (each is a distinct persona -- stay fully in character, do not let them agree \
by default):
- Advisor 1 -- The Contrarian: only looks at what will fail. Surfaces risks, failure modes, and \
reasons this goes wrong.
- Advisor 2 -- The First-Principles Thinker: rips apart every assumption baked into the question \
and rebuilds from the ground up.
- Advisor 3 -- The Expansionist: finds the upside, the bigger opportunity, and the option not \
being seen.
- Advisor 4 -- The Outsider: knows nothing about the relevant industry; reasons from common \
sense and naive questions, ignoring jargon and "how it's always done."
- Advisor 5 -- The Executor: only cares about what to actually DO next. Concrete, sequenced, \
practical.

=== STAGE 1: INDEPENDENT ANSWERS ===
Each of the five advisors answers the question independently and in their own voice. They must \
NOT reference each other.

=== STAGE 2: ANONYMIZED PEER REVIEW ===
Relabel the five Stage 1 answers as "Response A, B, C, D, E" in a random order. Then, acting as \
each advisor in turn, have them review all five anonymized responses -- including, unknowingly, \
their own -- evaluate each briefly, and produce a ranking. Compute an aggregate ranking across \
all five reviewers.

=== STAGE 3: CHAIRMAN'S FINAL CALL ===
Act as the Chairman. Using all five answers and the peer rankings (pay special attention to the \
responses that ranked highest, and to any disagreement between advisors), synthesize the final \
output.

This time, ALSO surface your work: alongside the missions, return a "deliberation" object \
recording what actually happened in the three stages above -- five advisor entries (name, \
persona, their independent analysis), the anonymized peer-review round (each reviewer's \
critiques and ranking of Responses A-E), the aggregate ranking, any real disagreement between \
advisors, any angle that was considered and rejected (and why), and the Chairman's own synthesis \
and rationale for which angles became the final missions. This is not a second deliberation --\
 it is a record of the one you already did internally. Never invent detail that didn't actually \
happen; if a stage produced nothing noteworthy for a field (e.g. no real disagreement), say so \
plainly rather than padding it.
"""

# Instructs the SAME complete_json call to also return a "deliberation"
# object -- no second call, no extra spend (see the module docstring's
# GOODHART FIREWALL note: this section must stay free of scoring machinery,
# same as everything else the Council sees or returns).
# Every nested object below declares properties/required/additionalProperties
# (not left bare) so OpenAI strict structured outputs -- which hard-require
# that shape on every object, recursively -- can accept MISSION_SCHEMA at
# all. _extract_deliberation() already parses each section tolerantly, so a
# model that omits or malforms one still produces a valid deliberation.
DELIBERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "advisors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "persona": {"type": "string"},
                    "analysis": {"type": "string"},
                },
                "required": ["name", "persona", "analysis"],
                "additionalProperties": False,
            },
        },
        "peer_review": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reviewer": {"type": "string"},
                    "critiques": {"type": "string"},
                    "ranking": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["reviewer", "critiques", "ranking"],
                "additionalProperties": False,
            },
        },
        "aggregate_ranking": {"type": "array", "items": {"type": "string"}},
        "disagreements": {"type": "string"},
        "rejected_angles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "angle": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["angle", "reason"],
                "additionalProperties": False,
            },
        },
        "chairman_synthesis": {"type": "string"},
        "selection_rationale": {"type": "string"},
    },
    "required": [
        "advisors", "peer_review", "aggregate_ranking", "disagreements",
        "rejected_angles", "chairman_synthesis", "selection_rationale",
    ],
    "additionalProperties": False,
}

# Sections _extract_deliberation() looks for -- each is graded independently,
# so one malformed/missing section never drops the rest.
DELIBERATION_SECTIONS = (
    "advisors", "peer_review", "aggregate_ranking", "disagreements",
    "rejected_angles", "chairman_synthesis", "selection_rationale",
)

MISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "missions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "rationale": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["label", "rationale", "prompt"],
                "additionalProperties": False,
            },
        },
        "deliberation": DELIBERATION_SCHEMA,
    },
    # Only "missions" is required here -- that's the strict, byte-compatible
    # CouncilError contract. "deliberation" stays optional at this layer:
    # claude_chat/chatgpt_browser's hand-rolled _validate() enforces
    # `required` verbatim (no recursion, no tolerant parsing) as the only
    # schema check they have, so requiring "deliberation" here would turn a
    # missing/malformed deliberation into a failed generation on the default
    # provider, instead of the lenient {'unavailable': True, ...} marker
    # _extract_deliberation() already produces. OpenAI strict structured
    # outputs need every property required; that's a transport-specific
    # constraint enforced by openai_provider._strict_schema() on a copy of
    # this schema, not here.
    "required": ["missions"],
    "additionalProperties": False,
}


class CouncilError(Exception):
    """Council output failed validation: wrong type, a missing/empty field,
    zero valid missions, or a case-insensitive label collision. Never raised
    for a missing/malformed "deliberation" section -- that's lenient (see
    _extract_deliberation) since it's a debugging trail, not something
    downstream missions/pipeline code acts on."""


def build_context(conn, interest, cfg):
    """Assemble the Council's planning context. Every part is bounded by a
    cfg value -- see the module docstring's Goodhart firewall note: nothing
    here ever touches interest.min_score, models.WEIGHTS, a dimension name,
    final_score or confidence."""
    frontier = conn.execute(
        """
        SELECT title, url, published_at FROM candidate_items
        WHERE origin_interest = ?
        ORDER BY id DESC LIMIT ?
        """,
        (interest.key, cfg.council_frontier_items),
    ).fetchall()
    feedback = (
        db.recent_feedback(conn, interest.id, cfg.council_feedback_items)
        if interest.id is not None else []
    )
    history = db.recent_missions(conn, interest.key, cfg.council_history_missions)
    return {
        "interest": interest,
        "frontier": [dict(r) for r in frontier],
        "feedback": [dict(r) for r in feedback],
        "history": [dict(r) for r in history],
    }


def _bullets(items):
    return "\n".join(f"  - {s}" for s in items) if items else "  (none)"


def render_prompt(context, count):
    interest = context["interest"]

    frontier_block = "\n".join(
        f"  - {row['title']} ({row['published_at'] or 'undated'}) {row['url']}"
        for row in context["frontier"]
    ) or "  (none yet)"

    feedback_block = "\n".join(
        f"  - {row['verdict']}: {row['title']}" + (f" -- {row['note']}" if row["note"] else "")
        for row in context["feedback"]
    ) or "  (none yet)"

    history_block = "\n".join(
        f"  - {row['label']}: {row['rationale']}" for row in context["history"]
    ) or "  (none yet)"

    return f"""\
=== INTEREST (verbatim, as the owner wrote it) ===
Title: {interest.title}
Description: {interest.description}
Positive signals (worth surfacing):
{_bullets(interest.positive_signals)}
Negative signals (not worth surfacing):
{_bullets(interest.negative_signals)}

=== RECENT DISCOVERY FRONTIER (already found, newest first -- do not just repeat these) ===
{frontier_block}

=== RECENT USER FEEDBACK (what actually landed or missed) ===
{feedback_block}

=== PREVIOUS MISSIONS FOR THIS INTEREST (newest first -- do not repeat an angle) ===
{history_block}

=== YOUR TASK ===
Plan exactly {count} genuinely distinct web-research missions that would surface new, \
worthwhile material for this interest. Each mission needs:
  - "label": a short slug-ish name, unique among the missions you return (no two may share a \
label, even by case)
  - "rationale": why this specific angle is worth spending a real web search on right now
  - "prompt": a complete, self-contained instruction to an executor who will run this mission \
with no other context than what you write here -- it must stand alone.

Also return your "deliberation" trail, exactly as the system instructions describe: the five \
advisors' independent analyses, the anonymized peer review (critiques + per-reviewer ranking), \
the aggregate ranking, any genuine disagreement, angles you considered and rejected (with why), \
and the Chairman's synthesis + selection rationale. Best-effort and honest, not padded -- an \
empty or "none" value for a field that genuinely had nothing is fine.

Return strict JSON: {{"missions": [{{"label": ..., "rationale": ..., "prompt": ...}}, ...], \
"deliberation": {{...}}}}, exactly {count} mission entries, nothing else.
"""


def plan_missions(provider, interest, context, count):
    """One provider.complete_json() call -> (missions, deliberation).

    `missions` is a validated list of at most `count` mission dicts
    ({label, rationale, prompt}) -- CouncilError on anything the pipeline
    can't safely act on, byte-compatible with this function's behavior
    before deliberation existed. `deliberation` is the SAME response's
    "deliberation" object, graded leniently section by section (see
    _extract_deliberation) -- it is never what raises CouncilError, and a
    provider that omits it entirely still returns a fully valid mission list."""
    system = COUNCIL_INSTRUCTIONS.format(count=count)
    prompt = render_prompt(context, count)
    data = provider.complete_json(system, prompt, MISSION_SCHEMA)
    missions = _validate_missions(data, count)
    deliberation = _extract_deliberation(data if isinstance(data, dict) else {})
    return missions, deliberation


def _validate_missions(data, count):
    if not isinstance(data, dict):
        raise CouncilError(f"expected a JSON object, got {type(data).__name__}")
    raw = data.get("missions")
    if not isinstance(raw, list):
        raise CouncilError("Council response missing a 'missions' array")

    missions = []
    seen_labels = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise CouncilError(f"mission entry is not an object: {entry!r}")
        label = entry.get("label")
        rationale = entry.get("rationale")
        mission_prompt = entry.get("prompt")
        if not isinstance(label, str) or not label.strip():
            raise CouncilError(f"mission missing a non-empty 'label': {entry!r}")
        if not isinstance(rationale, str):
            raise CouncilError(f"mission missing a 'rationale': {entry!r}")
        if not isinstance(mission_prompt, str) or not mission_prompt.strip():
            raise CouncilError(f"mission missing a non-empty 'prompt': {entry!r}")
        key = label.strip().lower()
        if key in seen_labels:
            raise CouncilError(f"duplicate mission label (case-insensitive): {label!r}")
        seen_labels.add(key)
        missions.append({
            "label": label.strip(), "rationale": rationale.strip(), "prompt": mission_prompt.strip(),
        })

    if len(missions) < 1:
        raise CouncilError("Council returned zero valid missions")
    return missions[:count]   # truncate/ignore extras past `count` rather than failing


def _unavailable(reason):
    return {"unavailable": True, "reason": reason}


# Expected shape per deliberation section -- a value of the wrong type is
# exactly as "malformed" as a missing one (see _extract_deliberation).
_LIST_SECTIONS = {"advisors", "peer_review", "aggregate_ranking", "rejected_angles"}
_STRING_SECTIONS = {"disagreements", "chairman_synthesis", "selection_rationale"}


def _extract_deliberation(data):
    """Best-effort, section-by-section extraction of the deliberation trail
    from the SAME response the missions came from -- never a second call,
    never fatal, never invented. A missing OR wrong-shaped section becomes
    {'unavailable': True, 'reason': ...} in its place; missions.py persists
    whatever comes back, verbatim, as trace nodes. Never claims
    provider-private hidden reasoning -- only what the response actually
    contains."""
    raw = data.get("deliberation") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {
            name: _unavailable("no 'deliberation' object in the response")
            for name in DELIBERATION_SECTIONS
        }
    out = {}
    for name in DELIBERATION_SECTIONS:
        value = raw.get(name)
        if value is None:
            out[name] = _unavailable(f"'{name}' missing from deliberation")
        elif name in _LIST_SECTIONS and not isinstance(value, list):
            out[name] = _unavailable(f"'{name}' was not a list: {value!r}")
        elif name in _STRING_SECTIONS and not isinstance(value, str):
            out[name] = _unavailable(f"'{name}' was not a string: {value!r}")
        else:
            out[name] = value
    return out
