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

Do all of Stage 1, Stage 2, and the Chairman's synthesis internally -- none of it should appear \
in your visible response. Your entire visible output is the Chairman's final call and nothing \
else: exactly {count} genuinely distinct research missions, as strict JSON matching the given \
schema. "Genuinely distinct" means different angles, different search strategies, different \
kinds of sources -- never the same query worded five different ways. If two missions would \
plausibly surface the same results, that is a failure of this deliberation: redo it internally \
before answering.
"""

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
    },
    "required": ["missions"],
    "additionalProperties": False,
}


class CouncilError(Exception):
    """Council output failed validation: wrong type, a missing/empty field,
    zero valid missions, or a case-insensitive label collision."""


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

Return strict JSON: {{"missions": [{{"label": ..., "rationale": ..., "prompt": ...}}, ...]}}, \
exactly {count} entries, nothing else.
"""


def plan_missions(provider, interest, context, count):
    """One provider.complete_json() call -> a validated list of at most
    `count` mission dicts ({label, rationale, prompt}). Raises CouncilError
    on anything the pipeline can't safely act on."""
    system = COUNCIL_INSTRUCTIONS.format(count=count)
    prompt = render_prompt(context, count)
    data = provider.complete_json(system, prompt, MISSION_SCHEMA)
    return _validate_missions(data, count)


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
