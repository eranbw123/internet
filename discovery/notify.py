"""Delivery. Telegram today, one message per item that cleared its bar.

Stdlib urllib rather than a Telegram library: it is one POST. A failed send is
logged and recorded as not-ok, never fatal -- a delivery outage must not stall
collection or scoring.
"""
import json
import sys
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"


def format_message(interest, item, final_score, reason, why_better="", confidence=None):
    """Scores are 0-1 internally; shown as 0-100 because that reads better on
    a phone. The reason is the whole point of the push -- it goes above the
    link, so the message is useful without opening anything."""
    header = f"[{final_score * 100:.0f}] {interest.title}"
    if confidence is not None and confidence < 0.5:
        header += f" (low confidence {confidence:.2f})"
    lines = [header, item.title, reason]
    if why_better:
        lines.append(why_better)
    lines.append(item.url)
    return "\n".join(line for line in lines if line)


def send(cfg, text, dry_run=False):
    """Returns True if the message was delivered (or printed in dry-run)."""
    if dry_run or not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        if not dry_run:
            print("TELEGRAM_BOT_TOKEN/CHAT_ID not set -- printing instead", file=sys.stderr)
        print(text)
        return dry_run

    payload = json.dumps(
        {"chat_id": cfg.telegram_chat_id, "text": text, "disable_web_page_preview": False}
    ).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=cfg.telegram_bot_token),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False
