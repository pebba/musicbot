import json
import os
import re

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "noplaylist": True,
}

# YouTube Music search "Songs" tab filter — filters out videos, albums, artists, playlists
YTM_SONGS_SP = "EgWKAQIIAWoKEAoQAxAEEAkQBQ%3D%3D"

# Autoplay: max songs a track must "wait" before it's fully available again.
# Scales down automatically for small artist pools (see _autoplay_next).
AUTOPLAY_MAX_COOLDOWN = 10

# Autoplay title pre-filter: skip live recordings, karaoke, covers, etc.
_AUTOPLAY_SKIP_RE = re.compile(
    r'\blive\s+(?:at|in|from|@)\b'       # "live at/in/from/@ …"
    r'|\(\s*live\s*[\),]'                # "(live)" / "(live,"
    r'|\[\s*live\s*[\],]'                # "[live]" / "[live,"
    r'|[-–—]\s*live\s*$'                 # "— live" at end of title
    r'|\bkaraoke\b'                      # any karaoke
    r'|\boriginally\s+performed\s+by\b'  # "originally performed by …"
    r'|\btribute\s+(?:to|band)\b'        # "tribute to" / "tribute band"
    r'|\b(?:guitar|piano|drums?)\s+cover\b'  # instrument cover
    r'|\binstrumental\b'                 # instrumental (no vocals) version
    r'|\bacoustic\b',                    # acoustic version/session
    re.IGNORECASE
)

# Autoplay title deprioritize: alternate/non-standard versions get weight × 0.1 so they
# rarely play but aren't completely excluded when the candidate pool is thin.
_AUTOPLAY_DEPRIORITIZE_RE = re.compile(
    r'\bakustik\b'           # German "acoustic" (e.g. "Akustik Version")
    r'|\bpiano\s*version\b'  # "Piano Version" / "PianoVersion"
    r'|\bunplugged\b'        # unplugged sessions
    r'|\bstripped\b'         # stripped-back versions
    r'|\bslowed\b'           # slowed/reverb edits
    r'|\bnightcore\b'        # nightcore speed-ups
    r'|\bsped\s*up\b'        # sped-up versions
    r'|\bradio\s+edit\b',    # radio edits (shortened)
    re.IGNORECASE
)

FFMPEG_RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

_STATIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "radio_stations.json")
try:
    with open(_STATIONS_FILE, encoding="utf-8") as _f:
        RADIO_STATIONS: dict[str, str] = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    print(f"[musicbot] Warning: could not load radio_stations.json: {e}")
    RADIO_STATIONS: dict[str, str] = {}

PLAYLISTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "playlists")
