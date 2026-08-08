"""Duplicate detection, in the order that costs least.

Three layers, because collectors disagree about the same story in three ways:
the same URL with different tracking params (canonicalised URL hash), the same
story re-published under the same headline elsewhere (title hash), and the
same body under a rewritten headline (content hash).

`(source, dedup_key)` uniqueness in SQLite is the backstop for exact repeats
within one collector; this module catches the cross-collector cases before an
item is stored at all.
"""
from collections import namedtuple

from . import db

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
