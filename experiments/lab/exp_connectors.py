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
never as 0. (In practice the web_search sample always needs a provider call,
so it is *structurally* unavailable in the zero-spend lane -- see
`verdict_detail` in the persisted dossier.)

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


def _reset_http_state():
    """Test seam: reset module-level counters between test cases."""
    global _request_count, _last_request_at
    _request_count = 0
    _last_request_at = {}


class ConnectorUnreachable(Exception):
    """The endpoint could not be reached at all (DNS/connect/timeout)."""


def _http_get(url, timeout=15):
    """The harness's ONE network entry point: enforces the request-count cap,
    per-host spacing (arxiv gets its own 3s etiquette gap), a descriptive
    User-Agent, and the timeout. Returns (status, body_bytes, headers_dict).
    Tests monkeypatch this whole function to exercise the connector-shaping
    and analysis code without touching the network; the counter/spacing
    logic itself is tested by patching urllib.request.urlopen underneath it.
    """
    global _request_count
    host = urllib.parse.urlparse(url).netloc
    if _request_count >= MAX_REQUESTS:
        raise RuntimeError(f"request cap {MAX_REQUESTS} reached; refusing {url}")
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
        raise ConnectorUnreachable(f"{host}: {e}") from e
    _request_count += 1
    _last_request_at[host] = time.monotonic()
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


def build_query(interest):
    parts = [interest.title, *interest.positive_signals[:3]]
    text = " ".join(p for p in parts if p)
    return text[:QUERY_MAX_CHARS]


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


def _sample(url, parser):
    """One bounded fetch-and-parse through _http_get. Never raises: any
    failure (network, HTTP status, parse) lands in entry['error']."""
    entry = {"records": [], "http_status": None, "endpoint": url, "error": None,
             "rate_limit_headers": {}}
    try:
        status, body, headers = _http_get(url)
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
        status, body, headers = _http_get(esearch_url)
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
        status2, body2, headers2 = _http_get(esummary_url)
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
    "hackernews": lambda q, n=N_PER_QUERY: _sample(hn_url(q, n), parse_hn),
    "reddit": lambda q, n=N_PER_QUERY: _sample(reddit_url(q, n), parse_reddit),
    "arxiv": lambda q, n=N_PER_QUERY: _sample(arxiv_url(q, n), parse_arxiv),
    "pubmed": sample_pubmed,
}


def _static_budget_check():
    """Guards the pre-registered request caps against a future edit to
    APPLICABILITY: fails loudly at import time rather than silently
    overspending."""
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


def build_x_entry(applicable, provider_ok, provider_why):
    detail = (f"provider.search_json requires a live claude.ai Chrome/CDP session; "
             f"provider.preflight() here: {provider_ok} "
             f"({provider_why or 'reachable, but this lane makes 0 provider calls by pre-registration'})")
    return {
        "name": "x",
        "applicable_interests": list(applicable),
        "availability": {"reachable": None, "status": "PENDING", "detail": detail, "checked_at": now()},
        "sample": {"status": "PENDING", "detail": detail, "per_interest": []},
        "metrics": {
            "marginal_unique_rate": None, "marginal_unique_rate_status": "PENDING_LIVE_LANE",
            "jaccard_overlap_with_web_search_sample": None, "jaccard_overlap_status": "PENDING_LIVE_LANE",
            "sample_validity_rate": None, "median_age_days": None,
            "above_bar_count": None, "above_bar_status": "PENDING_LIVE_LANE",
            "n_sampled": 0, "n_unique_urls": 0,
        },
        "failure_behavior": None,
        "command_to_complete": (
            "python experiments/lab/exp_connectors.py sample  # from a live operator "
            "session: Chrome --remote-debugging-port=9222, logged into claude.ai"
        ),
    }


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

    above_bar_count = above_bar_n = None
    above_bar_status = "VOID_NO_PROVIDER"
    if provider_ok:
        above_bar_count, above_bar_n = score_records_above_bar(provider, lab, per_interest, interests_by_key)
        above_bar_status = "OK"

    metrics = {
        "marginal_unique_rate": None,
        "marginal_unique_rate_status": "VOID_NO_BASELINE",
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
    if corpus_available and sample_urls:
        # Diagnostic only, NOT the pre-registered primary metric (which also
        # needs the web_search sample). corpus is a subset of the true
        # baseline, so this is an upper bound on the true marginal_unique_rate.
        metrics["marginal_unique_rate_vs_corpus_only_diagnostic"] = marginal_unique_rate(sample_urls, corpus_urls)

    failures = [f"{pi['interest']}: {pi['error']}" for pi in per_interest if pi["error"]]
    rl = {pi["interest"]: pi["rate_limit_headers"] for pi in per_interest if pi["rate_limit_headers"]}
    failure_behavior = "; ".join(failures) if failures else "no failures observed in this pass"
    if rl:
        failure_behavior += f" | rate-limit headers observed: {rl}"

    return {
        "name": name,
        "applicable_interests": list(applicable),
        "availability": {
            "reachable": bool(successes),
            "http_status": next((pi["http_status"] for pi in per_interest if pi["http_status"] is not None), None),
            "endpoint": per_interest[0]["endpoint"] if per_interest else None,
            "checked_at": per_interest[0]["collected_at"] if per_interest else now(),
            "detail": sample_detail or "reachable",
        },
        "sample": {"status": sample_status_, "detail": sample_detail, "per_interest": per_interest},
        "metrics": metrics,
        "failure_behavior": failure_behavior,
    }


def run_sample(lab, cfg):
    now_dt = datetime.now(timezone.utc)
    git_commit = git_head_sha()
    interests_by_key = {i.key: i for i in load_interests_file(cfg.interests_path)}

    corpus_urls, corpus_status = load_corpus_urls(cfg.db_path)
    provider = get_provider(cfg)
    provider_ok, provider_why = provider.preflight()

    connectors_out = []
    for name in CONNECTORS:
        applicable = [k for k in APPLICABILITY[name] if k in interests_by_key]
        if name == "x":
            connectors_out.append(build_x_entry(applicable, provider_ok, provider_why))
        else:
            connectors_out.append(build_http_connector_entry(
                name, applicable, interests_by_key, corpus_urls or set(), corpus_status["available"],
                now_dt, git_commit, provider, provider_ok, lab))

    # web_search_sample is structurally live-lane-only (needs a provider
    # call); the zero-spend lane never has it, so the pre-registered
    # baseline (corpus UNION web_search_sample) can never be fully assembled
    # here regardless of corpus availability.
    baseline_available = False
    connector_metrics_for_rule = {
        c["name"]: {"marginal_unique_rate": c["metrics"]["marginal_unique_rate"],
                    "jaccard_overlap": c["metrics"]["jaccard_overlap_with_web_search_sample"]}
        for c in connectors_out
    }
    verdict = apply_falsification_rule(connector_metrics_for_rule, baseline_available)

    corpus_note = ("available" if corpus_status["available"]
                   else f"not available -- {corpus_status['reason']}")
    verdict_detail = (
        "The pre-registered primary metric needs corpus UNION web_search_sample as "
        "its baseline. web_search_sample requires a provider.search_json call, which "
        "the zero-spend lane never makes by design -- so marginal_unique_rate and "
        "jaccard_overlap_with_web_search_sample are VOID for every connector this "
        f"pass, regardless of corpus availability (corpus at {cfg.db_path}: "
        f"{corpus_note}). H1 is UNDECIDED, not falsified, until a live-lane pass "
        "supplies the web_search sample. Reproduce with: "
        "python experiments/lab/exp_connectors.py sample (from a live operator "
        "session: Chrome --remote-debugging-port=9222, logged into claude.ai)."
    )

    dossier = {
        "schema_version": 1,
        "evidence_cutoff": now(),
        "git_commit": git_commit,
        "tool_version": TOOL_VERSION,
        "preregistration": PREREGISTRATION,
        "spend_actual": {
            "http_requests": _request_count,
            "provider_calls": lab.state["budget_used"],
            "paid_usd": 0.0,
            "youtube_quota_units": 0,
        },
        "lanes": {
            "zero_spend": {
                "status": "RAN",
                "detail": ("hackernews/reddit/arxiv/pubmed sampled via bounded free HTTP, "
                          "one pass per connector-interest pair; 0 provider calls spent."),
            },
            "live": {
                "status": "PENDING",
                "reason": (f"provider.preflight() = {provider_ok} ({provider_why or 'ok'}); "
                          "x's search_json sample, the web_search baseline sample, and the "
                          "above-bar sub-metric all require a live claude.ai Chrome/CDP "
                          "session, which an isolated worktree implementer session cannot "
                          "provide (see PROJECT_STATE.md step-01 precedent)."),
                "command_to_complete": "python experiments/lab/exp_connectors.py sample",
            },
        },
        "corpus": {**corpus_status, "path": cfg.db_path},
        "connectors": connectors_out,
        "prior_evidence": PRIOR_EVIDENCE,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
    }
    lab.log(event="sample_run", verdict=verdict, http_requests=_request_count)
    lab.save()
    return dossier


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

    dossier = run_sample(lab, cfg)
    DOSSIER_PATH.write_text(json.dumps(dossier, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {DOSSIER_PATH}")
    print(json.dumps({"verdict": dossier["verdict"], "spend_actual": dossier["spend_actual"]},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
