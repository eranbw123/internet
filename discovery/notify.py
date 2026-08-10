"""Delivery. Telegram today, one message per item that cleared its bar.

Stdlib urllib rather than a Telegram library: it's a handful of POSTs. A
failed send is logged and recorded as not-ok, never fatal -- a delivery
outage must not stall collection or scoring.

Two kinds of push, distinguished only by `item.type`:
  ALERT      time-sensitive (today: stocks.py's market_event) -- sent
             immediately by pipeline.deliver() every cycle.
  DISCOVERY  everything else -- held and sent later, batched, by
             pipeline.send_digest() (the daily `digest` job).
"""
import json
import sys
import urllib.request

API_BASE = "https://api.telegram.org"

# Types that skip the digest and go out the moment they clear the bar.
# Extend this set, not the pipeline, when a new collector needs the same
# urgency as stocks.py's market_event.
ALERT_TYPES = {"market_event"}

# Feedback-button vocabulary: callback_data code -> button label. The code is
# what's stored in feedback.verdict and what feedback_listener.py matches on.
FEEDBACK_VERDICTS = {
    "fire": "🔥 Very interesting",
    "up": "👍 Interesting",
    "down": "👎 Not interesting",
    "trash": "🗑 Bad match",
}


def is_alert(item):
    return item.type in ALERT_TYPES


def feedback_keyboard(score_id):
    """Telegram `reply_markup` for the four feedback buttons, two per row.
    `score_id` (not item_id) is embedded because it's the one id that gets
    the listener straight to item_id + interest_id + final_score in one
    lookup (db.score_by_id) -- see feedback_listener.py."""
    codes = list(FEEDBACK_VERDICTS)
    buttons = [
        {"text": FEEDBACK_VERDICTS[code], "callback_data": f"fb:{code}:{score_id}"}
        for code in codes
    ]
    return {"inline_keyboard": [buttons[:2], buttons[2:]]}


def _timestamp_range(item):
    """`⏱ 12:34-18:02` for a youtube video_segment; nothing for anything else."""
    start, end = item.metadata.get("start_time"), item.metadata.get("end_time")
    if item.type != "video_segment" or start is None or end is None:
        return None
    return f"⏱ {_mmss(start)}-{_mmss(end)}"


def _mmss(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_message(interest, item, final_score, reason, why_better="", confidence=None):
    """Scores are 0-1 internally; shown as 0-100 because that reads better on
    a phone.

    A `market_event` is the one exception to using `reason` as the
    explanation: stocks.py already built the full catalyst summary
    (confirmed/plausible/none, evidence, confidence) into `item.text`, and
    the scorer's one-sentence `reason` would only flatten that distinction.
    """
    kind = "🚨 ALERT" if is_alert(item) else "🔎 DISCOVERY"
    header = f"{kind}  ·  🔥 {final_score * 100:.0f}% interesting  ·  {interest.title}"
    if confidence is not None and confidence < 0.5:
        header += f"  (low confidence {confidence:.2f})"

    explanation = item.text if item.type == "market_event" else reason
    lines = [header, "", item.title, "", explanation]
    if why_better:
        lines += ["", "Why this matches me:", why_better]
    timestamp = _timestamp_range(item)
    if timestamp:
        lines += ["", timestamp]
    lines += ["", item.url]
    return "\n".join(line for line in lines if line is not None)


def api_call(token, method, params, timeout=15):
    """One Telegram Bot API POST. Raises on transport errors or `ok: false`
    so callers (feedback_listener.py's poll loop especially) can tell a real
    failure from an empty result."""
    request = urllib.request.Request(
        f"{API_BASE}/bot{token}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {body}")
    return body["result"]


def print_safe(text):
    """print() that survives a narrow console codepage (e.g. Windows cp1255,
    which can't encode emoji) -- falls back to '?' for anything it can't show
    instead of crashing the run. Actual Telegram delivery is unaffected: it
    goes out as UTF-8 JSON, never through this. `stats` reuses it for the same
    reason: its verdict labels are the same emoji."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding))


def send(cfg, text, reply_markup=None, dry_run=False):
    """Returns True if the message was delivered (or printed in dry-run)."""
    if dry_run or not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        if not dry_run:
            print("TELEGRAM_BOT_TOKEN/CHAT_ID not set -- printing instead", file=sys.stderr)
        print_safe(text)
        return dry_run

    payload = {"chat_id": cfg.telegram_chat_id, "text": text, "disable_web_page_preview": False}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        api_call(cfg.telegram_bot_token, "sendMessage", payload)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False
