"""YouTube videos, chunked into transcript segments.

A whole video is the wrong unit: a 90-minute podcast can be mostly filler
around one excellent 6-minute discussion, so this collector doesn't ask "is
this video relevant" -- it fetches each video's transcript, splits it into
fixed, overlapping time windows, and emits one CandidateItem *per segment*.
Everything downstream (matching, pre-filter, LLM scoring) already runs per
item, so "keep only strong segments" falls straight out of the existing
threshold -- no video-level judgement needed here.

Two stages, both best-effort and independently skippable:

  1. `_search_videos` -- YouTube Data API v3 (needs YOUTUBE_API_KEY), for
     `source_config["youtube"]["channels"]` (channel ids) and/or `["queries"]`
     (search terms). One bad channel/query is logged and skipped, like a bad
     query in web.py.
  2. `_fetch_transcript` -- youtube-transcript-api (`pip install
     youtube-transcript-api`, optional -- only this collector needs it).
     Most videos won't have a transcript in the requested language, or have
     captions off entirely; that's normal, not an error, so a video with no
     transcript is just skipped. `_fetch_transcript` is kept as its own
     function with a fixed contract (video id + languages in, snippet list or
     None out) so an audio-transcription fallback can slot in later without
     touching collection or chunking.

V0 chunking is a fixed window with overlap (`chunk_seconds` /
`chunk_overlap_seconds`), not semantic segmentation -- simple, and a window
sliding across a topic change still leaves some window mostly inside it.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..db import seen_dedup_keys
from ..models import CandidateItem

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

DEFAULT_MAX_VIDEOS = 5
DEFAULT_RECENCY_DAYS = 14
DEFAULT_WINDOW_SECONDS = 360    # 6 minutes -- the length of the motivating example
DEFAULT_OVERLAP_SECONDS = 60
DEFAULT_LANGUAGES = ["en"]


class YoutubeCollectorError(Exception):
    """A setup problem (no API key, a hard API failure), not a per-video miss.

    Raised, not swallowed: `collect()` lets it propagate so the pipeline's
    per-collector isolation (see collectors/__init__.py) logs it once instead
    of this module silently returning nothing.
    """


def collect(interest, cfg, provider=None, conn=None):   # provider unused: no LLM needed to find or chunk videos
    opts = interest.source_config.get("youtube", {})
    channels = opts.get("channels", [])
    queries = opts.get("queries", [])
    if not channels and not queries:
        return []

    api_key = getattr(cfg, "youtube_api_key", "") or ""
    if not api_key:
        raise YoutubeCollectorError("YOUTUBE_API_KEY not set")

    max_videos = int(opts.get("max_videos", DEFAULT_MAX_VIDEOS))
    recency_days = int(opts.get("recency_days", DEFAULT_RECENCY_DAYS))
    window_seconds = int(opts.get("chunk_seconds", DEFAULT_WINDOW_SECONDS))
    overlap_seconds = int(opts.get("chunk_overlap_seconds", DEFAULT_OVERLAP_SECONDS))
    languages = opts.get("languages", DEFAULT_LANGUAGES)
    limit = int(opts.get("limit", cfg.max_items_per_source))

    published_after = (datetime.now(timezone.utc) - timedelta(days=recency_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    videos, seen_ids = [], set()
    for channel_id in channels:
        videos.extend(
            _safe_search(api_key, published_after, max_videos, seen_ids, channel_id=channel_id)
        )
    for query in queries:
        videos.extend(
            _safe_search(api_key, published_after, max_videos, seen_ids, query=query)
        )

    items = []
    for video in videos:
        # The limit is checked at video boundaries, not mid-video: once a
        # video's segments are stored, the seen-prefix skip below never fetches
        # it again, so cutting a video off halfway would permanently drop its
        # later segments (the minute-80 gem in a 90-minute podcast). Emitting a
        # whole video may overshoot the limit by a few segments; the score
        # budget bounds what that can cost.
        if len(items) >= limit:
            break
        # Every segment's dedup_key starts with the video id, so one cheap
        # lookup skips a whole re-listed video before its transcript is fetched
        # and re-chunked -- the search job re-lists the same uploads every run.
        if conn is not None and seen_dedup_keys(conn, "youtube", video["video_id"] + ":"):
            continue
        transcript = _fetch_transcript(video["video_id"], languages)
        if not transcript:
            continue
        for start, end, text in _chunk_transcript(transcript, window_seconds, overlap_seconds):
            items.append(_to_item(video, start, end, text))
    return items


# --- video discovery ---------------------------------------------------------

def _safe_search(api_key, published_after, max_results, seen_ids, channel_id=None, query=None):
    """`_search_videos`, but one bad channel/query is logged and skipped
    rather than sinking every other one queued for this interest."""
    try:
        found = _search_videos(api_key, published_after, max_results, channel_id, query)
    except YoutubeCollectorError as e:
        print(f"youtube collector: {e}", file=sys.stderr)
        return []
    videos = []
    for video in found:
        if video["video_id"] in seen_ids:
            continue
        seen_ids.add(video["video_id"])
        if query:
            video["query"] = query
        videos.append(video)
    return videos


def _search_videos(api_key, published_after, max_results, channel_id, query):
    params = {
        "key": api_key,
        "part": "snippet",
        "type": "video",
        "order": "date",
        "maxResults": max_results,
        "publishedAfter": published_after,
    }
    if channel_id:
        params["channelId"] = channel_id
    if query:
        params["q"] = query
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise YoutubeCollectorError(f"search failed ({channel_id or query!r}): {e}") from e

    videos = []
    for entry in payload.get("items", []):
        video_id = entry.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = entry.get("snippet", {})
        videos.append(
            {
                "video_id": video_id,
                "video_title": snippet.get("title") or video_id,
                "channel": snippet.get("channelTitle") or "",
                "published_at": snippet.get("publishedAt"),
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return videos


# --- transcript + chunking ----------------------------------------------------

def _fetch_transcript(video_id, languages):
    """Snippet list (`.text` / `.start` / `.duration`) for `video_id`, or None.

    No transcript, captions disabled, region-blocked, the pip package not
    installed -- all of these are an expected miss for *some* videos, so they
    are logged and treated the same: skip this video, keep going.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import YouTubeTranscriptApiException
    except ImportError:
        print(
            "youtube collector: `pip install youtube-transcript-api` for transcripts",
            file=sys.stderr,
        )
        return None
    try:
        return list(YouTubeTranscriptApi().fetch(video_id, languages=languages))
    except YouTubeTranscriptApiException as e:
        print(f"youtube collector: no transcript for {video_id}: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001 -- network/parsing hiccups, same graceful skip
        print(f"youtube collector: transcript fetch failed for {video_id}: {e}", file=sys.stderr)
        return None


def _chunk_transcript(snippets, window_seconds, overlap_seconds):
    """Fixed time windows, sliding by (window - overlap), each window's text
    joined from every snippet that overlaps it at all.

    Not semantic chunking -- V0 keeps it simple -- but a 6-minute window
    sliding across a 90-minute transcript still leaves some window sitting
    almost entirely inside a strong segment, which is what scoring needs.
    """
    entries = [(s.start, s.start + s.duration, s.text) for s in snippets if s.text.strip()]
    if not entries:
        return []
    video_end = max(end for _, end, _ in entries)
    step = max(window_seconds - overlap_seconds, 1)

    chunks = []
    start = 0.0
    while start < video_end:
        end = start + window_seconds
        text = " ".join(text.strip() for s0, s1, text in entries if s0 < end and s1 > start)
        if text:
            chunks.append((start, min(end, video_end), text))
        start += step
    return chunks


def _to_item(video, start, end, text):
    start_i, end_i = int(start), int(round(end))
    title = f"{video['video_title']} — {_format_ts(start_i)}–{_format_ts(end_i)}"
    metadata = {
        "video_id": video["video_id"],
        "video_title": video["video_title"],
        "channel": video["channel"],
        "start_time": start_i,
        "end_time": end_i,
        "transcript": text,
        "video_url": video["video_url"],
    }
    if video.get("query"):
        metadata["query"] = video["query"]
    return CandidateItem(
        source="youtube",
        type="video_segment",
        title=title,
        # `t=<seconds>` is a real YouTube deep link (jumps playback there) and,
        # unlike a `#t=` fragment, survives normalize.canonical_url -- which
        # strips fragments -- so each segment keeps its own url_hash instead
        # of every segment of a video collapsing into one dedup match.
        url=f"{video['video_url']}&t={start_i}",
        text=text,
        author=video["channel"],
        published_at=video.get("published_at"),
        metadata=metadata,
        dedup_key=f"{video['video_id']}:{start_i}-{end_i}",
    )


def _format_ts(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
