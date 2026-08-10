"""YouTube videos, chunked into transcript segments.

A whole video is the wrong unit: a 90-minute podcast can be mostly filler
around one excellent 6-minute discussion, so this collector doesn't ask "is
this video relevant" -- it fetches each video's transcript, splits it into
fixed, overlapping time windows, and emits one CandidateItem *per segment*.
Everything downstream (matching, pre-filter, LLM scoring) already runs per
item, so "keep only strong segments" falls straight out of the existing
threshold -- no video-level judgement needed here.

Collection is LLM-first, in three stages, each spending a different scarce
resource as late as possible:

  1. Discovery (0 API quota): one `provider.search_json` conversation per
     interest finds candidate videos on the open web. Keyword `search.list`
     was retired -- it cost 100 quota units per query and most hand-written
     queries returned nothing; the model searching iteratively (and reading
     results between queries) finds better videos for free.
  2. Verify + enrich (1 quota unit per 50 ids): ONE batched `videos.list`
     call confirms each id actually exists and is recent -- the model's
     recency claim is never trusted -- and supplies the real title,
     description, channel, duration and publishedAt.
  3. Transcript fetch (IP-rationed): youtube-transcript-api is unofficial and
     soft-blocks bursting IPs, so fetches are ranked (LLM estimate first,
     keyword match as tie-break -- the model rates, code ranks), capped by
     `max_transcript_fetches`, paced by `FETCH_SLEEP_SECONDS`, and a block
     error trips a circuit breaker for the rest of the cycle -- it stops
     further *fetch attempts*, not the loop itself (see below).

Graceful degradation: a transcript miss -- no captions, breaker-tripped, or
simply ranked below the fetch budget -- no longer means the video is
discarded. Stage 1/2 already spent a discovery conversation and a quota unit
verifying it, so instead of a per-segment CandidateItem the collector emits
one video-level item (title + description, `type="video"`,
`dedup_key="<video_id>:video"`). The existing seen-prefix check (any stored
row for a video, segment or video-level) means a video is processed once, at
whatever fidelity was available that day -- a video-level row is never
retroactively upgraded to segments once transcripts come back.

V0 chunking is a fixed window with overlap (`chunk_seconds` /
`chunk_overlap_seconds`), not semantic segmentation -- simple, and a window
sliding across a topic change still leaves some window mostly inside it.
"""
import html
import json
import re
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..db import seen_dedup_keys
from ..matching import _match_one
from ..models import CandidateItem, clamp01
from ..providers.base import ProviderError

VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

DEFAULT_MAX_CANDIDATE_VIDEOS = 10
DEFAULT_MAX_TRANSCRIPT_FETCHES = 4
DEFAULT_RECENCY_DAYS = 14
DEFAULT_WINDOW_SECONDS = 360    # 6 minutes -- the length of the motivating example
DEFAULT_OVERLAP_SECONDS = 60
DEFAULT_LANGUAGES = ["en"]

# Pause between transcript fetches. Module-level so tests can zero it.
FETCH_SLEEP_SECONDS = 2.0

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

PROMPT = """\
Find YouTube videos from the last {recency_days} days likely to contain deep,
specific content on this interest.

Interest: {title}
Description: {description}
Worth surfacing: {positive}
Not worth surfacing: {negative}

Search iteratively -- site:youtube.com queries and the open web both count
(blogs, Reddit threads, conference pages that point at videos are all fair
game). Refine your queries as you learn from each search's results, and stop
early if this looks like a quiet period.
{query_hints}
Return AT MOST {max_candidates} entries as a JSON array, one object per video:
[{{"url": "<YouTube video URL>",
   "estimate": <relevance estimate, 0-1>,
   "why": "<one line: why this video>",
   "where": "<which part of the video is likely relevant, if known>"}}]

Return [] if nothing recent and substantive turned up. Output only the JSON
array, no other text.
"""


class YoutubeCollectorError(Exception):
    """A setup problem (no API key, a hard API failure), not a per-video miss.

    Raised, not swallowed: `collect()` lets it propagate so the pipeline's
    per-collector isolation (see collectors/__init__.py) logs it once instead
    of this module silently returning nothing.
    """


class TranscriptBlocked(Exception):
    """The transcript endpoint is refusing this IP (RequestBlocked/IpBlocked).

    Distinct from a per-video miss: one of these means every further fetch
    this cycle would also fail AND prolong the block, so `collect()` stops
    fetching entirely and lets the unfetched videos retry next cycle.
    """


def collect(interest, cfg, provider, conn=None):
    opts = interest.source_config.get("youtube", {})

    api_key = getattr(cfg, "youtube_api_key", "") or ""
    if not api_key:
        raise YoutubeCollectorError("YOUTUBE_API_KEY not set")

    max_candidates = int(opts.get("max_candidate_videos", DEFAULT_MAX_CANDIDATE_VIDEOS))
    fetch_budget = int(opts.get("max_transcript_fetches", DEFAULT_MAX_TRANSCRIPT_FETCHES))
    recency_days = int(opts.get("recency_days", DEFAULT_RECENCY_DAYS))
    window_seconds = int(opts.get("chunk_seconds", DEFAULT_WINDOW_SECONDS))
    overlap_seconds = int(opts.get("chunk_overlap_seconds", DEFAULT_OVERLAP_SECONDS))
    languages = opts.get("languages", DEFAULT_LANGUAGES)
    limit = int(opts.get("limit", cfg.max_items_per_source))
    queries = [str(q).strip() for q in opts.get("queries", []) if str(q).strip()]
    query_hints = ""
    if queries:
        query_hints = (
            "\nStarting-point searches (owner-suggested; refine freely and go "
            "beyond them):\n" + "\n".join(f"- {q}" for q in queries) + "\n"
        )

    # Stage 1: discovery. No fallback path -- a failed conversation is a
    # logged skip and the next cycle simply tries again.
    if provider is None:
        print(f"youtube collector: no provider for discovery ({interest.key})", file=sys.stderr)
        return []
    try:
        raw = provider.search_json(
            PROMPT.format(
                recency_days=recency_days,
                title=interest.title,
                description=interest.description,
                positive=", ".join(interest.positive_signals) or "(unspecified)",
                negative=", ".join(interest.negative_signals) or "(unspecified)",
                max_candidates=max_candidates,
                query_hints=query_hints,
            )
        )
    except ProviderError as e:
        print(f"youtube collector: discovery failed ({interest.key}): {e}", file=sys.stderr)
        return []

    candidates, ids = [], set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        video_id = _video_id_from_url(str(entry.get("url") or ""))
        if not video_id or video_id in ids:
            continue
        ids.add(video_id)
        candidates.append(
            {
                "video_id": video_id,
                "discovery_estimate": _estimate(entry.get("estimate")),
                "discovery_why": str(entry.get("why") or "")[:300],
            }
        )
        if len(candidates) >= max_candidates:
            break

    # Seen check BEFORE the videos.list spend: every stored segment's
    # dedup_key starts with "<video_id>:", so one cheap lookup skips a
    # re-discovered video before any quota or transcript request.
    unseen = [
        c for c in candidates
        if conn is None or not seen_dedup_keys(conn, "youtube", c["video_id"] + ":")
    ]
    if not unseen:
        return []

    # Stage 2: verify + enrich. The model's ids and recency claims are not
    # trusted -- videos.list is authoritative for existence and publishedAt.
    listed = _list_videos(api_key, [c["video_id"] for c in unseen])
    cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
    videos, dead, stale = [], 0, 0
    for cand in unseen:
        video = listed.get(cand["video_id"])
        if video is None:
            dead += 1
            continue
        published = _parse_timestamp(video.get("published_at"))
        if published is None or published < cutoff:
            stale += 1
            continue
        videos.append({**video, **cand})
    print(
        f"youtube collector: {interest.key}: {len(candidates)} discovered, "
        f"{len(candidates) - len(unseen)} seen, {dead} dead, {stale} stale, "
        f"{len(videos)} verified",
        file=sys.stderr,
    )

    # Stage 3: the model rates, code ranks. LLM estimate first, keyword match
    # against the interest as tie-break, then spend the fetch budget top-down.
    def rank(video):
        probe = types.SimpleNamespace(title=video["video_title"], text=video["description"])
        match_score, _ = _match_one(probe, interest)
        return (-video["discovery_estimate"], -match_score)

    videos.sort(key=rank)

    # A transcript is the best fidelity, but not having one is no longer a
    # reason to drop the video: Stage 1/2 already spent a discovery
    # conversation and a quota unit on it, so a transcript miss (no captions,
    # breaker-tripped, or simply ranked below the fetch budget) degrades to a
    # single video-level item (title + description) instead of discarding
    # that work. Each video is processed once, at the best fidelity available
    # that day -- see module docstring.
    items, fetches, fetching = [], 0, True
    for video in videos:
        # The limit is checked at video boundaries, not mid-video: once a
        # video's segments are stored, the seen-prefix skip above never
        # fetches it again, so cutting a video off halfway would permanently
        # drop its later segments. Emitting a whole video may overshoot the
        # limit by a few segments; the score budget bounds what that can cost.
        if len(items) >= limit:
            break

        transcript = None
        if fetching and fetches < fetch_budget:
            if fetches:
                time.sleep(FETCH_SLEEP_SECONDS)
            fetches += 1
            try:
                transcript = _fetch_transcript(video["video_id"], languages)
            except TranscriptBlocked as e:
                # Stop *fetching* -- every later fetch this cycle would also
                # fail and prolong the block -- but keep emitting video-level
                # items for the rest of the ranked list; that costs nothing.
                print(
                    f"youtube collector: transcript endpoint blocked, "
                    f"stopping fetches this cycle: {e}",
                    file=sys.stderr,
                )
                fetching = False

        if transcript:
            for start, end, text in _chunk_transcript(transcript, window_seconds, overlap_seconds):
                items.append(_to_item(video, start, end, text))
        else:
            items.append(_to_video_item(video))
    return items


# --- discovery helpers -------------------------------------------------------

def _video_id_from_url(url):
    """The 11-char video id, or None for anything that isn't a video URL."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    segments = [s for s in parsed.path.split("/") if s]
    if host == "youtu.be":
        video_id = segments[0] if segments else ""
    elif host == "youtube.com":
        if parsed.path.startswith("/watch"):
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        elif segments and segments[0] in ("shorts", "live", "embed"):
            video_id = segments[1] if len(segments) > 1 else ""
        else:
            return None
    else:
        return None
    return video_id if _VIDEO_ID_RE.fullmatch(video_id) else None


def _estimate(value):
    try:
        return clamp01(float(value))
    except (TypeError, ValueError):
        return 0.0


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- verification (videos.list) ----------------------------------------------

def _list_videos(api_key, video_ids):
    """id -> enriched video dict, via batched videos.list (1 unit per ≤50 ids).

    Ids missing from the response are simply absent from the result --
    hallucinated, deleted, or private videos all look the same and the caller
    drops them the same way.
    """
    found = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        params = {
            "key": api_key,
            "part": "snippet,contentDetails",
            "id": ",".join(batch),
            "maxResults": 50,
        }
        url = VIDEOS_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise YoutubeCollectorError(f"videos.list failed: {e}") from e
        for entry in payload.get("items", []):
            video_id = entry.get("id")
            if not video_id:
                continue
            snippet = entry.get("snippet", {})
            # API snippet strings can arrive HTML-escaped ("Let&#39;s",
            # "&amp;"); unescape here or the entities end up verbatim in
            # titles and Telegram.
            found[video_id] = {
                "video_id": video_id,
                "video_title": html.unescape(snippet.get("title") or "") or video_id,
                "channel": html.unescape(snippet.get("channelTitle") or ""),
                "description": snippet.get("description") or "",
                "published_at": snippet.get("publishedAt"),
                "duration_seconds": _duration_seconds(
                    entry.get("contentDetails", {}).get("duration")
                ),
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
            }
    return found


def _duration_seconds(iso_duration):
    match = _DURATION_RE.fullmatch(iso_duration or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# --- transcript + chunking ----------------------------------------------------

def _is_block_error(e):
    """youtube-transcript-api's RequestBlocked/IpBlocked, matched by class
    name so this file never imports the optional package at module level."""
    return any(cls.__name__ in ("RequestBlocked", "IpBlocked") for cls in type(e).__mro__)


def _fetch_transcript(video_id, languages):
    """Snippet list (`.text` / `.start` / `.duration`) for `video_id`, or None.

    No transcript, captions disabled, region-blocked, the pip package not
    installed -- all of these are an expected miss for *some* videos, so they
    are logged and treated the same: skip this video, keep going. An IP block
    is different -- it dooms every later fetch too -- so it raises
    TranscriptBlocked for the caller's circuit breaker.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            YouTubeTranscriptApiException,
        )
    except ImportError:
        print(
            "youtube collector: `pip install youtube-transcript-api` for transcripts",
            file=sys.stderr,
        )
        return None
    api = YouTubeTranscriptApi()
    try:
        return list(api.fetch(video_id, languages=languages))
    except NoTranscriptFound:
        # The library matches codes exactly, so languages=["en"] misses a video
        # whose only English track is "en-US". Retry by base language.
        try:
            bases = [lang.split("-")[0] for lang in languages]
            for transcript in api.list(video_id):
                if transcript.language_code.split("-")[0] in bases:
                    return list(transcript.fetch())
            print(f"youtube collector: no transcript for {video_id} in {languages}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 -- same graceful skip as below
            if _is_block_error(e):
                raise TranscriptBlocked(str(e)) from e
            print(f"youtube collector: no transcript for {video_id}: {e}", file=sys.stderr)
        return None
    except YouTubeTranscriptApiException as e:
        if _is_block_error(e):
            raise TranscriptBlocked(str(e)) from e
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
    if "discovery_estimate" in video:
        # Provenance for calibrating Stage-1 discovery against final segment
        # scores -- did the model put the right videos into the fetch budget?
        metadata["discovery_estimate"] = video["discovery_estimate"]
        metadata["discovery_why"] = video["discovery_why"]
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


def _to_video_item(video):
    """Video-level fallback for a transcript miss: title + description is
    thinner than a transcript segment, but it's real, verified (Stage 2)
    content -- worth scoring rather than silently dropping."""
    metadata = {
        "video_id": video["video_id"],
        "video_title": video["video_title"],
        "channel": video["channel"],
        "duration_seconds": video.get("duration_seconds", 0),
        "video_url": video["video_url"],
    }
    if "discovery_estimate" in video:
        metadata["discovery_estimate"] = video["discovery_estimate"]
        metadata["discovery_why"] = video["discovery_why"]
    return CandidateItem(
        source="youtube",
        type="video",
        title=video["video_title"],
        # No `&t=`: there is no segment to jump to, and unlike _to_item's
        # per-segment urls, every fallback for this video must share one
        # url/dedup_key -- the video is either segmented or video-level,
        # never both.
        url=video["video_url"],
        text=f"{video['video_title']}\n\n{video['description']}".strip(),
        author=video["channel"],
        published_at=video.get("published_at"),
        metadata=metadata,
        dedup_key=f"{video['video_id']}:video",
    )


def _format_ts(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
