import re
from datetime import timedelta

from .constants import _AUTOPLAY_SKIP_RE


def fmt_dur(seconds) -> str:
    if not seconds:
        return "Live"
    td = timedelta(seconds=int(seconds))
    return str(td)[2:] if seconds < 3600 else str(td)


def progress_bar(current, total, length=15) -> str:
    if not total:
        return "▬" * length
    pos = max(0, min(current, total))
    filled = int(length * pos / total)
    return "▬" * filled + "●" + "▬" * (length - filled)


def _extract_artist(info: dict) -> str:
    # yt-dlp provides 'artist' for music metadata, otherwise fall back to
    # the uploader/channel name with ' - Topic' stripped (YouTube auto-channels)
    if info.get("artist"):
        return info["artist"]
    uploader = info.get("uploader") or info.get("channel") or ""
    return uploader.removesuffix(" - Topic")


def _autoplay_title_ok(title: str) -> bool:
    return not _AUTOPLAY_SKIP_RE.search(title or "")


def _autoplay_artist_ok(expected: str, actual: str) -> bool:
    """Return False when the fetched track's artist is clearly a different act.
    Uses significant-word overlap so 'The Doors' won't pass for '3 Doors Down'."""
    if not actual:
        return True  # no metadata — give it the benefit of the doubt
    stop = {"the", "a", "an", "and", "&", "feat", "ft", "x", "vs", "with", "of"}

    def sig(s):
        return {w.lower() for w in re.split(r"[\s,/]+", s)
                if w.lower() not in stop and len(w) > 1}

    exp = sig(expected)
    act = sig(actual)
    if not exp:
        return True
    return len(exp & act) / len(exp) > 0.5
