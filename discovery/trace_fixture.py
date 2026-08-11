"""Deterministic trace acceptance fixture (step-13 task 1).

`build(conn, cfg)` drives the REAL production code paths (missions.web_tick,
pipeline.ingest/deliver/send_digest, feedback_listener._handle_callback) --
not a hand-rolled trace-table writer -- against deterministic fake providers
(same "needle in the prompt" seam test_discovery.py's FakeProvider/
FakeCouncilProvider use) so tasks 2/3's Datasette plugin and React UI have a
known-shape database to build against without a live Chrome/CDP session or
network access.

Produces, against a fresh (or at least fixture-untouched) `conn`:
  - one owner interest
  - one Council generation: five advisor analyses, an anonymized peer-review
    round, an aggregate ranking, one rejected angle, and a Chairman synthesis
    -- three missions
  - five raw search results across those three missions, covering:
      * one duplicate (two raw results, same url)
      * one prefilter rejection (too little text to judge)
      * one scoring failure followed by a retry (two model_calls rows on the
        same score-attempt node) that ultimately succeeds above the bar
      * one score that succeeds but lands below the bar
      * the retried item's above-bar score, delivered through send_digest()
        and given one piece of feedback through feedback_listener's own
        callback handler
  - `python -m app trace-fixture --db PATH` is the CLI entry point --
    `--db` is required (refuses to run without it) so an unflagged
    invocation can never fall back to cfg.db_path's real default.

Structurally deterministic (fixed labels, fixed content, fixed call order --
same node/edge/model_call COUNTS and LABELS on every call against a fresh
DB), not byte-identical on wall-clock timestamps: trace.py's timestamps come
from datetime.now(), which this module does not attempt to freeze -- there
is no injectable clock seam in db.py/trace.py today, and nothing in this
step's acceptance criteria needs one (see test_discovery.py's
TraceFixtureTests, which compares counts/labels, not timestamps).
"""
import json
from unittest import mock

from . import db, feedback_listener, missions, models, pipeline, providers, trace
from .models import Interest
from .providers.base import LLMProvider

INTEREST_KEY = "fixture-interest"

MISSION_LABELS = ("primary-sources", "cross-references", "recent-coverage")

ADVISOR_PERSONAS = (
    ("Advisor 1", "The Contrarian"),
    ("Advisor 2", "The First-Principles Thinker"),
    ("Advisor 3", "The Expansionist"),
    ("Advisor 4", "The Outsider"),
    ("Advisor 5", "The Executor"),
)

DELIBERATION = {
    "advisors": [
        {
            "name": name, "persona": persona,
            "analysis": f"{persona} take on the fixture interest, independent of the others.",
        }
        for name, persona in ADVISOR_PERSONAS
    ],
    "peer_review": [
        {
            "reviewer": name,
            "critiques": "Response C was the most concrete; Response E restated the question.",
            "ranking": ["C", "A", "B", "D", "E"],
        }
        for name, _persona in ADVISOR_PERSONAS
    ],
    "aggregate_ranking": ["C", "A", "B", "D", "E"],
    "disagreements": "The Contrarian and The Expansionist split on whether primary sources are reachable at all.",
    "rejected_angles": [
        {"angle": "social-media-sentiment", "reason": "too noisy to distinguish signal from restatement"},
    ],
    "chairman_synthesis": "Favor primary sources and cross-references over a broad recency sweep.",
    "selection_rationale": "Response C's angle (primary sources) generalizes best; folded a second angle in for coverage.",
}


def _mission_batch():
    return {
        "missions": [
            {
                "label": label,
                "rationale": f"why {label} is worth a real web search right now",
                "prompt": f"research {label} thoroughly",
            }
            for label in MISSION_LABELS
        ],
        "deliberation": DELIBERATION,
    }


DUPLICATE_URL = "https://fixture.example.com/duplicate-topic"
BELOW_BAR_URL = "https://fixture.example.com/below-threshold"
BREAKTHROUGH_URL = "https://fixture.example.com/breakthrough"

def _long_enough(label):
    """Distinct, >200-char body per item -- dedup.py's content-hash layer
    would otherwise treat every fixture item as a duplicate of the others
    (same body text), which is not the branch being demonstrated here."""
    return (f"Enough body text for the pre-filter to consider {label} worth an LLM call. " * 3)


SEARCH_RESULTS = {
    "primary-sources": [
        {"title": "Duplicate Topic Finding", "url": DUPLICATE_URL, "summary": _long_enough("this finding")},
        {"title": "Duplicate Topic Finding (mirror)", "url": DUPLICATE_URL, "summary": _long_enough("this finding")},
    ],
    "cross-references": [
        {"title": "Short", "url": "https://fixture.example.com/short", "summary": ""},
    ],
    "recent-coverage": [
        {"title": "Fixture Below Threshold Finding", "url": BELOW_BAR_URL,
         "summary": _long_enough("the below-threshold finding")},
        {"title": "Fixture Breakthrough Finding", "url": BREAKTHROUGH_URL,
         "summary": _long_enough("the breakthrough finding")},
    ],
}

# Only "recent-coverage"'s search_json call exposes tool events -- the other
# two missions get the "not exposed by provider" node, proving that path
# too (never fabricated for missions that didn't report any).
TOOL_EVENTS = {
    "recent-coverage": [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "fixture breakthrough finding"}},
        {"type": "web_search_tool_result", "name": "web_search", "input": {"result_count": 2}},
    ],
}


class _FixtureMissionProvider(LLMProvider):
    """Search-capable fake standing in for cfg.mission_provider. One Council
    call (asserted), then one search_json call per mission, dispatched by
    label (mirrors test_discovery.py's FakeCouncilProvider)."""

    name = "fixture_mission"

    def __init__(self):
        super().__init__("fixture-mission-1")
        self._council_called = False

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        if self._council_called:
            raise AssertionError("fixture: Council should be called exactly once")
        self._council_called = True
        payload = _mission_batch()
        self._emit_call(
            None, 1, system, prompt, schema, None,
            json.dumps(payload), payload, "valid", None, _now(), _now(),
        )
        return payload

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        for label, results in SEARCH_RESULTS.items():
            if label not in prompt:
                continue
            self.last_events = TOOL_EVENTS.get(label)
            self._emit_call(
                None, 1, None, prompt, None, None,
                json.dumps(results), results, "array", None, _now(), _now(),
                events=self.last_events,
            )
            return results
        raise AssertionError(f"fixture: unexpected mission prompt:\n{prompt}")


class _FixtureScoringProvider(LLMProvider):
    """Scoring fake standing in for cfg.provider. Dispatches on a needle in
    the prompt (the item's title, same convention as test_discovery.py's
    FakeProvider) -- BREAKTHROUGH_URL's item fails its first attempt
    (malformed reply) and succeeds on a real internal retry, exactly the
    shape claude_chat.py/chatgpt_browser.py's own two-attempt loop has, so
    it produces two model_calls rows under one score-attempt node without
    scoring.score_candidate() itself ever seeing a raised exception."""

    name = "fixture_scoring"

    def __init__(self):
        super().__init__("fixture-scoring-1")

    def complete_json(self, system, prompt, schema, max_tokens=2000):
        # The FIRST "Duplicate Topic Finding" raw result reaches scoring
        # normally (only the second, identical-url occurrence is caught by
        # dedup before it ever gets here) -- scored plainly, no retry, so the
        # duplicate branch stays a clean demonstration on its own.
        if "Duplicate Topic Finding" in prompt:
            return self._respond(system, prompt, schema, value=0.5, attempts=1)
        if "Fixture Below Threshold Finding" in prompt:
            return self._respond(system, prompt, schema, value=0.15, attempts=1)
        if "Fixture Breakthrough Finding" in prompt:
            return self._respond(system, prompt, schema, value=0.95, attempts=2)
        raise AssertionError(f"fixture: unexpected scoring prompt:\n{prompt}")

    def _respond(self, system, prompt, schema, value, attempts):
        for attempt in range(1, attempts + 1):
            started = _now()
            if attempt < attempts:   # every attempt but the last fails, on purpose
                error = "fixture: malformed scoring reply"
                self._emit_call(None, attempt, system, prompt, schema, None,
                                 "not valid json", None, "invalid", error, started, _now())
                continue
            payload = _score_payload(value)
            self._emit_call(None, attempt, system, prompt, schema, None,
                             json.dumps(payload), payload, "valid", None, started, _now())
            return payload
        raise AssertionError("unreachable")   # attempts >= 1 always returns above


def _score_payload(value):
    payload = {name: value for name in models.DIMENSIONS}
    payload.update(
        interest_key=INTEREST_KEY,
        confidence=0.85,
        reason="Fixture reason: names the specific evidence this item contains.",
        why_better_than_generic="Fixture why-better: has the numbers generic coverage omits.",
        evidence_used="Fixture evidence_used: the item's own summary text.",
        why_this_interest="Fixture why_this_interest: only shortlisted interest.",
        dimension_rationale={"personal_relevance": "on-topic" if value > 0.5 else "tangential"},
        alternative_interpretation="Fixture alternative_interpretation: could read as routine coverage.",
        why_not_higher="" if value > 0.9 else "Fixture why_not_higher: thin on primary evidence.",
        uncertainties="Fixture uncertainties: none material." if value > 0.5 else "Fixture uncertainties: mostly summary, no primary source.",
    )
    return payload


def _now():
    return db.now()


def build(conn, cfg):
    """Runs the fixture against `conn`/`cfg`. Returns a small summary dict.
    Meant for a fresh DB (or at least one this fixture hasn't already
    populated) -- see the module docstring's determinism note."""
    db.init(conn)

    interest = Interest(
        key=INTEREST_KEY,
        title="Fixture interest",
        description="A deterministic interest used only by trace_fixture.py.",
        positive_signals=["fixture finding", "breakthrough"],
        negative_signals=["unrelated noise"],
        min_score=0.75,
        sources=["web_search"],
    )
    db.upsert_interest(conn, interest)

    mission_provider = _FixtureMissionProvider()
    scoring_provider = _FixtureScoringProvider()

    fixture_cfg = _fixture_cfg(cfg)

    with mock.patch.object(providers, "get_provider", return_value=mission_provider):
        tick_result = missions.web_tick(conn, fixture_cfg, provider=scoring_provider, dry_run=True)

    digest_tracer = trace.Tracer(conn, fixture_cfg)
    digest_run_id = digest_tracer.begin_run("fixture-digest", provider=scoring_provider.name)
    sent = pipeline.send_digest(conn, fixture_cfg, dry_run=True, tracer=digest_tracer)
    digest_tracer.finish_run(digest_run_id, status="done")

    feedback_recorded = _record_feedback(conn, fixture_cfg)

    return {
        "interest": INTEREST_KEY,
        "missions_generated": len(MISSION_LABELS),
        "web_tick": tick_result,
        "digest_sent": sent,
        "feedback_recorded": feedback_recorded,
    }


def _fixture_cfg(cfg):
    import dataclasses

    return dataclasses.replace(
        cfg,
        council_missions_per_generation=len(MISSION_LABELS),
        missions_per_tick=len(MISSION_LABELS),
        mission_low_water=len(MISSION_LABELS),
        mission_max_results=len(MISSION_LABELS),
        min_text_chars=120,
        min_match_score=0.25,
        digest_max_items=10,
    )


def _record_feedback(conn, cfg):
    """One piece of feedback on the item that cleared the bar, through the
    REAL feedback_listener._handle_callback() path -- notify.api_call is
    stubbed (same seam test_discovery.py's FeedbackListenerTests uses) so
    this never touches a real Telegram endpoint."""
    row = conn.execute(
        """
        SELECT s.id FROM scores s
        JOIN candidate_items i ON i.id = s.item_id
        WHERE i.title = 'Fixture Breakthrough Finding'
        """
    ).fetchone()
    if row is None:
        return False
    callback = {
        "id": "fixture-callback-1",
        "data": f"fb:fire:{row['id']}",
        "message": {"chat": {"id": 1}},
    }
    with mock.patch.object(feedback_listener, "api_call", lambda *a, **kw: {}):
        return feedback_listener._handle_callback(conn, "fixture-token", callback, cfg)
