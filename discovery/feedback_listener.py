"""Handles Telegram feedback-button presses (see notify.feedback_keyboard)
and records them via db.add_feedback.

Two entry points, one shared offset:

  * listen()  long-polls forever -- interactive use, `python -m app listen`.
  * drain()   one bounded pass, `timeout=0` -- `python -m app listen --drain`,
              meant to run as a short-lived scheduled task (see
              ops/install_tasks.py's every-5-minutes job) rather than a
              session child that gets reaped on disconnect.

Both read/write the same `telegram_offset` in service_state, so a button
press that arrives while nothing is listening is caught by the next drain
instead of being lost forever -- the old behavior (listen() skipped any
backlog on startup) was fine for a process that was never down, but this is
meant to be down between runs by design.

This only records feedback; nothing here retrains or re-scores anything.
`python -m app stats` is what turns the verdicts into a judgement about
whether the scoring works.
"""
import sys
import time
import urllib.error

from . import db, trace
from .notify import FEEDBACK_VERDICTS, api_call

POLL_TIMEOUT = 30


def _offset(conn):
    stored = db.state_get(conn, "telegram_offset")
    return int(stored) if stored is not None else 0


def listen(conn, cfg):
    token = cfg.telegram_bot_token
    chat_id = str(cfg.telegram_chat_id)
    if not token or not chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")

    offset = _offset(conn)

    print("feedback listener: waiting for button presses...", flush=True)
    while True:
        try:
            updates = api_call(
                token, "getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT},
                timeout=POLL_TIMEOUT + 10,
            )
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"poll error: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            db.state_set(conn, "telegram_offset", offset)
            callback = update.get("callback_query")
            if not callback or str(callback["message"]["chat"]["id"]) != chat_id:
                continue
            try:
                _handle_callback(conn, token, callback, cfg)
            except Exception as e:  # noqa: BLE001
                # answerCallbackQuery fails routinely -- Telegram expires a
                # callback id after ~a minute -- and the feedback is already
                # recorded by then. Losing the ack must not lose the listener.
                print(f"feedback callback failed: {e}", file=sys.stderr)


def drain(conn, cfg):
    """One bounded getUpdates pass: handles every pending callback, advances
    and persists the offset, and returns how many callbacks resulted in
    actual recorded feedback (not merely acked -- a garbage payload or a
    score id that's no longer tracked is acked but must not count, or
    `feedback_recorded` overstates what actually happened).

    A transport failure (Telegram down, network down) is not fatal to the
    process -- it is printed and counted, and `None` is returned instead of
    raising so a scheduled drain never crashes its task. `None` is a
    distinct sentinel from `0` ("polled fine, nothing to do") so the caller
    (__main__._drain_cmd) can tell a failed run from an empty one and not
    record it as a job success."""
    token = cfg.telegram_bot_token
    chat_id = str(cfg.telegram_chat_id)
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 0

    offset = _offset(conn)
    try:
        updates = api_call(token, "getUpdates", {"offset": offset, "timeout": 0}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"feedback drain: poll failed ({e})", file=sys.stderr)
        db.bump(conn, {"run_failed": 1})
        return None

    count = 0
    for update in updates:
        offset = update["update_id"] + 1
        callback = update.get("callback_query")
        if not callback or str(callback["message"]["chat"]["id"]) != chat_id:
            continue
        try:
            if _handle_callback(conn, token, callback, cfg):
                count += 1
        except Exception as e:  # noqa: BLE001
            print(f"feedback callback failed: {e}", file=sys.stderr)

    db.state_set(conn, "telegram_offset", offset)
    if count:
        db.bump(conn, {"feedback_recorded": count})
    return count


def _handle_callback(conn, token, callback, cfg=None):
    """callback_data is `fb:<verdict>:<score_id>` from feedback_keyboard().
    Returns True if feedback was actually recorded, False for a garbage
    payload or an untracked score id -- both are acked either way, but only
    a real recording should count towards `feedback_recorded`.

    Traced as its own tiny run (kind='feedback'; a no-op when cfg is None or
    tracing is off) rather than needing listen()/drain() to carry a Tracer
    of their own through every call -- a button press is a self-contained
    unit of work, same reasoning as run_once()/web_tick()'s own wrapping."""
    tracer = trace.Tracer(conn, cfg) if cfg is not None else trace.NULL_TRACER
    run_id = tracer.begin_run("feedback")
    parts = callback.get("data", "").split(":")
    if len(parts) != 3 or parts[0] != "fb" or parts[1] not in FEEDBACK_VERDICTS:
        api_call(token, "answerCallbackQuery", {"callback_query_id": callback["id"]})
        tracer.finish_run(run_id, status="rejected")
        return False

    _, verdict, score_id = parts
    row = db.score_by_id(conn, int(score_id))
    if row is None:
        api_call(
            token, "answerCallbackQuery",
            {"callback_query_id": callback["id"], "text": "That item is no longer tracked."},
        )
        tracer.finish_run(run_id, status="untracked")
        return False

    feedback_id = db.add_feedback(
        conn, row["item_id"], row["interest_id"], verdict, original_score=row["final_score"]
    )
    feedback_node = tracer.node(
        run_id, "feedback", entity_type="feedback", entity_id=feedback_id,
        label=verdict, input_json={"score_id": int(score_id)},
    )
    # "threshold" is the node type pipeline._score() creates that's actually
    # entity-linked to entity_type='scores' (the pre-persist scoring-attempt
    # node can't be -- score.id doesn't exist yet when it's created, and a
    # trace node's entity link can't be changed after the fact).
    score_node = tracer.find_entity_node("scores", score_id, node_type="threshold")
    tracer.edge(feedback_node, score_node, "feedback_on")
    notification_node = tracer.find_entity_node("notifications", score_id, node_type="notification")
    tracer.edge(feedback_node, notification_node, "feedback_on")
    api_call(
        token, "answerCallbackQuery",
        {"callback_query_id": callback["id"], "text": f"Recorded: {FEEDBACK_VERDICTS[verdict]}"},
    )
    tracer.finish_run(run_id, status="done")
    return True
