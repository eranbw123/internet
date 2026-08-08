"""Long-polls Telegram for feedback-button presses (see
notify.feedback_keyboard) and records them via db.add_feedback.

Runs as its own process, separate from `run` (the collect/score/notify
scheduler): one blocks on Telegram's long poll, the other sleeps on a timer,
and merging them would mean threads/async for no real gain. `python -m app
listen`.

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


def listen(conn, cfg):
    token = cfg.telegram_bot_token
    chat_id = str(cfg.telegram_chat_id)
    if not token or not chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")

    # Skip any backlog: start after the latest existing update instead of
    # replaying old button presses on startup. A transient failure here is not
    # worth refusing to start over -- 0 just means "replay whatever is queued".
    try:
        latest = api_call(token, "getUpdates", {"offset": -1})
        offset = (latest[-1]["update_id"] + 1) if latest else 0
    except Exception as e:  # noqa: BLE001
        print(f"could not read the update backlog ({e}); starting from 0", file=sys.stderr)
        offset = 0

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
