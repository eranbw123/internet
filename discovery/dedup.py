"""Duplicate detection, in the order that costs least.

Three exact layers, because collectors disagree about the same story in three
ways: the same URL with different tracking params (canonicalised URL hash),
the same story re-published under the same headline elsewhere (title hash),
and the same body under a rewritten headline (content hash).

`(source, dedup_key)` uniqueness in SQLite is the backstop for exact repeats
within one collector; this module catches the cross-collector cases before an
item is stored at all.

A fourth, near-duplicate layer runs later in the pipeline (after the item is
stored and prefiltered, right before it would be scored): the same story
RE-TOLD -- three outlets each wording "VPG down 25%" their own way -- shares
no URL, title or body hash. llm_near_duplicate() finds lexical suspects among
recently stored articles for free, and only a non-empty suspect list buys one
small LLM call to confirm. A confirmed repeat is linked
(candidate_items.duplicate_of) instead of scored, so the judge call replaces
the strictly larger scoring call it saves. There is no hard time rule for
what counts as a duplicate: the window only bounds how far back retrieval
looks; the judge sees dates and decides.
"""
import json
import sys
from collections import namedtuple

from . import db, normalize

Duplicate = namedtuple("Duplicate", "existing reason")

# Short bodies collide by chance (a one-line stock summary, a boilerplate
# abstract), so content-hash matching only applies above this length.
MIN_CONTENT_CHARS_FOR_HASH = 200


def find_duplicate(conn, item):
    """Return a Duplicate if this item is already stored, else None."""
    for column, reason, value in (
        ("url_hash", "same url", item.url_hash),
        ("title_hash", "same title", item.title_hash),
        ("content_hash", "same body text", _content_hash(item)),
    ):
        existing = db.find_item_by_hash(conn, column, value)
        if existing is not None and existing.id != item.id:
            return Duplicate(existing, reason)
    return None


def _content_hash(item):
    if not item.text or len(item.text) < MIN_CONTENT_CHARS_FOR_HASH:
        return None
    return item.content_hash


# --- near-duplicates: free lexical suspects, one small LLM call to confirm ---

NEAR_DUP_SYSTEM = """\
You decide whether a news item is a repeat of a story the reader has already \
been shown. Two items are the same story when they report the same underlying \
event or fact -- the same company's same price move, the same paper, the same \
deal or announcement -- even when the outlet, wording, or level of detail \
differ. Items about the same subject are NOT the same story when they report \
distinct developments. Dates are shown: the same kind of event on a different \
date is a different story."""

NEAR_DUP_PROMPT = """\
<new_item>
{new}
</new_item>

<already_stored>
{stored}
</already_stored>

If the new item is the same story as one already stored, return that item's \
id as duplicate_of (the closest match, if several qualify); otherwise return \
null. Also return reason: one short sentence naming the shared fact -- or, \
for null, the development that sets the new item apart.
"""

NEAR_DUP_SCHEMA = {
    "type": "object",
    "properties": {
        "duplicate_of": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["duplicate_of", "reason"],
    "additionalProperties": False,
}

# Tokens too common in headlines to signal a shared story on their own.
STOPWORDS = frozenset("""
a about after against all amid an and are as at be but by can could down for
from get has have her his how in into is it its more new no not now of off on
or out over says set she so than that the their they this to top up was what
when who why will with
""".split())

SNIPPET_CHARS = 300          # of body text folded into each token set
PROMPT_SNIPPET_CHARS = 400   # of body text the judge sees per item
MIN_SHARED_TOKENS = 3
# Overlap coefficient |A&B| / min(|A|,|B|) -- recall-leaning on purpose; the
# judge supplies the precision. Numbers survive _comparable(), so a shared
# "25" pulls its weight.
MIN_OVERLAP = 0.4
POOL_MAX_ROWS = 2000         # newest-first cost bound on the retrieval pool


def llm_near_duplicate(conn, provider, item, cfg):
    """The stored article that already tells this item's story, or None.

    Free until proven suspicious: token overlap against recently stored
    articles costs no calls, and only a non-empty suspect list buys one
    complete_json. Fails open -- a judge outage repeats a story rather than
    losing one.
    """
    if item.type != "article" or not cfg.dedup_llm:
        return None
    suspects = find_suspects(conn, item, cfg)
    if not suspects:
        return None
    try:
        data = provider.complete_json(
            NEAR_DUP_SYSTEM, _near_dup_prompt(item, suspects), NEAR_DUP_SCHEMA,
            max_tokens=1000,
        )
    except Exception as e:  # noqa: BLE001
        print(f"near-dup judge failed for item {item.id}: {e}", file=sys.stderr)
        return None
    chosen = data.get("duplicate_of")
    if chosen not in {s["id"] for s in suspects}:  # null verdict, or a made-up id
        return None
    existing = db.get_item(conn, chosen)
    if existing is None:
        return None
    return Duplicate(existing, f"same story as #{chosen}: {data.get('reason', '')}"[:300])


def find_suspects(conn, item, cfg):
    """Stored articles lexically close enough to be the same story, best first.

    Two free signals, either is enough: token overlap between title+snippet
    sets, and a shared metadata ticker (the stocks collector stamps one on the
    articles it fetches to explain a move, so two explanations of the same
    move match even with disjoint wording). Capped at cfg.dedup_max_candidates.
    """
    tokens = _tokens(item.title, item.text)
    ticker = (item.metadata or {}).get("ticker")
    scored = []
    for row in db.near_dup_pool(
        conn, db.ago(cfg.dedup_window_days * 86400),
        exclude_id=item.id, limit=POOL_MAX_ROWS,
    ):
        row_tokens = _tokens(row["title"], row["snippet"])
        shared = len(tokens & row_tokens)
        overlap = shared / max(1, min(len(tokens), len(row_tokens)))
        same_ticker = bool(ticker) and _row_ticker(row) == ticker
        if same_ticker or (shared >= MIN_SHARED_TOKENS and overlap >= MIN_OVERLAP):
            scored.append((overlap + (1.0 if same_ticker else 0.0), row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[: cfg.dedup_max_candidates]]


def _tokens(title, text):
    words = normalize._comparable(f"{title} {(text or '')[:SNIPPET_CHARS]}").split()
    return {w for w in words if len(w) > 1 and w not in STOPWORDS}


def _row_ticker(row):
    try:
        return json.loads(row["metadata"] or "{}").get("ticker")
    except ValueError:
        return None


def _near_dup_prompt(item, suspects):
    stored = "\n\n".join(
        _describe(s["id"], s["title"], s["source"], s["published_at"],
                  s["snippet"], s["first_seen_at"])
        for s in suspects
    )
    new = _describe(None, item.title, item.source, item.published_at, item.text)
    return NEAR_DUP_PROMPT.format(new=new, stored=stored)


def _describe(item_id, title, source, published, text, first_seen=None):
    head = f"[id {item_id}] {title}" if item_id is not None else title
    when = published or (f"first seen {first_seen}" if first_seen else "unknown")
    return f"{head}\nSource: {source} | Published: {when}\n{(text or '')[:PROMPT_SNIPPET_CHARS]}"
