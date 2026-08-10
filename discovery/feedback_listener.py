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

from . import db
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
                _handle_callback(conn, token, callback)
            except Exception as e:  # noqa: BLE001
                # answerCallbackQuery fails routinely -- Telegram expires a
                # callback id after ~a minute -- and the feedback is already
                # recorded by then. Losing the ack must not lose the listener.
                print(f"feedback callback failed: {e}", file=sys.stderr)


def drain(conn, cfg):
    """One bounded getUpdates pass: handles every pending callback, advances
    and persists the offset, and returns how many callbacks were processed.
    A transport failure (Telegram down, network down) is not fatal -- it is
    printed, counted as a failed run, and swallowed rather than raised, so a
    scheduled drain never crashes its task."""
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
        return 0

    count = 0
    for update in updates:
        offset = update["update_id"] + 1
        callback = update.get("callback_query")
        if not callback or str(callback["message"]["chat"]["id"]) != chat_id:
            continue
        try:
            _handle_callback(conn, token, callback)
            count += 1
        except Exception as e:  # noqa: BLE001
            print(f"feedback callback failed: {e}", file=sys.stderr)

    db.state_set(conn, "telegram_offset", offset)
    if count:
        db.bump(conn, {"feedback_recorded": count})
    return count


def _handle_callback(conn, token, callback):
    """callback_data is `fb:<verdict>:<score_id>` from feedback_keyboard()."""
    parts = callback.get("data", "").split(":")
    if len(parts) != 3 or parts[0] != "fb" or parts[1] not in FEEDBACK_VERDICTS:
        api_call(token, "answerCallbackQuery", {"callback_query_id": callback["id"]})
        return

    _, verdict, score_id = parts
    row = db.score_by_id(conn, int(score_id))
    if row is None:
        api_call(
            token, "answerCallbackQuery",
            {"callback_query_id": callback["id"], "text": "That item is no longer tracked."},
        )
        return

    db.add_feedback(conn, row["item_id"], row["interest_id"], verdict, original_score=row["final_score"])
    api_call(
        token, "answerCallbackQuery",
        {"callback_query_id": callback["id"], "text": f"Recorded: {FEEDBACK_VERDICTS[verdict]}"},
    )
