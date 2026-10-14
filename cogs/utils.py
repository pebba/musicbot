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


# " - Remastered 2011", " - Single Version", " - Live" and similar version tags.
_VERSION_SUFFIX_RE = re.compile(
    r"\s+[-–—]\s+(?:\d{4}\s+)?(?:remaster(?:ed)?|single|album|radio|mono|stereo|explicit|clean|"
    r"original|extended|edit|version|mix|live|from|bonus|deluxe|acoustic|demo)\b.*$"
)


def song_key(title: str, artist: str = "") -> str:
    """Reduce a title to the song itself, so the album cut, the single, the
    remaster and the music video of one song compare equal."""
    t = (title or "").lower()
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)  # (Official Video), [Remastered] ...
    t = _VERSION_SUFFIX_RE.sub("", t)
    if " - " in t:
        # YouTube uploads are "Artist - Title", sometimes "Title - Artist".
        first, rest = t.split(" - ", 1)
        art = (artist or "").lower().removesuffix(" - topic")
        t = first if art and art in rest and art not in first else rest
    t = re.sub(r"\s(?:feat|ft)\.?\s.*$", "", t)
    t = " ".join(re.sub(r"[^\w\s]", "", t).split())
    return t or (title or "").lower().strip()


def artist_key(name: str) -> str:
    """Normalise an artist or channel name so 'Seiler und Speer - Topic',
    'BILDERBUCH' and 'TheHootersVEVO' compare with the plain artist name."""
    n = (name or "").lower().removesuffix(" - topic")
    n = re.sub(r"vevo$|\bofficial\b", "", n)
    return re.sub(r"[^\w]", "", n)


def _autoplay_title_ok(title: str) -> bool:
    return not _AUTOPLAY_SKIP_RE.search(title or "")


def _autoplay_artist_ok(expected: str, actual: str) -> bool:
    """True when one of the track's credited artists is the expected act.
    Compared per credited artist, so 'Taylor Swift, Seeb' passes for Taylor
    Swift while same-named acts don't: 'Wanda Jackson' for Wanda, 'C.R.O' for
    CRO, '3 Doors Down' for The Doors."""
    if not actual:
        return True  # no metadata — give it the benefit of the doubt
    stop = {"the", "a", "an", "and", "&", "feat", "ft", "x", "vs", "with", "of"}

    def sig(s):
        return {w.lower() for w in re.split(r"[\s/]+", s)
                if w.lower() not in stop and len(w) > 1}

    exp = sig(expected)
    if not exp:
        return True
    for credited in re.split(r",|&|\bfeat\.?|\bft\.?|\bx\b|\bvs\.?", actual, flags=re.IGNORECASE):
        act = sig(credited)
        if act and len(exp & act) / len(exp | act) >= 0.6:
            return True
    return False
