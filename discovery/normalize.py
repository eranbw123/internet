"""Turn whatever a collector produced into a canonical CandidateItem.

Collectors are allowed to be sloppy (tracking params on URLs, whitespace,
half-parsed dates); everything downstream -- dedup especially -- assumes
normalized fields, so this runs on every item before it touches the DB.
"""
import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Campaign/referrer junk that changes per share but not per article.
TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "source", "spm", "utm_campaign", "utm_content", "utm_id", "utm_medium",
    "utm_name", "utm_source", "utm_term",
}
MAX_TEXT_CHARS = 20000  # a transcript is fine; a whole book is not worth scoring


def normalize(item, origin_interest=None):
    """Canonicalise in place and return the item."""
    item.url = canonical_url(item.url)
    item.title = collapse_whitespace(item.title)
    item.text = collapse_whitespace(item.text)[:MAX_TEXT_CHARS]
    item.author = collapse_whitespace(item.author) or None
    item.published_at = normalize_date(item.published_at)
    if origin_interest and not item.origin_interest:
        item.origin_interest = origin_interest
    if not item.dedup_key:
        item.dedup_key = item.url

    item.url_hash = _hash(item.url)
    item.title_hash = _hash(_comparable(item.title))
    # Titles repeat across syndicated copies, so content is hashed separately;
    # an item with no body just has no content hash rather than a hash of "".
    item.content_hash = _hash(_comparable(item.text)) if item.text else None
    return item


def canonical_url(url):
    """Lowercase scheme/host, drop tracking params and fragments, sort the rest.

    Two links to the same article should produce the same string -- that is
    what makes URL dedup work across collectors and across runs.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if (scheme, parts.port) in (("http", 80), ("https", 443)):
        host = host.rsplit(":", 1)[0]
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        sorted(p for p in parse_qsl(parts.query, keep_blank_values=True)
               if p[0].lower() not in TRACKING_PARAMS)
    )
    return urlunsplit((scheme, host, path, query, ""))


def collapse_whitespace(text):
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_date(value):
    """Keep an ISO-ish date/datetime, drop anything we can't recognise.

    Deliberately not a date parser: a wrong timestamp is worse than none, and
    nothing downstream depends on the value beyond display and recency.
    """
    value = (value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})([T ]\d{2}:\d{2}(:\d{2})?)?", value)
    return value if match else None


def _comparable(text):
    """Casefolded, punctuation-stripped form used only for hashing.

    So "Orexin agonist hits endpoint" and "Orexin Agonist Hits Endpoint!"
    dedup against each other.
    """
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[^\w\s]", "", text).strip()


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
