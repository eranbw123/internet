"""YouTube videos + transcripts. Not implemented yet.

Planned shape, so the interface stays honest in the meantime: read
`source_config["youtube"]` for channel ids and/or search queries, pull recent
uploads from the YouTube Data API (needs YOUTUBE_API_KEY), fetch each
video's transcript, and emit one CandidateItem per video with the transcript
as `text` and type "video". Dedup on the video URL, which is stable.
"""


def collect(interest, cfg, provider=None):
    return []
