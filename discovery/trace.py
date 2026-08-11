"""Append-only trace storage: the observability backbone for the discovery
engine (step-13 task 1). Everything the Council/missions/pipeline/scoring do
gets recorded here as a run -> nodes -> edges graph, plus one model_calls row
per provider call attempt -- so a later Datasette plugin / React UI (tasks 2
and 3) can answer "why did/didn't this get notified" without re-deriving it.

Two properties everything here is built around:

  * Off is a true no-op. `cfg.trace_enabled` (DISCOVERY_TRACE, default on) is
    read once, at Tracer construction -- with it off, every method returns
    immediately with no SQL touching a trace_* table at all. This is the
    rollback lever.
  * Fail-soft, always. A tracing write that raises (disk full, a locked db)
    is swallowed after bumping the 'trace_write_failed' metric via db.bump --
    tracing must never abort a tick, change a ranking, or turn an otherwise-
    successful call into a failure. Every public method that can fail routes
    through `_guard`.

Nothing here duplicates logging at the council/missions/scoring call sites:
those modules call Tracer.node()/edge()/finish_node() at the seams the plan
specifies, and the *provider* call itself is captured centrally by
providers/base.py's LLMProvider._emit_call(), which forwards to
`Tracer.sink` when installed as `provider.trace_sink`. Callers set which
(call_role, node_id) the next provider call(s) belong to via
`tracer.calls(role, node_id)`; providers never know about roles.

Redaction: `redact()`/`redact_json()` replace literal secret VALUES (not
field names) found in the environment -- so a prompt/interest/response is
stored byte-exact except for genuine secrets. See the module docstring's
Goodhart-adjacent caution in council.py: this module has no opinion on what
content is safe to show a human, only on what must never be a raw secret.
"""
import dataclasses
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from . import db

# Relationship vocabulary, used verbatim -- kept as a plain tuple (not an
# enum: no new abstraction for a set of string constants) so both trace.py
# and any caller can validate against the same list.
RELATIONSHIPS = (
    "generated", "selected", "executed", "returned", "normalized_to",
    "duplicate_of", "matched", "rejected", "deferred", "scored",
    "cleared_threshold", "rendered", "sent", "failed", "retried_as",
    "feedback_on",
)

_SECRET_NAME_RE = re.compile(r"(?i)(token|secret|key|password|cookie|auth)")

# A candidate secret shorter than this is almost certainly a flag/mode value
# ("1", "us", "true"), not a genuine token -- redacting it would rewrite
# every stored prompt/response that happens to contain that short substring,
# silently breaking the byte-exact guarantee with no signal it happened.
_MIN_SECRET_LEN = 8


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _secret_values():
    """{value: VARNAME} for every non-empty environment variable whose NAME
    looks like a secret. Read fresh on every call (not cached at import time)
    so a test that monkeypatches os.environ mid-run is picked up immediately,
    and so a value that changes (rotated token) doesn't stick around stale."""
    values = {}
    for name, value in os.environ.items():
        if value and len(value) >= _MIN_SECRET_LEN and _SECRET_NAME_RE.search(name):
            values[value] = name
    return values


def redact(text):
    """Replace every occurrence of a secret env-var VALUE with
    '[REDACTED:<VARNAME>]'. Only ever substitutes genuine secret values --
    everything else in `text` (prompts, interests, responses) survives
    byte-exact. Non-string input is returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    secrets = _secret_values()
    if not secrets:
        return text
    # Longest value first: if one secret's value happens to be a substring of
    # another's, the longer (more specific) one must win the replacement.
    for value in sorted(secrets, key=len, reverse=True):
        if value in text:
            text = text.replace(value, f"[REDACTED:{secrets[value]}]")
    return text


def redact_json(obj):
    """redact() applied recursively through a JSON-shaped value."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    return obj


def _dumps(obj):
    if obj is None:
        return None
    return json.dumps(redact_json(obj), sort_keys=True, default=str)


class Tracer:
    """Bound to one (conn, cfg). One Tracer per command invocation (see
    __main__.py/missions.py/pipeline.py's run-level wrapping) -- cheap to
    construct, holds no state beyond the current call-attribution context."""

    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg
        self.enabled = bool(getattr(cfg, "trace_enabled", True))
        self._call_role = None
        self._call_node = None
        # Convenience only -- every method still takes run_id explicitly (the
        # plan's own spelling), but callers that only ever have one run open
        # per Tracer (every caller today) can write `tracer.node(tracer.run_id, ...)`
        # instead of threading a second variable through their own call chain.
        self.run_id = None

    # --- run lifecycle --------------------------------------------------------

    def begin_run(self, kind, provider="", model="", config_json=None):
        """Opens a trace_runs row. `config_json`, if omitted, defaults to a
        redacted snapshot of the bound cfg (dataclasses.asdict) -- callers
        only need to pass it explicitly to override that default."""
        if not self.enabled:
            return None
        if config_json is None:
            config_json = _cfg_snapshot(self.cfg)
        self.run_id = self._guard(lambda: self._begin_run(kind, provider, model, config_json))
        return self.run_id

    def _begin_run(self, kind, provider, model, config_json):
        cur = self.conn.execute(
            """INSERT INTO trace_runs (kind, provider, model, config_json, started_at, status)
               VALUES (?, ?, ?, ?, ?, 'running')""",
            (kind, provider, model, _dumps(config_json), _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id, status="done", error=None):
        if not self.enabled or run_id is None:
            return
        self._guard(lambda: self._finish_run(run_id, status, error))

    def _finish_run(self, run_id, status, error):
        self.conn.execute(
            "UPDATE trace_runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (status, _now(), redact(error) if error else error, run_id),
        )
        self.conn.commit()

    # --- nodes ------------------------------------------------------------------

    def node(self, run_id, node_type, entity_type=None, entity_id=None, label="",
             status="ok", summary="", input_json=None, output_json=None, exact_text=None):
        if not self.enabled or run_id is None:
            return None
        return self._guard(lambda: self._node(
            run_id, node_type, entity_type, entity_id, label, status, summary,
            input_json, output_json, exact_text,
        ))

    def _node(self, run_id, node_type, entity_type, entity_id, label, status, summary,
              input_json, output_json, exact_text):
        cur = self.conn.execute(
            """INSERT INTO trace_nodes
                (run_id, node_type, entity_type, entity_id, label, status, summary,
                 input_json, output_json, exact_text, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, node_type, entity_type,
                str(entity_id) if entity_id is not None else None,
                redact(label), status, redact(summary) or "", _dumps(input_json), _dumps(output_json),
                redact(exact_text), _now(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_node(self, node_id, status=None, summary=None, output_json=None, error=None):
        if not self.enabled or node_id is None:
            return
        self._guard(lambda: self._finish_node(node_id, status, summary, output_json, error))

    def _finish_node(self, node_id, status, summary, output_json, error):
        sets, args = ["finished_at = ?"], [_now()]
        if status is not None:
            sets.append("status = ?"); args.append(status)
        if summary is not None:
            sets.append("summary = ?"); args.append(redact(summary))
        if output_json is not None:
            sets.append("output_json = ?"); args.append(_dumps(output_json))
        if error is not None:
            sets.append("error = ?"); args.append(redact(error))
        args.append(node_id)
        self.conn.execute(f"UPDATE trace_nodes SET {', '.join(sets)} WHERE id = ?", args)
        self.conn.commit()

    # --- edges ------------------------------------------------------------------

    def edge(self, from_node_id, to_node_id, relationship, ordinal=0):
        if not self.enabled or from_node_id is None or to_node_id is None:
            return None
        return self._guard(lambda: self._edge(from_node_id, to_node_id, relationship, ordinal))

    def _edge(self, from_node_id, to_node_id, relationship, ordinal):
        # Validation lives inside the guarded call (not a bare `assert`
        # before it): a bad relationship must be recorded as a fail-soft
        # trace_write_failed bump, never raise out and abort the tick -- and
        # unlike `assert`, this check survives `python -O`.
        if relationship not in RELATIONSHIPS:
            raise ValueError(f"unknown trace relationship {relationship!r}")
        cur = self.conn.execute(
            "INSERT INTO trace_edges (from_node_id, to_node_id, relationship, ordinal) VALUES (?, ?, ?, ?)",
            (from_node_id, to_node_id, relationship, ordinal),
        )
        self.conn.commit()
        return cur.lastrowid

    # --- provider call attribution + sink ----------------------------------------

    @contextmanager
    def calls(self, role, node_id):
        """While inside this block, any provider call made through a
        provider whose `trace_sink` is `self.sink` is attributed to
        (role, node_id). Restores the previous context on exit so a nested
        use can't leak its attribution to the caller's own later calls."""
        previous = (self._call_role, self._call_node)
        self.set_call_context(role, node_id)
        try:
            yield
        finally:
            self._call_role, self._call_node = previous

    def set_call_context(self, role, node_id):
        self._call_role, self._call_node = role, node_id

    def sink(self, **kwargs):
        """The callable installed as `provider.trace_sink`. Called once per
        provider-call attempt by LLMProvider._emit_call(); a no-op when
        tracing is off or nothing has set a call context (a provider used
        outside any `tracer.calls()` block -- e.g. an interactive `score`
        command -- just isn't attributed to a node, which is correct: there
        is no node to attach it to)."""
        if not self.enabled or self._call_node is None:
            return
        self._guard(lambda: self._sink(**kwargs))

    def _sink(self, attempt, provider, model, system, prompt, schema, params,
              raw_text, parsed, validation, error, started, finished,
              request_id=None, usage=None, events=None, call_role=None):
        node_id = self._call_node
        role = self._call_role or call_role or ""
        if events is not None:
            usage = dict(usage or {})
            usage["events"] = events
        self.conn.execute(
            """INSERT INTO model_calls
                (trace_node_id, call_role, attempt, provider, model,
                 exact_system_prompt, exact_user_prompt, exact_schema_json, exact_parameters_json,
                 raw_response_text, parsed_response_json, validation_result, usage_json,
                 provider_request_id, started_at, finished_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node_id, role, attempt, provider, model,
                redact(system), redact(prompt), _dumps(schema), _dumps(params),
                redact(raw_text), _dumps(parsed), redact(validation), _dumps(usage),
                request_id, started, finished, redact(error) if error else error,
            ),
        )
        self.conn.commit()

    # --- lookups -----------------------------------------------------------------

    def find_entity_node(self, entity_type, entity_id, node_type=None):
        """Most recent trace node linked to a given DB row, across any run --
        used to resolve a duplicate_of edge back to the item it duplicates
        (which may have first appeared on an earlier run/tick), or the most
        recent send attempt on a score for a retried_as edge. `node_type`
        narrows to one kind of node when an entity carries several (e.g. a
        score has both a 'score' node and, per send attempt, a 'render'
        node). None (not an error) when tracing is off or nothing was ever
        traced for it."""
        if not self.enabled or entity_id is None:
            return None
        return self._guard(lambda: self._find_entity_node(entity_type, entity_id, node_type))

    def _find_entity_node(self, entity_type, entity_id, node_type):
        if node_type is None:
            row = self.conn.execute(
                """SELECT id FROM trace_nodes WHERE entity_type = ? AND entity_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (entity_type, str(entity_id)),
            ).fetchone()
        else:
            row = self.conn.execute(
                """SELECT id FROM trace_nodes WHERE entity_type = ? AND entity_id = ? AND node_type = ?
                   ORDER BY id DESC LIMIT 1""",
                (entity_type, str(entity_id), node_type),
            ).fetchone()
        return row["id"] if row else None

    # --- fail-soft guard -----------------------------------------------------------

    def _guard(self, fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 -- tracing must never abort a tick
            try:
                db.bump(self.conn, {"trace_write_failed": 1})
            except Exception:  # noqa: BLE001
                pass
            return None


def _cfg_snapshot(cfg):
    if cfg is None:
        return None
    try:
        return dataclasses.asdict(cfg)
    except TypeError:
        return None


class _DisabledCfg:
    trace_enabled = False


# A Tracer that is always off -- every method is the same cheap no-op as a
# real Tracer constructed with cfg.trace_enabled=False. Lets pipeline.py/
# missions.py functions take `tracer=None` and default to
# `tracer = tracer or NULL_TRACER` once, instead of an `if tracer:` guard
# around every single call site.
NULL_TRACER = Tracer(None, _DisabledCfg())
