"""E5 -- read-only connector-reconnaissance harness (step-09a).

Step-09 will decide whether the discovery engine gains a new connector (a
new source/collector). This step collects the cheap, low-risk half of that
evidence EARLY: it does NOT register a collector, edit interests.json,
change config, or promote anything. Every write this module makes is either
to `experiments/lab/artifacts/connectors/` (gitignored, via `lab_common.Lab`)
or to the tracked `experiments/lab/connector_evidence.json` dossier.

=== PRE-REGISTRATION (frozen before any run) ===
HYPOTHESIS H1 (marginal retrieval surface): at least one candidate connector
supplies items the engine cannot already reach -- i.e. for that connector,
pooled over the probed interests, `marginal_unique_rate >= 0.40` AND
`jaccard_overlap_with_web_search_sample < 0.30`.

BASELINE: the existing `web_search` collector's sample over the same
interests, same window, same per-interest item cap. For the zero-spend
(offline) lane the baseline is the stored production corpus; if neither the
corpus nor a web_search sample is reachable, the metric is recorded as VOID,
never as 0. `marginal_unique_rate` is computed against the corpus alone
whenever `discovery.db` is reachable (an honest offline-lane substitute for
the full corpus-UNION-web_search_sample baseline). `jaccard_overlap_with_
web_search_sample` always needs an actual web_search sample, which needs a
provider call -- and THIS HARNESS VERSION DOES NOT YET IMPLEMENT a web_search
baseline sampler or an `x` connector sampler (both need `provider.search_
json`), independent of whether a live provider is reachable. So `x`'s entry
and the overlap component are recorded NOT_IMPLEMENTED, not merely PENDING
on an operator session -- see `lanes.live.blocking_work` in the persisted
dossier for exactly what code is missing.

PRIMARY METRIC: `marginal_unique_rate` = |sample_urls \\ (corpus_urls UNION
web_search_sample_urls)| / |sample_urls|, computed per connector, pooled
across probed interests, using canonicalized URLs and canonicalized titles.

SECONDARY METRICS (reported, explicitly NON-DECISIVE): count of sampled
items at/above the interest's own `min_score` under the frozen production
scorer (absolute count, never a share -- LAB.md guardrail 7 forbids
share-based criteria below 8 observations); median item age in days; sample
validity rate (fraction of returned records with both a URL and a title).

STOPPING CONDITION: exactly one pass per (connector, interest) pair at the
caps below. Queries are frozen in code before the run and derived
mechanically from the committed interest definition; NO query tuning, no
re-runs, no connector added after seeing results.

FALSIFICATION CONDITION: if every candidate connector has
`marginal_unique_rate < 0.40` or overlap >= 0.30, H1 is FALSIFIED -- "no
candidate connector adds retrieval surface; step-09 should not add a
connector on retrieval grounds." A falsified H1 is a SUCCESSFUL outcome of
this step, not a shortfall.

VALIDITY VOIDS (distinct from a negative result): network unavailable;
connector endpoint unreachable or shape-changed; production corpus
(discovery.db) unavailable; sample n < 5 for a connector; provider
unavailable for the scoring sub-metric.

=== PRE-REGISTERED SPEND BOUND (hard; exceeding it is a rollback trigger) ===
Paid API spend: $0.00 (no connector needing a key/paid tier/account; the
twitterapi.io fallback is explicitly EXCLUDED). YouTube Data API quota: 0
units (youtube is an existing collector, not a candidate). Free public HTTP:
<= 40 requests total, <= 10 per connector, one page per query (<= 50
records), 15s timeout, >= 1.0s spacing between requests to the same host
(>= 3.0s for arxiv), a descriptive non-spoofed User-Agent, no auth, no HTML
scraping -- documented public JSON/Atom endpoints only. Provider (LLM)
calls: 0 in the zero-spend lane; <= 40 in the live lane, enforced by
`Lab("connectors", budget_cap=40)`. No deliberate rate-limit stress testing
-- rate-limit behavior is read passively off whatever `X-RateLimit-*` /
`Retry-After` headers the bounded requests already return.

=== CANDIDATE CONNECTORS (frozen set of 5) ===
x (search_json, no scraping/API -- prior evidence exists and is RE-TESTED,
not trusted), hackernews (Algolia search_by_date, no key), reddit (public
.json listings, no key, UA-gated), arxiv (Atom API, no key), pubmed (NCBI
E-utilities esearch/esummary JSON, no key).

=== PROBED INTERESTS (frozen) ===
behavioral-psychology, personal-knowledge-learning, emdr-trauma-therapy (the
three starved interests), narcolepsy-eds, ai-agents-dev-tools -- read from
the committed interests.json via discovery/interests.py. APPLICABILITY below
is the frozen connector -> interest map (part of the pre-registration, kept
small enough to stay inside the request caps).

QUERY CONSTRUCTION: identical mechanical rule for every connector -- an
interest's title plus its first three positive_signals, joined with spaces
and capped at QUERY_MAX_CHARS. No per-connector hand tuning.

Modes:
    python experiments/lab/exp_connectors.py probe    # availability only, ~1 request/connector
    python experiments/lab/exp_connectors.py sample   # availability + bounded retrieval + analysis; writes connector_evidence.json
    python experiments/lab/exp_connectors.py report    # render the persisted dossier as text; zero spend

Does NOT touch exp_discovery.py or its `Lab("discovery")` state -- proposal
005 is running detached against it (engine-lab-005). The small
canonicalization/percentile helpers below are therefore a local, deliberate
duplicate of exp_discovery.py's private copies; collapsing them onto one
shared module is a follow-up once 005 has completed (see PROJECT_STATE.md).
"""
import argparse
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import db_replay
import prod_scorer
from lab_common import REPO, Lab, now, utf8_streams

from discovery import matching  # noqa: E402
from discovery.config import load as load_cfg  # noqa: E402
from discovery.interests import load_file as load_interests_file  # noqa: E402
from discovery.models import CandidateItem  # noqa: E402
from discovery.providers import get_provider  # noqa: E402

TOOL_VERSION = "1.0"
DOSSIER_PATH = Path(__file__).resolve().parent / "connector_evidence.json"

CONNECTORS = ("x", "hackernews", "reddit", "arxiv", "pubmed")
PROBED_INTERESTS = (
    "behavioral-psychology", "personal-knowledge-learning", "emdr-trauma-therapy",
    "narcolepsy-eds", "ai-agents-dev-tools",
)

# Frozen connector -> probed-interest applicability map (pre-registration).
# hackernews/arxiv skew toward the tech/academic interests; pubmed is
# clinical-only (its own explicit example); reddit and x cover all five since
# both are general-purpose discussion/social surfaces.
APPLICABILITY = {
    "x": PROBED_INTERESTS,
    "hackernews": ("ai-agents-dev-tools", "personal-knowledge-learning"),
    "reddit": PROBED_INTERESTS,
    "arxiv": ("behavioral-psychology", "personal-knowledge-learning", "ai-agents-dev-tools"),
    "pubmed": ("narcolepsy-eds", "emdr-trauma-therapy"),
}

QUERY_MAX_CHARS = 300
N_PER_QUERY = 10          # records requested per (connector, interest) query, <= the 50 cap
MAX_REQUESTS = 40
PER_CONNECTOR_CAP = 10
DEFAULT_MIN_GAP_SECONDS = 1.0
HOST_MIN_GAP_SECONDS = {"export.arxiv.org": 3.0}   # arxiv's own stated etiquette
USER_AGENT = ("engine-control-connector-recon/1.0 "
              "(read-only research probe for a personal discovery engine's lab; "
              "no scraping, documented public API only)")

PREREGISTRATION = {
    "hypothesis_h1": (
        "At least one candidate connector supplies items the engine cannot already "
        "reach: pooled over the probed interests, marginal_unique_rate >= 0.40 AND "
        "jaccard_overlap_with_web_search_sample < 0.30."
    ),
    "baseline": (
        "The existing web_search collector's sample over the same interests, same "
        "window, same per-interest item cap. For the zero-spend lane the baseline is "
        "the stored production corpus; if neither corpus nor a web_search sample is "
        "reachable, the metric is recorded as VOID, never as 0."
    ),
    "primary_metric": (
        "marginal_unique_rate = |sample_urls \\ (corpus_urls UNION "
        "web_search_sample_urls)| / |sample_urls|, per connector, pooled across "
        "probed interests, canonicalized URLs/titles."
    ),
    "secondary_metrics_non_decisive": [
        "count of sampled items at/above the interest's own min_score under the "
        "frozen production scorer (absolute count, never a share, below 8 "
        "observations -- LAB.md guardrail 7)",
        "median item age in days",
        "sample validity rate (fraction of records with both a URL and a title)",
    ],
    "stopping_condition": (
        "Exactly one pass per (connector, interest) pair at the caps below. Queries "
        "frozen in code before the run, derived mechanically from the committed "
        "interest definition. No query tuning, no re-runs, no connector added after "
        "seeing results."
    ),
    "falsification_condition": (
        "If every candidate connector has marginal_unique_rate < 0.40 or overlap >= "
        "0.30, H1 is FALSIFIED: no candidate connector adds retrieval surface, "
        "step-09 should not add a connector on retrieval grounds. A falsified H1 is "
        "a successful outcome of this step."
    ),
    "validity_voids": [
        "network unavailable", "connector endpoint unreachable or shape-changed",
        "production corpus (discovery.db) unavailable", "sample n < 5 for a connector",
        "provider unavailable for the scoring sub-metric",
    ],
    "spend_bound": {
        "paid_usd": 0.0,
        "youtube_quota_units": 0,
        "http_requests_max_total": MAX_REQUESTS,
        "http_requests_max_per_connector": PER_CONNECTOR_CAP,
        "http_records_max_per_query": 50,
        "http_timeout_seconds": 15,
        "http_min_gap_seconds": DEFAULT_MIN_GAP_SECONDS,
        "http_min_gap_seconds_arxiv": HOST_MIN_GAP_SECONDS["export.arxiv.org"],
        "provider_calls_zero_spend_lane": 0,
        "provider_calls_live_lane_max": 40,
        "deliberate_rate_limit_stress_testing": False,
    },
    "candidate_connectors": list(CONNECTORS),
    "probed_interests": list(PROBED_INTERESTS),
    "applicability_map": {k: list(v) for k, v in APPLICABILITY.items()},
}

# === E5b (step-09) PRE-REGISTRATION -- a SEPARATE, later-registered pass ===
# step-09a's H1/PREREGISTRATION above is FROZEN and untouched: its recorded
# results (connectors[], verdict, verdict_detail) stay byte-identical. This
# is a second, independently pre-registered question (H2), added because
# step-09a's own dossier showed the frozen query rule -- interest.title plus
# its first 3 positive_signals, concatenated into up to 300 chars of prose --
# was over-constrained for AND-matching engines (hackernews: 0 hits) and
# topically wrong for relevance-ranked ones (arxiv/pubmed returned mostly
# off-topic records). So step-09a measured the query rule, not the
# connectors; H2 asks the retrieval question again under a corrected,
# still-mechanical rule.
#
# HYPOTHESIS H2 (usable retrieval yield): under a corrected, still-mechanical
# query rule, at least one candidate connector delivers, pooled over its
# applicable probed interests and within the unchanged spend bound, >= 8
# USABLE records.
#
# USABLE, exactly: (a) the record has both a URL and a title; (b) built into
# a discovery.models.CandidateItem with origin_interest EXPLICITLY UNSET
# (None) and text = the record's title, discovery.matching.match_interests
# against the interest it was queried for scores >= cfg.min_match_score
# (production default 0.25, read from discovery.config.load()); (c) not a
# within-sample duplicate (pooled across a connector's applicable interests)
# by canonicalized URL or canonicalized title. origin_interest stays None so
# matching.ORIGIN_MATCH_FLOOR does not hand every record a free 0.5 pass
# (that would make the metric vacuous); matching.prefilter is NOT used
# because its min_text_chars branch would reject every title-only recon
# record regardless of relevance -- only the relevance component of the
# engine's own free gate applies. No new tokenizer, no new relevance
# heuristic: reuse discovery.matching (interest_state.py already reuses
# matching._tokens; same precedent).
#
# REVISED QUERY RULE (frozen, see build_query_v2): the first 4 distinctive
# tokens of interest.title, in title order, deduped, lowercased, joined by
# single spaces, where 'distinctive token' is exactly matching._tokens (4+
# chars, not a stopword). If the title yields fewer than 2 distinctive
# tokens, extend in order with distinctive tokens from positive_signals[0]
# until 2 are present. Identical for every connector -- no per-connector hand
# tuning beyond each endpoint's existing parameter encoding.
#
# BASELINE (zero new spend): the same connectors' step-09a records, already
# persisted in connector_evidence.json, with usable-yield recomputed under
# the identical USABLE definition (see usable_records, called on both arms).
#
# PRIMARY METRIC: usable_yield, an absolute count per connector pooled over
# its applicable interests. usable_rate is reported only when n_sampled >= 8
# (LAB.md guardrail 7 forbids share-based criteria below 8 observations).
#
# SECONDARY, EXPLICITLY NON-DECISIVE: median item age in days; sample
# validity rate; per-connector uniqueness against the union of the OTHER
# candidate connectors' usable URLs (uniqueness among candidates, NOT against
# the engine's existing surface); observed failure behavior and rate-limit
# headers; requests spent.
#
# MARGINAL UNIQUENESS vs the engine's existing surface: marginal_unique_rate
# is computed against the production corpus only when discovery.db is
# genuinely reachable (same db_replay.open_ro path as H1); when it is not, it
# is recorded VOID_NO_BASELINE, never 0. jaccard_overlap_with_web_search_
# sample stays VOID_NO_WEB_SEARCH_SAMPLE unless a live provider lane ran.
#
# STOPPING CONDITION: exactly one pass per (connector, interest) pair under
# the new rule. Spend bound is UNCHANGED from H1 (see PREREGISTRATION above):
# <=40 requests total, <=10/connector, one page/query, 15s timeout, per-host
# spacing, descriptive UA, no auth, no scraping, $0, 0 YouTube quota, 0
# provider calls. No query tuning, no re-runs, no connector added after
# seeing results. A pass that aborts is recorded in aborted_attempts, not
# silently re-run.
#
# CONNECTOR SCOPE: hackernews, arxiv, pubmed sampled under the new rule;
# reddit gets ONE availability re-check request (its 403s may have been
# transient) and is sampled further only if that re-check succeeds -- a
# second independent 403 retires it (RETIRED_UNREACHABLE). x is NOT sampled:
# it needs provider.search_json both to sample and to exist as a collector,
# no provider is reachable here -- recorded DEFERRED_NEEDS_PROVIDER.
#
# FALSIFICATION: if every candidate connector's usable_yield under the new
# rule is < 8, H2 is FALSIFIED -- a successful, properly measured outcome,
# not a shortfall. Do not soften, re-run, or hand-tune toward a pass.
#
# PROMOTION GATE (apply_promotion_gate; all three must hold, any VOID input
# fails rather than passes): G1 exactly one connector holds the max
# usable_yield and that max is >= 8; G2 that max is >= 2x the runner-up's
# (clear winner, not noise); G3 that connector's marginal_unique_rate against
# a REACHABLE baseline is >= 0.40 (VOID baseline fails G3 outright).
PREREGISTRATION_PASS2 = {
    "hypothesis_h2": (
        "Under a corrected, still-mechanical query rule, at least one candidate "
        "connector delivers, pooled over its applicable probed interests and within "
        "the unchanged spend bound, >= 8 USABLE records."
    ),
    "usable_definition": (
        "(a) has both a URL and a title; (b) built into a CandidateItem with "
        "origin_interest UNSET (None) and text=title, matching.match_interests "
        "against the interest it was queried for scores >= cfg.min_match_score "
        "(matching.prefilter NOT used -- its min_text_chars branch would reject every "
        "title-only record regardless of relevance); (c) not a within-sample "
        "(pooled per connector) duplicate by canonicalized URL or title."
    ),
    "query_rule": (
        "build_query_v2: first 4 distinctive tokens (matching._tokens: 4+ chars, not "
        "a stopword) of interest.title, in title order, deduped, lowercased, joined "
        "by spaces; extended from positive_signals[0] if the title yields < 2. Frozen "
        "before any run; identical for every connector."
    ),
    "baseline": (
        "step-09a's already-persisted records for the same connectors, with "
        "usable-yield recomputed under the identical USABLE definition -- zero new "
        "spend for this arm."
    ),
    "primary_metric": (
        "usable_yield: absolute count per connector, pooled over its applicable "
        "interests. usable_rate reported only when n_sampled >= 8 (LAB.md guardrail 7)."
    ),
    "secondary_metrics_non_decisive": [
        "median item age in days",
        "sample validity rate (fraction of records with both a URL and a title)",
        "per-connector uniqueness against the union of the OTHER candidate connectors' "
        "usable URLs (among candidates, NOT against the engine's existing surface)",
        "observed failure behavior and rate-limit headers",
        "requests spent",
    ],
    "marginal_uniqueness_vs_corpus": (
        "marginal_unique_rate computed against discovery.db only when reachable via "
        "db_replay.open_ro; VOID_NO_BASELINE (never 0) otherwise. "
        "jaccard_overlap_with_web_search_sample stays VOID_NO_WEB_SEARCH_SAMPLE unless "
        "a live provider lane actually ran."
    ),
    "stopping_condition": (
        "Exactly one pass per (connector, interest) pair under the new rule. Spend "
        "bound unchanged from H1. No query tuning, no re-runs, no connector added "
        "after seeing results. Aborted passes recorded in aborted_attempts, not "
        "silently re-run."
    ),
    "connector_scope": {
        "sampled_under_new_rule": ["hackernews", "arxiv", "pubmed"],
        "reddit": (
            "ONE availability re-check request only; sampled further only if it "
            "succeeds. A second independent 403 retires it (RETIRED_UNREACHABLE)."
        ),
        "x": (
            "NOT sampled -- needs provider.search_json both to sample and to exist as "
            "a collector, no provider reachable here; DEFERRED_NEEDS_PROVIDER."
        ),
    },
    "falsification_condition": (
        "If every candidate connector's usable_yield under the new rule is < 8, H2 is "
        "FALSIFIED. A falsified H2 is a successful outcome of this step, not a "
        "shortfall."
    ),
    "promotion_gate": (
        "G1: exactly one connector holds max usable_yield and that max >= 8. "
        "G2: that max >= 2x the runner-up's usable_yield (clear winner). "
        "G3: that connector's marginal_unique_rate against a REACHABLE baseline is "
        ">= 0.40 (VOID baseline fails G3 outright). All three must hold to PROMOTE."
    ),
    "spend_bound": PREREGISTRATION["spend_bound"],
}

PRIOR_EVIDENCE = [
    {
        "source": "experiments/x_prompt_lab/conclusions.md (untracked; not re-verifiable from this clone)",
        "as_of": "2026-08-08",
        "status": "prior, unreplicated in this step",
        "summary": (
            "Search-prompts-only X discovery via search_json (no scraping/API) judged "
            "VIABLE: 2 interests x 3 generations, 91/91 items valid status URLs, 0 "
            "hallucinated (15 ids independently re-found as a realness proof), "
            "judge-ranked main news. Freshness floor: D-1 broad topics, D-2 single "
            "ticker -> digest source, NOT alert."
        ),
        "bearing_on_h1": (
            "Suggestive that the x connector could clear the marginal-unique-rate bar "
            "on retrieval grounds, but this step could not re-test it -- the "
            "zero-spend lane makes 0 provider calls, and x's search_json needs one. "
            "Cited as evidence, not counted as a measurement made here."
        ),
    },
    {
        "source": "experiments/lab/design_council.py proposal ledger (PROJECT_STATE.md, engine lab section)",
        "as_of": "2026-08-10",
        "status": "prior, unreplicated in this step",
        "summary": (
            "001 EXECUTED then REVERTED by its own trigger (same-version scorer "
            "non-determinism, not prompt-version drift). 002 EXECUTED then mothballed "
            "indefinitely by owner decision (\"proceed label-free\"). 003 EXECUTED "
            "then REVERTED on a 0.0003 conjunctive miss. 004 VALIDATED (drift closed "
            "for the pinned same-hash/same-model condition). 005 EXECUTED, running "
            "detached as Scheduled Task engine-lab-005 (E2 discovery-yield A/B on the "
            "three starved interests + nbis control, retrieval vs scoring failure)."
        ),
        "bearing_on_h1": (
            "005 asks whether the three starved interests are collection-limited or "
            "scorer-limited using the *existing* web_search collector under strategist "
            "angles -- a sibling question, not a substitute for this step's "
            "connector-level marginal-retrieval-surface question. Its result (still "
            "in flight) will bear on how to read a H1_FALSIFIED outcome here: if 005 "
            "shows the starved interests are scorer-limited, that further weakens the "
            "case for adding a new connector on retrieval grounds alone."
        ),
    },
]


def utf8_stdout():
    utf8_streams()


# --- network: the ONE entry point for all HTTP I/O -----------------------

_request_count = 0
_last_request_at = {}
_connector_request_count = {}


def _reset_http_state():
    """Test seam: reset module-level counters between test cases."""
    global _request_count, _last_request_at, _connector_request_count
    _request_count = 0
    _last_request_at = {}
    _connector_request_count = {}


class ConnectorUnreachable(Exception):
    """The endpoint could not be reached at all (DNS/connect/timeout)."""


def _http_get(url, connector=None, timeout=15):
    """The harness's ONE network entry point: enforces the global
    request-count cap AND (when `connector` is given) the pre-registered
    per-connector cap at runtime -- not just via the import-time
    `_static_budget_check()` sanity check, which would otherwise silently
    drift from reality if a sampler grew a retry or a paged fetch. Also
    enforces per-host spacing (arxiv gets its own 3s etiquette gap), a
    descriptive User-Agent, and the timeout. Returns (status, body_bytes,
    headers_dict). Tests monkeypatch this whole function to exercise the
    connector-shaping and analysis code without touching the network; the
    counter/spacing logic itself is tested by patching
    urllib.request.urlopen underneath it.
    """
    global _request_count
    host = urllib.parse.urlparse(url).netloc
    if _request_count >= MAX_REQUESTS:
        raise RuntimeError(f"request cap {MAX_REQUESTS} reached; refusing {url}")
    if connector is not None and _connector_request_count.get(connector, 0) >= PER_CONNECTOR_CAP:
        raise RuntimeError(f"per-connector cap {PER_CONNECTOR_CAP} reached for "
                           f"'{connector}'; refusing {url}")
    min_gap = HOST_MIN_GAP_SECONDS.get(host, DEFAULT_MIN_GAP_SECONDS)
    last = _last_request_at.get(host)
    if last is not None:
        wait = min_gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            headers = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        body = e.read()
        status = e.code
        headers = dict(e.headers.items()) if e.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _request_count += 1
        _last_request_at[host] = time.monotonic()
        if connector is not None:
            _connector_request_count[connector] = _connector_request_count.get(connector, 0) + 1
        raise ConnectorUnreachable(f"{host}: {e}") from e
    _request_count += 1
    _last_request_at[host] = time.monotonic()
    if connector is not None:
        _connector_request_count[connector] = _connector_request_count.get(connector, 0) + 1
    return status, body, headers


def _rate_limit_headers(headers):
    return {k: v for k, v in headers.items()
            if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"}


# --- pure analysis helpers (no network, no clock reads outside `now_dt`) --

def canon_url(url):
    return re.sub(r"[#?].*$", "", str(url or "").lower()).rstrip("/")


def canon_title(title):
    return re.sub(r"[^a-z0-9]+", "", str(title or "").lower())


def dedup_urls(records):
    return {canon_url(r["url"]) for r in records if r.get("url")}


def jaccard_overlap(a, b):
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def marginal_unique_rate(sample_urls, baseline_urls):
    """None (VOID -- caller decides whether to even call this) if the sample
    is empty; otherwise the fraction of sample_urls absent from baseline_urls."""
    if not sample_urls:
        return None
    return len(sample_urls - baseline_urls) / len(sample_urls)


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return round(values[idx], 4)


def validity_rate(records):
    if not records:
        return None
    valid = sum(1 for r in records if r.get("url") and r.get("title"))
    return round(valid / len(records), 3)


def _parse_dt(value):
    """Best-effort provenance-timestamp parse; None on failure (never guessed).
    Handles ISO-8601 (hackernews/reddit/arxiv) and NCBI's looser pubdate
    styles ("2023 Jan 15", "2023 Jan-Feb", "2023")."""
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y %b %d", "%Y %b", "%Y"):
        try:
            dt = datetime.strptime(text.split("-")[0].strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def median_age_days(records, now_dt):
    ages = [(now_dt - dt).total_seconds() / 86400
            for dt in (_parse_dt(r.get("published_at")) for r in records) if dt]
    if not ages:
        return None
    return round(statistics.median(ages), 2)


def apply_falsification_rule(connector_metrics, baseline_available):
    """connector_metrics: {name: {"marginal_unique_rate": float|None,
    "jaccard_overlap": float|None}}. Pure mechanical application of the
    pre-registered falsification rule.

    - baseline_available False (corpus+web_search_sample not both assemblable)
      -> VOID_NO_BASELINE, regardless of what individual connectors measured.
    - baseline_available True but no connector has both metrics computed
      -> VOID_NO_MEASURABLE_CONNECTORS.
    - baseline_available True, at least one connector measurable, none clears
      the H1 threshold -> H1_FALSIFIED (a real, decisive result -- not a void).
    - any connector clears marginal_unique_rate >= 0.40 AND overlap < 0.30
      -> H1_SUPPORTED.
    """
    if not baseline_available:
        return "VOID_NO_BASELINE"
    measurable = {
        name: m for name, m in connector_metrics.items()
        if m.get("marginal_unique_rate") is not None and m.get("jaccard_overlap") is not None
    }
    if not measurable:
        return "VOID_NO_MEASURABLE_CONNECTORS"
    for m in measurable.values():
        if m["marginal_unique_rate"] >= 0.40 and m["jaccard_overlap"] < 0.30:
            return "H1_SUPPORTED"
    return "H1_FALSIFIED"


def apply_h2_falsification_rule(usable_yields):
    """usable_yields: {connector_name: int}. Pure mechanical application of
    the H2 falsification rule: SUPPORTED if any connector cleared >= 8
    USABLE records under the new rule, else FALSIFIED (a real, decisive
    result -- not a void; H2 has no baseline dependency, unlike H1)."""
    if any(n >= 8 for n in usable_yields.values()):
        return "H2_SUPPORTED"
    return "H2_FALSIFIED"


def apply_promotion_gate(pass2_metrics, baseline_available):
    """Pure, mechanical application of the pre-registered promotion gate.

    pass2_metrics: {connector: {"usable_yield": int,
    "marginal_unique_rate": float|None}}. All three gates must hold; any VOID
    input fails the gate rather than passing it.

    G1: exactly one connector holds the max usable_yield, and that max >= 8.
    G2: that max is >= 2x the runner-up's usable_yield (a clear winner, not a
        tie inside noise).
    G3: the G1 winner's marginal_unique_rate against a REACHABLE baseline is
        >= 0.40. baseline_available=False, or a None/low rate, fails G3.

    Returns {"result": "PROMOTE"|"NO_PROMOTION", "winner": str|None,
             "failing_gate": "G1"|"G2"|"G3"|None, "detail": str}.
    """
    if not pass2_metrics:
        return {"result": "NO_PROMOTION", "winner": None, "failing_gate": "G1",
                "detail": "no connectors measured this pass"}

    ranked = sorted(pass2_metrics.items(), key=lambda kv: kv[1]["usable_yield"], reverse=True)
    top_name, top = ranked[0]
    tied = [n for n, m in pass2_metrics.items() if m["usable_yield"] == top["usable_yield"]]
    if len(tied) != 1 or top["usable_yield"] < 8:
        return {"result": "NO_PROMOTION", "winner": None, "failing_gate": "G1",
                "detail": f"max usable_yield={top['usable_yield']} held by {tied}"}

    runner_up = ranked[1][1]["usable_yield"] if len(ranked) > 1 else 0
    if top["usable_yield"] < 2 * runner_up:
        return {"result": "NO_PROMOTION", "winner": None, "failing_gate": "G2",
                "detail": f"{top_name} usable_yield={top['usable_yield']} not >= 2x "
                          f"runner-up={runner_up}"}

    mur = top.get("marginal_unique_rate")
    if not baseline_available or mur is None or mur < 0.40:
        return {"result": "NO_PROMOTION", "winner": None, "failing_gate": "G3",
                "detail": f"{top_name} marginal_unique_rate={mur} "
                          f"baseline_available={baseline_available}"}

    return {"result": "PROMOTE", "winner": top_name, "failing_gate": None,
            "detail": f"{top_name} usable_yield={top['usable_yield']} clears G1/G2/G3 "
                      f"(marginal_unique_rate={mur})"}


def build_query(interest):
    parts = [interest.title, *interest.positive_signals[:3]]
    text = " ".join(p for p in parts if p)
    return text[:QUERY_MAX_CHARS]


def _ordered_distinctive_tokens(text):
    """Same distinctiveness rule as matching._tokens (4+ chars, not a
    stopword) -- but order-preserving and de-duplicating, which the frozen
    "first N tokens in title order" query rule needs and the set that
    matching._tokens returns can't give us."""
    seen, out = set(), []
    for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()):
        if w in matching.STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def build_query_v2(interest):
    """H2's frozen revised query rule (pre-registered above): first 4
    distinctive tokens of the title, in title order, deduped, extended from
    positive_signals[0] if the title alone yields fewer than 2. Identical for
    every connector -- derived mechanically from the committed interest, must
    not be adjusted after any result is seen."""
    tokens = _ordered_distinctive_tokens(interest.title)
    if len(tokens) < 2 and interest.positive_signals:
        for w in _ordered_distinctive_tokens(interest.positive_signals[0]):
            if w not in tokens:
                tokens.append(w)
            if len(tokens) >= 2:
                break
    return " ".join(tokens[:4])


def build_probe_item(record):
    """The CandidateItem used only to test USABLE-ness (H2). origin_interest
    is deliberately left unset (None): setting it would hand every record a
    free matching.ORIGIN_MATCH_FLOOR (0.5) pass, making the metric vacuous.
    text = title only -- these recon records carry no body."""
    return CandidateItem(source="connector_probe_pass2", type="article",
                          title=record.get("title") or "", url=record.get("url") or "",
                          text=record.get("title") or "")


def usable_records(per_interest, interests_by_key, cfg):
    """USABLE records per the H2 pre-registration, pooled across a
    connector's applicable interests (within-sample dedup is pooled, not
    per-interest -- see PRIMARY METRIC). Reused for BOTH arms: the freshly
    sampled new-rule records and step-09a's already-persisted old-rule
    records (same shape: {"interest": key, "records": [...]}), which is what
    makes the baseline arm's "identical USABLE definition" requirement exact
    rather than reimplemented. matching.prefilter is deliberately NOT used --
    see build_probe_item / the H2 pre-registration comment above."""
    seen_urls, seen_titles, out = set(), set(), []
    for pi in per_interest:
        interest = interests_by_key.get(pi["interest"])
        if interest is None:
            continue
        for r in pi["records"]:
            url, title = r.get("url"), r.get("title")
            if not url or not title:
                continue
            cu, ct = canon_url(url), canon_title(title)
            if cu in seen_urls or ct in seen_titles:
                continue
            matches = matching.match_interests(build_probe_item(r), [interest])
            if not matches or matches[0][1] < cfg.min_match_score:
                continue
            seen_urls.add(cu)
            seen_titles.add(ct)
            out.append({**r, "interest": pi["interest"], "match_score": matches[0][1]})
    return out


# --- connector endpoint builders + parsers (pure given bytes) -------------

def hn_url(query, n=N_PER_QUERY):
    qs = urllib.parse.urlencode({"query": query, "tags": "story", "hitsPerPage": n})
    return f"https://hn.algolia.com/api/v1/search_by_date?{qs}"


def parse_hn(body):
    data = json.loads(body)
    out = []
    for hit in data.get("hits", []):
        url = hit.get("url") or (
            f"https://news.ycombinator.com/item?id={hit['objectID']}" if hit.get("objectID") else None
        )
        out.append({"url": url, "title": hit.get("title") or hit.get("story_title"),
                    "published_at": hit.get("created_at")})
    return out


def reddit_url(query, n=N_PER_QUERY):
    qs = urllib.parse.urlencode({"q": query, "sort": "new", "limit": n, "t": "year"})
    return f"https://www.reddit.com/search.json?{qs}"


def parse_reddit(body):
    data = json.loads(body)
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        permalink = d.get("permalink")
        url = f"https://www.reddit.com{permalink}" if permalink else d.get("url")
        created = d.get("created_utc")
        published = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="seconds")
                     if created else None)
        out.append({"url": url, "title": d.get("title"), "published_at": published})
    return out


def arxiv_url(query, n=N_PER_QUERY):
    qs = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0, "max_results": n})
    return f"https://export.arxiv.org/api/query?{qs}"


def parse_arxiv(body):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(body)
    out = []
    for entry in root.findall("a:entry", ns):
        title_el = entry.find("a:title", ns)
        id_el = entry.find("a:id", ns)
        pub_el = entry.find("a:published", ns)
        out.append({
            "url": id_el.text.strip() if id_el is not None and id_el.text else None,
            "title": title_el.text.strip() if title_el is not None and title_el.text else None,
            "published_at": pub_el.text if pub_el is not None else None,
        })
    return out


def pubmed_esearch_url(query, n=N_PER_QUERY):
    qs = urllib.parse.urlencode({"db": "pubmed", "term": query, "retmode": "json", "retmax": n})
    return f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{qs}"


def pubmed_esummary_url(ids):
    qs = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    return f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?{qs}"


def parse_pubmed_ids(body):
    return json.loads(body).get("esearchresult", {}).get("idlist", [])


def parse_pubmed_summaries(body):
    result = json.loads(body).get("result", {})
    out = []
    for uid in result.get("uids", []):
        rec = result.get(uid, {})
        out.append({"url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                    "title": rec.get("title"), "published_at": rec.get("pubdate")})
    return out


def _sample(url, parser, connector=None):
    """One bounded fetch-and-parse through _http_get. Never raises: any
    failure (network, HTTP status, parse) lands in entry['error']."""
    entry = {"records": [], "http_status": None, "endpoint": url, "error": None,
             "rate_limit_headers": {}}
    try:
        status, body, headers = _http_get(url, connector=connector)
    except Exception as e:
        entry["error"] = str(e)
        return entry
    entry["http_status"] = status
    entry["rate_limit_headers"] = _rate_limit_headers(headers)
    if status != 200:
        entry["error"] = f"http {status}"
        return entry
    try:
        entry["records"] = parser(body)
    except Exception as e:
        entry["error"] = f"parse error: {e}"
    return entry


def sample_pubmed(query, n=N_PER_QUERY):
    esearch_url = pubmed_esearch_url(query, n)
    entry = {"records": [], "http_status": None, "endpoint": esearch_url, "error": None,
             "rate_limit_headers": {}}
    try:
        status, body, headers = _http_get(esearch_url, connector="pubmed")
    except Exception as e:
        entry["error"] = f"esearch failed: {e}"
        return entry
    entry["http_status"] = status
    entry["rate_limit_headers"] = _rate_limit_headers(headers)
    if status != 200:
        entry["error"] = f"esearch http {status}"
        return entry
    try:
        ids = parse_pubmed_ids(body)
    except Exception as e:
        entry["error"] = f"esearch parse error: {e}"
        return entry
    if not ids:
        return entry   # zero hits is a legitimate empty result, not a failure
    esummary_url = pubmed_esummary_url(ids)
    entry["endpoint"] = f"{esearch_url} -> {esummary_url}"
    try:
        status2, body2, headers2 = _http_get(esummary_url, connector="pubmed")
    except Exception as e:
        entry["error"] = f"esummary failed: {e}"
        return entry
    entry["http_status"] = status2
    entry["rate_limit_headers"].update(_rate_limit_headers(headers2))
    if status2 != 200:
        entry["error"] = f"esummary http {status2}"
        return entry
    try:
        entry["records"] = parse_pubmed_summaries(body2)
    except Exception as e:
        entry["error"] = f"esummary parse error: {e}"
    return entry


CONNECTOR_SAMPLERS = {
    "hackernews": lambda q, n=N_PER_QUERY: _sample(hn_url(q, n), parse_hn, connector="hackernews"),
    "reddit": lambda q, n=N_PER_QUERY: _sample(reddit_url(q, n), parse_reddit, connector="reddit"),
    "arxiv": lambda q, n=N_PER_QUERY: _sample(arxiv_url(q, n), parse_arxiv, connector="arxiv"),
    "pubmed": sample_pubmed,
}


def _static_budget_check():
    """Import-time sanity check on the pre-registered request caps against
    APPLICABILITY -- fails loudly before any request is attempted rather than
    silently overspending. The actual guarantee is the runtime per-connector
    counter in `_http_get` (this is a cheap early warning, not the enforcement
    point)."""
    per_query_requests = {"hackernews": 1, "reddit": 1, "arxiv": 1, "pubmed": 2}
    total = 0
    for name, per_query in per_query_requests.items():
        n = len(APPLICABILITY[name]) * per_query
        if n > PER_CONNECTOR_CAP:
            raise AssertionError(f"{name} would need {n} requests > cap {PER_CONNECTOR_CAP}")
        total += n
    if total > MAX_REQUESTS:
        raise AssertionError(f"total {total} requests > cap {MAX_REQUESTS}")


_static_budget_check()


def _static_budget_check_pass2():
    """Same early-warning check as _static_budget_check, for the H2 pass:
    worst case is reddit's re-check succeeding and every applicable interest
    getting sampled (len(APPLICABILITY['reddit']) requests, same as its
    step-09a request count) -- so the same per_query_requests shape and the
    same total bound apply. Kept separate from _static_budget_check because
    the two passes are independently pre-registered and must not share a
    single check that could silently drift when only one changes."""
    per_query_requests = {"hackernews": 1, "reddit": 1, "arxiv": 1, "pubmed": 2}
    total = 0
    for name, per_query in per_query_requests.items():
        n = len(APPLICABILITY[name]) * per_query
        if n > PER_CONNECTOR_CAP:
            raise AssertionError(f"pass2 {name} would need {n} requests > cap {PER_CONNECTOR_CAP}")
        total += n
    if total > MAX_REQUESTS:
        raise AssertionError(f"pass2 total {total} requests > cap {MAX_REQUESTS}")


_static_budget_check_pass2()


# --- H2 (pass_2_e5b) sampling: reuses CONNECTOR_SAMPLERS, new query rule --

def sample_connector_pass2(name, applicable, interests_by_key):
    """One bounded fetch per (connector, applicable interest) under the
    revised query rule (build_query_v2). No baseline/above-bar logic here --
    unlike H1's build_http_connector_entry, H2's metrics (usable_records,
    apply_promotion_gate) are computed separately from the raw sample."""
    per_interest = []
    for key in applicable:
        interest = interests_by_key[key]
        query = build_query_v2(interest)
        result = CONNECTOR_SAMPLERS[name](query, N_PER_QUERY)
        per_interest.append({
            "interest": key, "query": query, "endpoint": result["endpoint"],
            "http_status": result["http_status"], "error": result["error"],
            "records": result["records"], "n_records": len(result["records"]),
            "rate_limit_headers": result.get("rate_limit_headers") or {},
            "collected_at": now(), "lane": "pass_2_e5b",
        })
    return per_interest


def sample_reddit_pass2(applicable, interests_by_key):
    """CONNECTOR SCOPE: one availability re-check request only (its own
    first applicable interest's query_v2), sampled further ONLY if that
    re-check succeeds. A second independent 403 (it 403'd on all 5 interests
    in step-09a) is a retirement signal, not retried."""
    if not applicable:
        return [], {"reachable": None, "detail": "no applicable probed interest"}

    first_key = applicable[0]
    query = build_query_v2(interests_by_key[first_key])
    result = CONNECTOR_SAMPLERS["reddit"](query, N_PER_QUERY)
    recheck = {
        "interest": first_key, "query": query, "endpoint": result["endpoint"],
        "http_status": result["http_status"], "error": result["error"],
        "records": result["records"], "n_records": len(result["records"]),
        "rate_limit_headers": result.get("rate_limit_headers") or {},
        "collected_at": now(), "lane": "pass_2_e5b",
    }
    if result["error"] is not None:
        return [recheck], {"reachable": False,
                           "detail": f"availability re-check failed: {result['error']}"}

    per_interest = [recheck]
    for key in applicable[1:]:
        query = build_query_v2(interests_by_key[key])
        r = CONNECTOR_SAMPLERS["reddit"](query, N_PER_QUERY)
        per_interest.append({
            "interest": key, "query": query, "endpoint": r["endpoint"],
            "http_status": r["http_status"], "error": r["error"],
            "records": r["records"], "n_records": len(r["records"]),
            "rate_limit_headers": r.get("rate_limit_headers") or {},
            "collected_at": now(), "lane": "pass_2_e5b",
        })
    return per_interest, {"reachable": True, "detail": "availability re-check succeeded (http 200)"}


def x_deferred_entry():
    """x is DEFERRED_NEEDS_PROVIDER for H2, not sampled: it needs
    provider.search_json both to sample and to exist as a collector, and no
    provider is reachable from this harness. Its only evidence stays the
    unreplicated experiments/x_prompt_lab prior (see PRIOR_EVIDENCE)."""
    return {
        "name": "x",
        "status": "DEFERRED_NEEDS_PROVIDER",
        "detail": (
            "Not sampled per CONNECTOR SCOPE: needs provider.search_json both to "
            "sample and to exist as a collector; no provider is reachable from this "
            "harness. Blocking work: implement an x sampler (provider.search_json "
            "against build_query_v2) and a web_search baseline sampler, then re-run "
            "from a live claude.ai Chrome/CDP operator session."
        ),
    }


def uniqueness_among_candidates(name, usable_urls_by_connector):
    """Fraction of `name`'s usable URLs absent from the union of every OTHER
    candidate connector's usable URLs this pass. Non-decisive: measures
    uniqueness AMONG CANDIDATES, not against the engine's existing surface
    (that's marginal_unique_rate, against the corpus)."""
    own = usable_urls_by_connector.get(name) or set()
    if not own:
        return None
    others = [u for n, u in usable_urls_by_connector.items() if n != name]
    other_union = set().union(*others) if others else set()
    return round(len(own - other_union) / len(own), 3)


def build_connector_pass2_entry(name, applicable, interests_by_key, cfg, old_per_interest,
                                corpus_urls, corpus_available, now_dt, requests_before):
    """Samples `name` under the new rule (or reddit's recheck-gated flow),
    then computes both arms' usable_yield plus the non-decisive secondary
    metrics. `old_per_interest` is step-09a's already-persisted per_interest
    list for this connector (same shape), used for the zero-new-spend
    baseline arm."""
    availability = None
    if name == "reddit":
        per_interest, availability = sample_reddit_pass2(applicable, interests_by_key)
    else:
        per_interest = sample_connector_pass2(name, applicable, interests_by_key)

    all_records = [r for pi in per_interest for r in pi["records"]]
    successes = [pi for pi in per_interest if pi["error"] is None]
    sample_urls = dedup_urls(all_records)

    if corpus_available:
        mur = marginal_unique_rate(sample_urls, corpus_urls)
        mur_status = "OK_OFFLINE_LANE_CORPUS_ONLY_BASELINE" if mur is not None else "VOID_EMPTY_SAMPLE"
    else:
        mur, mur_status = None, "VOID_NO_BASELINE"

    new_usable = usable_records(per_interest, interests_by_key, cfg)
    old_usable = usable_records(old_per_interest, interests_by_key, cfg)

    arm_new_rule = {
        "queries": {pi["interest"]: pi["query"] for pi in per_interest},
        "per_interest": per_interest,
        "n_sampled": len(all_records),
        "n_unique_urls": len(sample_urls),
        "usable_yield": len(new_usable),
        "usable_rate": (round(len(new_usable) / len(all_records), 3)
                        if len(all_records) >= 8 else None),
        "usable_records": new_usable,
        "marginal_unique_rate": mur,
        "marginal_unique_rate_status": mur_status,
        "median_age_days": median_age_days(all_records, now_dt),
        "sample_validity_rate": validity_rate(all_records),
        "requests_spent": _connector_request_count.get(name, 0) - requests_before,
        "failure_behavior": ("; ".join(f"{pi['interest']}: {pi['error']}"
                                       for pi in per_interest if pi["error"])
                             or "no failures observed in this pass"),
    }
    if availability is not None:
        arm_new_rule["availability"] = availability

    old_all_records = [r for pi in old_per_interest for r in pi["records"]]
    arm_old_rule_recomputed = {
        "n_sampled": len(old_all_records),
        "usable_yield": len(old_usable),
        "usable_rate": (round(len(old_usable) / len(old_all_records), 3)
                        if len(old_all_records) >= 8 else None),
        "usable_records": old_usable,
    }

    return {
        "name": name,
        "applicable_interests": list(applicable),
        "reachable": bool(successes) if per_interest else None,
        "arm_new_rule": arm_new_rule,
        "arm_old_rule_recomputed": arm_old_rule_recomputed,
    }, dedup_urls(new_usable)


def run_pass2_e5b(dossier, cfg):
    """Runs the H2 pre-registered pass (see PREREGISTRATION_PASS2 above) and
    returns the pass_2_e5b dict to be merged into the loaded dossier. Never
    touches `dossier`'s existing step-09a keys -- the caller is responsible
    for only adding this under a new top-level key."""
    now_dt = datetime.now(timezone.utc)
    interests_by_key = {i.key: i for i in load_interests_file(cfg.interests_path)}
    old_by_connector = {c["name"]: c["sample"]["per_interest"] for c in dossier["connectors"]}
    corpus_urls, corpus_status = load_corpus_urls(cfg.db_path)
    corpus_urls = corpus_urls or set()

    sampled_connectors, aborted_attempts = [], []
    usable_urls_by_connector = {}
    for name in ("hackernews", "arxiv", "pubmed", "reddit"):
        applicable = [k for k in APPLICABILITY[name] if k in interests_by_key]
        requests_before = _connector_request_count.get(name, 0)
        try:
            entry, usable_urls = build_connector_pass2_entry(
                name, applicable, interests_by_key, cfg, old_by_connector.get(name, []),
                corpus_urls, corpus_status["available"], now_dt, requests_before)
        except Exception as e:
            aborted_attempts.append({"connector": name, "reason": str(e), "at": now()})
            continue
        sampled_connectors.append(entry)
        usable_urls_by_connector[name] = usable_urls

    for entry in sampled_connectors:
        entry["arm_new_rule"]["uniqueness_among_candidates"] = uniqueness_among_candidates(
            entry["name"], usable_urls_by_connector)

    gate_metrics = {
        e["name"]: {"usable_yield": e["arm_new_rule"]["usable_yield"],
                    "marginal_unique_rate": e["arm_new_rule"]["marginal_unique_rate"]}
        for e in sampled_connectors
    }
    gate = apply_promotion_gate(gate_metrics, corpus_status["available"])
    verdict = apply_h2_falsification_rule(
        {name: m["usable_yield"] for name, m in gate_metrics.items()})

    # Per-connector disposition for the dossier + PROJECT_STATE.md: the
    # binding constraint in THIS worktree is the absent corpus (G3), not low
    # yield -- so every sampled connector reads NOT_PROMOTED_VOID_BASELINE
    # unless it's the gate's own winner, even where usable_yield alone would
    # have cleared H2.
    dispositions = {"x": "DEFERRED_NEEDS_PROVIDER"}
    for e in sampled_connectors:
        name = e["name"]
        if gate["result"] == "PROMOTE" and gate["winner"] == name:
            dispositions[name] = "PROMOTED"
        elif name == "reddit" and e["reachable"] is False:
            dispositions[name] = "RETIRED_UNREACHABLE"
        elif not corpus_status["available"]:
            dispositions[name] = "NOT_PROMOTED_VOID_BASELINE"
        else:
            dispositions[name] = "NOT_PROMOTED_LOW_YIELD"

    return {
        "schema_version": 1,
        "evidence_cutoff": now(),
        "git_commit": git_head_sha(),
        "preregistration": PREREGISTRATION_PASS2,
        "spend_actual": {
            "http_requests": _request_count,
            "provider_calls": 0,
            "paid_usd": 0.0,
            "youtube_quota_units": 0,
        },
        "corpus": {**corpus_status, "path": cfg.db_path},
        "connectors": sampled_connectors,
        "x": x_deferred_entry(),
        "aborted_attempts": aborted_attempts,
        "gate": gate,
        "dispositions": dispositions,
        "verdict": verdict,
        "verdict_detail": (
            f"H2 gate: {gate['detail']}. " +
            (f"H2 {verdict.split('_')[1].lower()}: "
             f"{ {n: m['usable_yield'] for n, m in gate_metrics.items()} }.")
        ),
    }


# --- discovery.db (read-only corpus baseline) -----------------------------

def load_corpus_urls(db_path):
    """(urls_set_or_None, status_dict). status_dict never fabricates a
    number -- 'available' is False with a named reason on any failure."""
    try:
        conn = db_replay.open_ro(db_path)
    except sqlite3.OperationalError as e:
        return None, {"available": False, "reason": f"discovery.db not reachable at {db_path}: {e}"}
    try:
        rows = conn.execute("SELECT url FROM candidate_items").fetchall()
    except sqlite3.OperationalError as e:
        return None, {"available": False, "reason": f"candidate_items table unreadable: {e}"}
    finally:
        conn.close()
    urls = {canon_url(r["url"]) for r in rows if r["url"]}
    return urls, {"available": True, "reason": None, "n_rows": len(rows)}


# --- live-lane optional sub-metric (prod_scorer reuse; never invoked here) -

def score_records_above_bar(provider, lab, per_interest_entries, interests_by_key):
    """Scores every sampled record once with the frozen production scorer
    against the interest it was sampled for; counts final_score >= that
    interest's min_score. Costs len(records) provider calls against the
    Lab's budget cap. Only ever called after provider.preflight() succeeded."""
    total_above = total_scored = 0
    for pi in per_interest_entries:
        interest = interests_by_key[pi["interest"]]
        above = scored = 0
        for r in pi["records"]:
            item = CandidateItem(source="connector_probe", type="article",
                                  title=r.get("title") or "", url=r.get("url") or "",
                                  text=r.get("title") or "", published_at=r.get("published_at"))
            result = lab.call("score", lambda i=item, itr=interest: prod_scorer.score_item(
                provider, i, itr, match_score=0.5), interest=pi["interest"])
            if result is None:
                continue
            scored += 1
            if result.final_score >= interest.min_score:
                above += 1
        pi["above_bar_count"], pi["scored_n"] = above, scored
        total_above += above
        total_scored += scored
    return total_above, total_scored


# --- dossier assembly ------------------------------------------------------

def git_head_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                             text=True, timeout=10, check=True)
        return out.stdout.strip()
    except Exception:
        return None


def build_http_connector_entry(name, applicable, interests_by_key, corpus_urls, corpus_available,
                               now_dt, git_commit, provider, provider_ok, lab):
    per_interest = []
    for key in applicable:
        interest = interests_by_key[key]
        query = build_query(interest)
        result = CONNECTOR_SAMPLERS[name](query, N_PER_QUERY)
        per_interest.append({
            "interest": key, "query": query, "endpoint": result["endpoint"],
            "http_status": result["http_status"], "error": result["error"],
            "records": result["records"], "n_records": len(result["records"]),
            "rate_limit_headers": result.get("rate_limit_headers") or {},
            "collected_at": now(), "lane": "zero_spend", "git_commit": git_commit,
        })

    all_records = [r for pi in per_interest for r in pi["records"]]
    successes = [pi for pi in per_interest if pi["error"] is None]
    if not per_interest:
        sample_status_, sample_detail = "VOID_NOT_APPLICABLE", "no probed interest maps to this connector"
    elif not successes:
        sample_status_ = "VOID_UNREACHABLE"
        sample_detail = "; ".join(f"{pi['interest']}: {pi['error']}" for pi in per_interest)
    elif len(all_records) < 5:
        sample_status_ = "VOID_LOW_N"
        sample_detail = f"n={len(all_records)} < 5 across {len(applicable)} probed interests"
    else:
        sample_status_, sample_detail = "OK", None

    sample_urls = dedup_urls(all_records)

    # marginal_unique_rate: the offline-lane substitute baseline is the
    # corpus alone (pre-registration). If the sample itself is void (VALIDITY
    # VOIDS explicitly lists "sample n < 5 for a connector"), that void
    # reason wins over the baseline question. jaccard_overlap always stays
    # void here -- it needs a web_search sample, which this harness does not
    # yet implement (see x_deferred_entry / lanes.live.blocking_work).
    if sample_status_ != "OK":
        mur, mur_status = None, sample_status_
    elif not corpus_available:
        mur, mur_status = None, "VOID_NO_BASELINE"
    else:
        mur = marginal_unique_rate(sample_urls, corpus_urls)
        mur_status = "OK_OFFLINE_LANE_CORPUS_ONLY_BASELINE" if mur is not None else "VOID_EMPTY_SAMPLE"

    # Above-bar sub-metric only spends provider calls when the pass could
    # also produce a real marginal_unique_rate -- otherwise it is budget
    # spent on a non-decisive number attached to an unusable connector-level
    # result this pass (reviewer finding).
    above_bar_count = above_bar_n = None
    if provider_ok and corpus_available:
        above_bar_count, above_bar_n = score_records_above_bar(provider, lab, per_interest, interests_by_key)
        above_bar_status = "OK"
    elif not provider_ok:
        above_bar_status = "VOID_NO_PROVIDER"
    else:
        above_bar_status = "VOID_NO_BASELINE"

    metrics = {
        "marginal_unique_rate": mur,
        "marginal_unique_rate_status": mur_status,
        "jaccard_overlap_with_web_search_sample": None,
        "jaccard_overlap_status": "VOID_NO_WEB_SEARCH_SAMPLE",
        "sample_validity_rate": validity_rate(all_records),
        "median_age_days": median_age_days(all_records, now_dt),
        "above_bar_count": above_bar_count,
        "above_bar_n_scored": above_bar_n,
        "above_bar_status": above_bar_status,
        "n_sampled": len(all_records),
        "n_unique_urls": len(sample_urls),
    }

    failures = [f"{pi['interest']}: {pi['error']}" for pi in per_interest if pi["error"]]
    rl = {pi["interest"]: pi["rate_limit_headers"] for pi in per_interest if pi["rate_limit_headers"]}
    failure_behavior = "; ".join(failures) if failures else "no failures observed in this pass"
    if rl:
        failure_behavior += f" | rate-limit headers observed: {rl}"

    # availability.detail is reachability only (did the endpoint answer at
    # all) -- distinct from sample.detail, which is about sample size/shape.
    # Conflating the two mislabeled a clean HTTP 200 with 0 hits as an
    # "unreachable-looking" availability detail (reviewer finding).
    if successes:
        availability_detail = f"reachable (http {successes[0]['http_status']})"
    elif per_interest:
        availability_detail = "; ".join(f"{pi['interest']}: {pi['error']}" for pi in per_interest)
    else:
        availability_detail = "no probed interest maps to this connector"

    return {
        "name": name,
        "applicable_interests": list(applicable),
        "availability": {
            "reachable": bool(successes),
            "http_status": next((pi["http_status"] for pi in per_interest if pi["http_status"] is not None), None),
            "endpoint": per_interest[0]["endpoint"] if per_interest else None,
            "checked_at": per_interest[0]["collected_at"] if per_interest else now(),
            "detail": availability_detail,
        },
        "sample": {"status": sample_status_, "detail": sample_detail, "per_interest": per_interest},
        "metrics": metrics,
        "failure_behavior": failure_behavior,
    }


def run_probe(cfg):
    interests_by_key = {i.key: i for i in load_interests_file(cfg.interests_path)}
    provider = get_provider(cfg)
    provider_ok, provider_why = provider.preflight()
    print(f"x: provider.preflight() = {provider_ok} ({provider_why or 'reachable'})")
    for name in ("hackernews", "reddit", "arxiv", "pubmed"):
        applicable = [k for k in APPLICABILITY[name] if k in interests_by_key]
        if not applicable:
            print(f"{name}: no applicable probed interest")
            continue
        query = build_query(interests_by_key[applicable[0]])
        result = CONNECTOR_SAMPLERS[name](query, 1)
        print(f"{name}: endpoint={result['endpoint']} http_status={result['http_status']} "
              f"error={result['error']}")


def print_report():
    if not DOSSIER_PATH.exists():
        print(f"no dossier yet at {DOSSIER_PATH}; run: python experiments/lab/exp_connectors.py sample")
        return
    dossier = json.loads(DOSSIER_PATH.read_text(encoding="utf-8"))

    print("=== pass 1 (step-09a, H1: marginal retrieval surface) ===")
    print(f"schema_version={dossier['schema_version']} evidence_cutoff={dossier['evidence_cutoff']} "
          f"git_commit={dossier['git_commit']} tool_version={dossier['tool_version']}")
    print(f"verdict: {dossier['verdict']}")
    print(f"  {dossier.get('verdict_detail', '')}")
    print(f"spend_actual: {dossier['spend_actual']}")
    for lane, info in dossier["lanes"].items():
        print(f"lane {lane}: {info['status']} -- {info.get('detail') or info.get('reason')}")
    for c in dossier["connectors"]:
        m = c["metrics"]
        print(f"- {c['name']}: sample={c['sample']['status']} n_sampled={m['n_sampled']} "
              f"validity={m['sample_validity_rate']} median_age_days={m['median_age_days']} "
              f"marginal_unique_rate={m['marginal_unique_rate']} ({m['marginal_unique_rate_status']})")

    pass2 = dossier.get("pass_2_e5b")
    if not pass2:
        print("\n=== pass 2 (step-09, H2: usable retrieval yield) ===\nnot yet run")
        return
    print("\n=== pass 2 (step-09, H2: usable retrieval yield) ===")
    print(f"evidence_cutoff={pass2['evidence_cutoff']} git_commit={pass2['git_commit']}")
    print(f"verdict: {pass2['verdict']}")
    print(f"  {pass2.get('verdict_detail', '')}")
    print(f"spend_actual: {pass2['spend_actual']}")
    print(f"gate: {pass2['gate']}")
    print(f"dispositions: {pass2['dispositions']}")
    for c in pass2["connectors"]:
        new_arm, old_arm = c["arm_new_rule"], c["arm_old_rule_recomputed"]
        print(f"- {c['name']}: new_rule usable_yield={new_arm['usable_yield']} "
              f"(n_sampled={new_arm['n_sampled']}, mur={new_arm['marginal_unique_rate']} "
              f"[{new_arm['marginal_unique_rate_status']}]) | "
              f"old_rule_recomputed usable_yield={old_arm['usable_yield']} "
              f"(n_sampled={old_arm['n_sampled']})")
    print(f"- x: {pass2['x']['status']}")
    if pass2["aborted_attempts"]:
        print(f"aborted_attempts: {pass2['aborted_attempts']}")


def main():
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["probe", "sample", "report"])
    ap.add_argument("--budget", type=int, default=40,
                    help="Lab provider-call budget cap for the live lane (unused unless a "
                        "live provider is reachable)")
    args = ap.parse_args()

    if args.mode == "report":
        print_report()
        return

    cfg = load_cfg()
    lab = Lab("connectors", budget_cap=args.budget)
    if args.mode == "probe":
        run_probe(cfg)
        return

    # `sample` now runs ONLY the H2 (pass_2_e5b) pass: step-09a's own
    # zero_spend lane already ran for real and its dossier keys are frozen
    # (see PREREGISTRATION_PASS2 above) -- re-running it would spend fresh
    # requests against live, non-reproducible data and silently overwrite
    # results the pre-registration commits this pass supersedes rather than
    # edits. The frozen dossier must already exist; there is no code path
    # left that regenerates it from scratch.
    if not DOSSIER_PATH.exists():
        print(f"no existing dossier at {DOSSIER_PATH} -- step-09a's frozen pass_1 "
              "results must already be committed before pass_2_e5b can run", file=sys.stderr)
        return 1
    dossier = json.loads(DOSSIER_PATH.read_text(encoding="utf-8"))
    pass2 = run_pass2_e5b(dossier, cfg)
    dossier["pass_2_e5b"] = pass2
    lab.log(event="pass2_e5b_run", verdict=pass2["verdict"], gate=pass2["gate"],
            http_requests=pass2["spend_actual"]["http_requests"])
    lab.save()
    DOSSIER_PATH.write_text(json.dumps(dossier, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {DOSSIER_PATH} (pass_2_e5b)")
    print(json.dumps({"verdict": pass2["verdict"], "gate": pass2["gate"],
                     "spend_actual": pass2["spend_actual"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
