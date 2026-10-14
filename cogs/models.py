import asyncio
import re
import time
from collections import deque

import discord

from .constants import AUTOPLAY_HISTORY
from .utils import _extract_artist


class Track:
    __slots__ = (
        "stream_url", "title", "artist", "duration",
        "webpage_url", "thumbnail", "requester", "query",
    )

    def __init__(self, info: dict, requester: str, query: str | None = None):
        self.requester = requester
        self.query = query
        self.stream_url = info["url"]
        self.title = info.get("title", query or "Unknown")
        self.artist = _extract_artist(info)
        self.duration = info.get("duration", 0)
        self.webpage_url = info.get("webpage_url", "")
        self.thumbnail = info.get("thumbnail", "")

    @classmethod
    def lazy(cls, query: str, requester: str) -> "Track":
        """Create an unresolved track — stream URL will be fetched before playback."""
        t = cls.__new__(cls)
        t.requester = requester
        t.query = query
        t.stream_url = None
        t.title = query
        t.artist = ""
        t.duration = 0
        t.webpage_url = ""
        t.thumbnail = ""
        return t

    def resolve(self, info: dict):
        self.stream_url = info["url"]
        self.title = info.get("title", self.title)
        self.artist = _extract_artist(info)
        self.duration = info.get("duration", 0)
        self.webpage_url = info.get("webpage_url", "")
        self.thumbnail = info.get("thumbnail", "")

    @property
    def is_resolved(self) -> bool:
        return self.stream_url is not None

    def is_expired(self) -> bool:
        """True when the stream URL's own expiry (YouTube's run out after ~6 h)
        would hit before this track finishes playing."""
        if not self.stream_url:
            return False
        m = re.search(r"[?&/]expire[=/](\d+)", self.stream_url)
        if not m:
            return False
        return int(m.group(1)) < time.time() + (self.duration or 0) + 60


class GuildState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop_mode = "off"  # off, track, queue
        self.volume = 0.5
        self.start_time: float | None = None
        self.pause_start: float | None = None
        self.seek_offset: float = 0.0
        self.seek_to: float | None = None
        self.dj_role: int | None = None
        self.idle_ticks = 0
        self.channel: discord.TextChannel | None = None
        self.np_message: discord.Message | None = None
        self.np_view = None  # NowPlayingView | None
        self.radio_station: str | None = None
        self.autoplay: bool = False
        self.recent_songs: deque[str] = deque(maxlen=AUTOPLAY_HISTORY)  # song_key() of played tracks
        self.last_artist: str | None = None
        self.ytm_only: bool = False
        self._prefetch_task: asyncio.Task | None = None
        # Held while play_next picks and starts a track, so a /play during a
        # slow transition cannot start a second track on top of it.
        self.play_lock = asyncio.Lock()
        self.skip_requested = False
        self.fail_streak = 0  # tracks in a row that ended right after starting
        self.radio_failures = 0

    def position(self) -> float:
        if self.start_time is None:
            return 0.0
        # Freeze position while paused
        if self.pause_start is not None:
            return self.pause_start - self.start_time + self.seek_offset
        return time.time() - self.start_time + self.seek_offset
