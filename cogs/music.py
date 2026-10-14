import asyncio
import json
import os
import random
import re
import time
import traceback
import urllib.parse
from collections import deque

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    _SPOTIPY_AVAILABLE = True
except ImportError:
    _SPOTIPY_AVAILABLE = False

from .constants import (
    AUTOPLAY_DEPTH,
    BUSY_FILE,
    FFMPEG_RECONNECT,
    PLAYLISTS_DIR,
    RADIO_STATIONS,
    SETTINGS_FILE,
    YTDL_OPTIONS,
    YTM_SONGS_SP,
    _AUTOPLAY_DEPRIORITIZE_RE,
)
from .models import GuildState, Track
from .utils import _autoplay_artist_ok, _autoplay_title_ok, _extract_artist, fmt_dur, song_key
from .views import NowPlayingView, SearchView, build_np_embed, build_queue_embed


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.settings: dict[str, dict] = self._load_settings()
        self._ytm_cache: dict[str, tuple[float, list[dict]]] = {}
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        self._idle_check.start()
        self._progress_update.start()

    def cog_unload(self):
        self._idle_check.cancel()
        self._progress_update.cancel()

    def get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            state = GuildState()
            saved = self.settings.get(str(guild_id), {})
            state.dj_role = saved.get("dj_role")
            state.ytm_only = saved.get("ytm_only", False)
            state.volume = saved.get("volume", state.volume)
            state.autoplay = saved.get("autoplay", False)
            self.states[guild_id] = state
        return self.states[guild_id]

    @staticmethod
    def _load_settings() -> dict[str, dict]:
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_setting(self, guild_id: int, key: str, value):
        self.settings.setdefault(str(guild_id), {})[key] = value
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f)
        os.replace(tmp, SETTINGS_FILE)

    def _is_dj(self, interaction: discord.Interaction) -> bool:
        state = self.get_state(interaction.guild_id)
        if state.dj_role is None:
            return True
        # Server managers always pass, so setting a role cannot lock them out.
        if interaction.user.guild_permissions.manage_guild:
            return True
        role = interaction.guild.get_role(state.dj_role)
        return role in interaction.user.roles if role else True

    async def _deny_non_dj(self, interaction: discord.Interaction) -> bool:
        """Tell non-DJs off and return True; return False when they may proceed."""
        if self._is_dj(interaction):
            return False
        msg = "You need the DJ role to use this."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        # The tree logs the traceback; this only makes sure the user is not left
        # staring at "thinking..." forever.
        msg = "⚠️ Something went wrong — please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # --- Shared helpers ---

    def _schedule(self, coro):
        """Run a coroutine from a voice player thread and log it if it fails —
        otherwise the exception vanishes with the future and playback just stops."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
        fut.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(fut):
        if not fut.cancelled() and fut.exception():
            print("[musicbot] Background playback error:")
            traceback.print_exception(fut.exception())

    async def _notify(self, state: GuildState, text: str):
        if not state.channel:
            return
        try:
            await state.channel.send(text)
        except discord.HTTPException as e:
            print(f"[musicbot] Could not send to channel: {e}")

    async def _retire_np(self, state: GuildState, delete: bool = False):
        """Retire the last now-playing message: deleted when a new one replaces
        it (keeps the channel from filling up), otherwise just stripped of its
        buttons so it stays as a record of what played last."""
        view, msg = state.np_view, state.np_message
        state.np_view = None
        state.np_message = None
        if view:
            view.stop()
        if not msg:
            return
        try:
            if delete:
                await msg.delete()
            elif view:
                await msg.edit(view=None)
        except discord.HTTPException:
            pass

    def _clear_playback(self, state: GuildState):
        self._cancel_prefetch(state)
        if state._preload:
            state._preload[1].cancel()
            state._preload = None
        state.queue.clear()
        state.current = None
        state.radio_station = None
        state.skip_requested = False

    def _end_radio(self, state: GuildState, vc: discord.VoiceClient | None):
        """Hand the voice channel over from radio to the queue (/play while radio runs)."""
        if state.radio_station:
            state.radio_station = None
            state.radio_failures = 0
            if vc:
                vc.stop()

    async def _resolve(self, track: Track, ytm_only: bool) -> bool:
        query = track.webpage_url or track.query
        info = await self._fetch_track(query, ytm_only=ytm_only) if query else None
        if info:
            track.resolve(info)
        return bool(info)

    def _start_preload(self, state: GuildState):
        """Resolve the next queued track in the background while this one plays.
        Spotify, playlist and saved-playlist tracks are only search terms until
        then, which otherwise leaves a few seconds of silence before each one."""
        if not state.queue or state.radio_station:
            return
        nxt = state.queue[0]
        if nxt.is_resolved and not nxt.is_expired():
            return
        if state._preload and state._preload[0] is nxt:
            return  # one background attempt per track; the transition retries if needed
        state._preload = (nxt, asyncio.create_task(self._resolve(nxt, state.ytm_only)))

    @staticmethod
    def _mark_busy(busy: bool):
        try:
            if busy:
                with open(BUSY_FILE, "w") as f:
                    f.write(str(int(time.time())))
            elif os.path.exists(BUSY_FILE):
                os.remove(BUSY_FILE)
        except OSError as e:
            print(f"[musicbot] Could not update {BUSY_FILE}: {e}")

    def _skip(self, state: GuildState, vc: discord.VoiceClient, to: int = 1):
        skipped = [state.queue.popleft() for _ in range(min(max(0, to - 1), len(state.queue)))]
        if not state.radio_station:
            if state.loop_mode == "queue":
                # Keep the loop intact: current and skipped tracks go round again.
                if state.current:
                    state.queue.append(state.current)
                state.queue.extend(skipped)
            state.skip_requested = True
        vc.stop()

    # --- Playback core ---

    def _build_ffmpeg_opts(self, seek: float = 0) -> dict:
        before = FFMPEG_RECONNECT
        if seek > 0:
            before = f"-ss {seek:.2f} " + before
        return {"before_options": before, "options": "-vn"}

    async def _play_track(self, guild_id: int, track: Track, state: GuildState,
                          seek: float = 0, announce: bool = True):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc:
            return

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, **self._build_ffmpeg_opts(seek=seek)),
            volume=state.volume,
        )
        state.start_time = time.time()
        state.pause_start = None
        state.seek_offset = seek
        state.seek_to = None

        def after(error):
            if error:
                print(f"[musicbot] Player error: {error}")
            self._schedule(self.play_next(guild_id))

        vc.play(source, after=after)

        key = song_key(track.title, track.artist)
        if key and (not state.recent_songs or state.recent_songs[-1] != key):
            state.recent_songs.append(key)

        self._start_prefetch(guild_id, state)
        self._start_preload(state)
        self._mark_busy(True)

        if announce and state.channel:
            await self._retire_np(state, delete=True)
            view = NowPlayingView(self, guild_id)
            try:
                state.np_message = await state.channel.send(embed=build_np_embed(state, track), view=view)
                state.np_view = view
            except discord.HTTPException as e:
                print(f"[musicbot] Could not post now-playing message: {e}")
                view.stop()

    async def play_next(self, guild_id: int):
        state = self.get_state(guild_id)
        async with state.play_lock:
            await self._play_next_locked(guild_id, state)

    async def _play_next_locked(self, guild_id: int, state: GuildState):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc:
            return
        # A /play that raced this transition already started something.
        if vc.is_playing() or vc.is_paused():
            return

        skipping = state.skip_requested
        state.skip_requested = False

        # Pending seek: replay current track from a new position
        if state.seek_to is not None and state.current:
            seek_pos = state.seek_to
            state.seek_to = None
            await self._play_track(guild_id, state.current, state, seek=seek_pos, announce=False)
            return

        # A track that ends right after starting did not play at all (dead or
        # expired stream). Stop after a few in a row instead of cycling through
        # them and posting a now-playing message for each.
        if state.current and state.start_time and not skipping:
            if time.time() - state.start_time < 2:
                state.fail_streak += 1
            else:
                state.fail_streak = 0
            if state.fail_streak >= 5:
                state.fail_streak = 0
                state.current = None
                state.start_time = None
                await self._retire_np(state)
                await self._notify(state, "⚠️ Several tracks in a row failed to play — stopped. "
                                          "Use `/play` to try again.")
                return

        if state.loop_mode == "track" and state.current and not skipping:
            next_track = state.current
        else:
            if state.loop_mode == "queue" and state.current and not skipping:
                state.queue.append(state.current)
            if state.queue:
                next_track = state.queue.popleft()
            else:
                if state.autoplay and state.current and not state.radio_station:
                    task = state._prefetch_task
                    state._prefetch_task = None
                    if task and not task.done():
                        # Pre-fetch is still running — wait for it. Starting over
                        # would only redo the same work, so be patient.
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                            task.cancel()
                    if not state.queue:
                        # Pre-fetch failed or was cancelled — fall back to a synchronous fetch
                        fetched = await self._autoplay_next(state)
                        if not fetched:
                            state.current = None
                            state.start_time = None
                            await self._retire_np(state)
                            return
                    next_track = state.queue.popleft()
                else:
                    state.current = None
                    state.start_time = None
                    await self._retire_np(state)
                    return

        # Resolve lazy tracks, and re-resolve looped ones whose stream URL expired.
        while True:
            state.current = next_track
            if next_track.is_resolved and not next_track.is_expired():
                break
            pre = state._preload
            if pre and pre[0] is next_track and not pre[1].done():
                # Already being resolved in the background — let that finish.
                state._preload = None
                try:
                    await asyncio.wait_for(asyncio.shield(pre[1]), timeout=30.0)
                except Exception:
                    pass
                if next_track.is_resolved and not next_track.is_expired():
                    break
            if await self._resolve(next_track, state.ytm_only):
                break
            await self._notify(state, f"⚠️ Couldn't find **{next_track.title}**, skipping...")
            state.current = None
            if not state.queue:
                state.start_time = None
                await self._retire_np(state)
                return
            next_track = state.queue.popleft()

        await self._play_track(guild_id, next_track, state, announce=True)

    # --- yt-dlp helpers ---

    async def _fetch_info(self, query: str, playlist: bool = False) -> dict:
        opts = {**YTDL_OPTIONS, "noplaylist": not playlist}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            return await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))

    async def _fetch_track(self, query: str, ytm_only: bool = False) -> dict | None:
        if query.startswith("http"):
            try:
                info = await self._fetch_info(query)
                if "entries" in info:
                    info = info["entries"][0]
                return info
            except Exception as e:
                print(f"[musicbot] Fetch error: {e}")
                return None
        return await self._fetch_best_audio(query, ytm_only=ytm_only)

    async def _fetch_best_audio(self, query: str, ytm_only: bool = False) -> dict | None:
        """Search YouTube Music for the query and return the first song result.
        Falls back to regular YouTube search unless ytm_only is True."""
        loop = asyncio.get_running_loop()
        opts = {**YTDL_OPTIONS, "extract_flat": True}

        # YouTube Music "Songs" tab search — sp filter restricts to songs only
        ytm_url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}&sp={YTM_SONGS_SP}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                results = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(ytm_url, download=False)
                )
            songs = [e for e in (results.get("entries") or [])
                     if e.get("ie_key") == "Youtube" and e.get("id")]
            if songs:
                ytm_watch = (f"https://music.youtube.com/watch?v={songs[0]['id']}"
                             if songs[0].get("id") else songs[0]["url"])
                info = await self._fetch_info(ytm_watch)
                if "entries" in info:
                    info = info["entries"][0]
                return info
        except Exception as e:
            print(f"[musicbot] YTM search error: {e}")

        if ytm_only:
            return None

        # Fallback: regular YouTube search
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                results = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(f"ytsearch:{query}", download=False)
                )
            entries = results.get("entries") or []
            if entries:
                info = await self._fetch_info(entries[0]["url"])
                if "entries" in info:
                    info = info["entries"][0]
                return info
        except Exception as e:
            print(f"[musicbot] YT fallback error: {e}")

        return None

    async def _search(self, query: str, count: int = 10, ytm_only: bool = False) -> list[dict]:
        opts = {**YTDL_OPTIONS, "extract_flat": True}
        loop = asyncio.get_running_loop()
        if ytm_only:
            ytm_url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}&sp={YTM_SONGS_SP}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(ytm_url, download=False)
                )
            entries = [e for e in (info.get("entries") or [])
                       if e.get("ie_key") == "Youtube" and e.get("id")]
            return entries[:count]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            )
        return info.get("entries", [])

    # --- Spotify helpers ---

    def _spotify_client(self):
        if not _SPOTIPY_AVAILABLE:
            return None
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None
        if not hasattr(self, "_sp"):
            self._sp = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=client_id, client_secret=client_secret,
                ),
                requests_timeout=10,
            )
        return self._sp

    async def _spotify_queries(self, url: str) -> list[str]:
        sp = self._spotify_client()
        if not sp:
            raise ValueError(
                "Spotify credentials not configured. "
                "Add `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` to your .env file."
            )
        loop = asyncio.get_running_loop()

        def _track_query(t: dict) -> str:
            artists = ", ".join(a["name"] for a in t["artists"])
            return f"{artists} - {t['name']}"

        if "/track/" in url:
            track = await loop.run_in_executor(None, lambda: sp.track(url))
            return [_track_query(track)]

        if "/album/" in url:
            def _all_album_tracks():
                page = sp.album_tracks(url)
                items = list(page["items"])
                while page.get("next"):  # the API pages at 50 tracks
                    page = sp.next(page)
                    items.extend(page["items"])
                return items
            items = await loop.run_in_executor(None, _all_album_tracks)
            return [_track_query(item) for item in items if item]

        if "/playlist/" in url:
            # Spotify's API blocks playlist access for Client Credentials since 2024.
            # Use yt-dlp's built-in Spotify extractor instead — it uses Spotify's
            # internal web token and doesn't need user OAuth.
            opts = {**YTDL_OPTIONS, "extract_flat": True, "noplaylist": False, "quiet": True}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = await loop.run_in_executor(
                        None, lambda: ydl.extract_info(url, download=False)
                    )
                queries = []
                for e in (info.get("entries") or []):
                    title = e.get("title") or ""
                    artist = (e.get("artist") or
                              ", ".join(e.get("artists") or []) or
                              e.get("uploader") or "")
                    queries.append(f"{artist} - {title}" if artist and title else title)
                queries = [q for q in queries if q]
                if queries:
                    return queries
            except Exception as ex:
                print(f"[musicbot] yt-dlp Spotify playlist error: {ex}")
            raise ValueError(
                "Could not load Spotify playlist. Spotify's API now requires user login for playlists. "
                "Try individual track links or a YouTube playlist instead."
            )

        raise ValueError("Unsupported Spotify URL. Use a track, album, or playlist link.")

    # --- Autoplay ---

    async def _prefetch_autoplay(self, guild_id: int):
        """Background task: run _autoplay_next while the current song is still playing."""
        state = self.get_state(guild_id)
        try:
            await self._autoplay_next(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[musicbot] Prefetch error: {e}")

    def _start_prefetch(self, guild_id: int, state: GuildState):
        """Pick the next autoplay track in the background while this one plays,
        so it is ready the moment the track ends."""
        if state.autoplay and state.current and not state.queue and not state.radio_station:
            self._cancel_prefetch(state)
            state._prefetch_task = asyncio.create_task(self._prefetch_autoplay(guild_id))

    def _set_autoplay(self, guild_id: int, state: GuildState, on: bool):
        state.autoplay = on
        self._save_setting(guild_id, "autoplay", on)
        if on:
            self._start_prefetch(guild_id, state)  # don't wait for the next track to start
        else:
            self._interrupt_autoplay(state)  # drop an already queued autoplay pick

    def _cancel_prefetch(self, state: GuildState):
        if state._prefetch_task and not state._prefetch_task.done():
            state._prefetch_task.cancel()
        state._prefetch_task = None

    def _interrupt_autoplay(self, state: GuildState):
        """Cancel prefetch and remove any autoplay-queued tracks so user's addition plays next."""
        self._cancel_prefetch(state)
        state.queue = deque(t for t in state.queue if t.requester != "Autoplay 🎲")

    async def _autoplay_next(self, state: GuildState) -> bool:
        """Queue one well-known song by the same artist via YTM that wasn't played recently."""
        track = state.current
        if not track:
            return False

        # Prefer artist parsed from title ("Artist - Song" format) over uploader-based artist,
        # because regular YouTube videos often have the channel name as uploader, not the real artist.
        artist_source = track.artist
        if track.title and " - " in track.title:
            title_part = track.title.split(" - ", 1)[0].strip()
            for sep in (" feat.", " Feat.", " ft.", " Ft."):
                if sep in title_part:
                    title_part = title_part.split(sep)[0].strip()
                    break
            if title_part:
                artist_source = title_part

        if not artist_source:
            return False

        # Prefer last_artist to stay consistent within an autoplay chain
        artists = [a.strip() for a in artist_source.split(",")]
        if state.last_artist and state.last_artist in artists:
            artist = state.last_artist
        else:
            artist = random.choice(artists) if artists else None
        if not artist:
            return False

        current_id = ""
        if "v=" in track.webpage_url:
            current_id = track.webpage_url.split("v=")[-1].split("&")[0]

        entries = await self._ytm_song_search(artist)
        if not entries:
            return False

        # YTM search entries carry only id and title — no views, duration or artist.
        # Their order is the ranking signal: the top is what the artist is known
        # for, the tail is deep cuts. Keep one entry per song, best-ranked first.
        current_key = song_key(track.title, track.artist)
        seen: set[str] = set()
        ranked = []
        for e in entries:
            if e.get("ie_key") != "Youtube" or not e.get("id") or e["id"] == current_id:
                continue
            if not _autoplay_title_ok(e.get("title", "")):
                continue
            key = song_key(e.get("title", ""), artist)
            if key in seen:
                continue
            seen.add(key)
            ranked.append((len(ranked), e, key))
            if len(ranked) >= AUTOPLAY_DEPTH:
                break
        if not ranked:
            return False

        recent = list(state.recent_songs)
        pool = [c for c in ranked if c[2] not in recent and c[2] != current_key]
        avoid = set(recent) | {current_key}
        if not pool:
            # Every well-known song was played recently: repeat the ones heard
            # longest ago rather than digging into the obscure tail.
            last_played = {k: i for i, k in enumerate(recent)}
            pool = sorted((c for c in ranked if c[2] != current_key),
                          key=lambda c: last_played.get(c[2], -1))[:3]
            avoid = {current_key}

        # Steep rank decay (1.0, 0.8, 0.64 ... 0.04 at #15) so hits dominate;
        # alternate versions (acoustic, slowed, ...) get a further × 0.1.
        weights = [
            0.8 ** rank * (0.1 if _AUTOPLAY_DEPRIORITIZE_RE.search(e.get("title", "")) else 1.0)
            for rank, e, _ in pool
        ]

        # Each try is a full extraction, so cap them to keep the gap short.
        for _ in range(min(4, len(pool))):
            i = random.choices(range(len(pool)), weights=weights, k=1)[0]
            _, chosen, _ = pool.pop(i)
            weights.pop(i)
            try:
                info = await self._fetch_info(f"https://music.youtube.com/watch?v={chosen['id']}")
                if "entries" in info:
                    info = info["entries"][0]
            except Exception as e:
                print(f"[musicbot] Autoplay fetch error: {e}")
                continue
            # Only the full info has these: the real title ("live" etc.), the
            # artist, and the duration (skip intros/teasers and full-album uploads).
            if not _autoplay_title_ok(info.get("title", "")):
                continue
            if not _autoplay_artist_ok(artist, _extract_artist(info)):
                continue
            duration = info.get("duration") or 0
            if duration and not 60 <= duration <= 900:
                continue
            if song_key(info.get("title", ""), _extract_artist(info)) in avoid:
                continue
            state.queue.append(Track(info, "Autoplay 🎲"))
            state.last_artist = artist
            return True

        return False

    async def _ytm_song_search(self, query: str) -> list[dict] | None:
        """YTM "Songs" search, cached for an hour — autoplay stays on one artist,
        so after the first pick this saves a round trip per song."""
        key = query.lower()
        hit = self._ytm_cache.get(key)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        ytm_url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}&sp={YTM_SONGS_SP}"
        loop = asyncio.get_running_loop()
        try:
            with yt_dlp.YoutubeDL({**YTDL_OPTIONS, "extract_flat": True}) as ydl:
                results = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(ytm_url, download=False)
                )
        except Exception as e:
            print(f"[musicbot] Autoplay YTM error: {e}")
            return None
        entries = list(results.get("entries") or [])
        self._ytm_cache.pop(key, None)
        self._ytm_cache[key] = (time.time(), entries)
        if len(self._ytm_cache) > 200:
            self._ytm_cache.pop(next(iter(self._ytm_cache)))
        return entries

    # --- Background tasks ---

    @tasks.loop(seconds=30)
    async def _idle_check(self):
        for guild_id, state in list(self.states.items()):
            # An exception escaping a tasks.loop ends it for good, so contain it per guild.
            try:
                await self._idle_check_guild(guild_id, state)
            except Exception as e:
                print(f"[musicbot] Idle check failed for guild {guild_id}: {e!r}")
        self._mark_busy(any(vc.is_playing() or vc.is_paused() for vc in self.bot.voice_clients))

    async def _idle_check_guild(self, guild_id: int, state: GuildState):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc or not vc.channel:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans and not state.radio_station:
            state.idle_ticks += 1
            if state.idle_ticks >= 6:  # 3 min with no humans in channel
                await self._idle_disconnect(guild_id, state, vc)
            return
        if vc.is_playing() or vc.is_paused() or state.queue or state.radio_station:
            state.idle_ticks = 0
        else:
            state.idle_ticks += 1
            if state.idle_ticks >= 10:  # 5 min idle
                await self._idle_disconnect(guild_id, state, vc)

    async def _idle_disconnect(self, guild_id: int, state: GuildState, vc: discord.VoiceClient):
        self._clear_playback(state)
        await vc.disconnect()
        self.states.pop(guild_id, None)
        await self._retire_np(state)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState, after: discord.VoiceState):
        # The bot was disconnected (kicked, channel deleted, /leave): drop playback
        # state so e.g. a stale radio station doesn't keep blocking /play.
        if member.id != self.bot.user.id or after.channel is not None:
            return
        state = self.states.get(member.guild.id)
        if state:
            self._clear_playback(state)
            await self._retire_np(state)

    @_idle_check.before_loop
    async def _before_idle_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5)
    async def _progress_update(self):
        for guild_id, state in list(self.states.items()):
            self._start_preload(state)  # picks up reorders (/move, /shuffle, /playnext)
            if not state.np_message or not state.current or state.radio_station:
                continue
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            vc = guild.voice_client
            if not vc or (not vc.is_playing() and not vc.is_paused()):
                continue
            try:
                if state.np_view:
                    # Also catches /pause, /loop etc. issued as commands rather than buttons.
                    state.np_view._sync_buttons()
                    await state.np_message.edit(embed=build_np_embed(state, state.current),
                                                view=state.np_view)
                else:
                    await state.np_message.edit(embed=build_np_embed(state, state.current))
            except discord.NotFound:
                state.np_message = None
            except Exception:
                pass

    @_progress_update.before_loop
    async def _before_progress_update(self):
        await self.bot.wait_until_ready()

    # --- Slash Commands ---

    @app_commands.command(name="join", description="Join your voice channel")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "You need to be in a voice channel.", ephemeral=True)
        if await self._deny_non_dj(interaction):
            return
        # Connecting can take longer than the 3 s Discord gives an interaction.
        await interaction.response.defer()
        ch = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc:
            await vc.move_to(ch)
        else:
            await ch.connect(self_deaf=True)
        await interaction.followup.send(f"Joined **{ch.name}**.")

    @app_commands.command(name="leave", description="Leave the voice channel and clear the queue")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message(
                "I'm not in a voice channel.", ephemeral=True)
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        self._clear_playback(state)
        await vc.disconnect()
        await interaction.response.send_message("👋 Disconnected.")
        await self._retire_np(state)

    @app_commands.command(name="play", description="Play a song or add it to the queue")
    @app_commands.describe(query="Song name, YouTube URL, or playlist URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first!", ephemeral=True)
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        state.channel = interaction.channel
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect(self_deaf=True)
        self._end_radio(state, vc)
        requester = interaction.user.display_name
        # A locked play_lock means a track change is underway; treat that as playing.
        already_playing = vc.is_playing() or vc.is_paused() or state.play_lock.locked()

        # User is taking over — cancel prefetch and drop any autoplay-queued track
        if already_playing and state.autoplay:
            self._interrupt_autoplay(state)

        is_spotify = "open.spotify.com" in query
        is_yt_playlist = (not is_spotify and query.startswith("http")
                          and ("list=" in query or "/playlist" in query))

        if is_spotify:
            try:
                queries = await self._spotify_queries(query)
            except Exception as e:
                return await interaction.followup.send(f"Spotify error: {e}")
            if not queries:
                return await interaction.followup.send("No tracks found in that Spotify link.")
            for q in queries:
                state.queue.append(Track.lazy(q, requester))
            label = "track" if len(queries) == 1 else f"**{len(queries)}** tracks"
            await interaction.followup.send(f"Added {label} from Spotify to the queue.")

        elif is_yt_playlist:
            try:
                loop = asyncio.get_running_loop()
                opts = {**YTDL_OPTIONS, "extract_flat": True, "noplaylist": False}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = await loop.run_in_executor(
                        None, lambda: ydl.extract_info(query, download=False)
                    )
                entries = info.get("entries") or []
                added = 0
                for entry in entries:
                    if not entry or not entry.get("id"):
                        continue
                    watch_url = f"https://www.youtube.com/watch?v={entry['id']}"
                    t = Track.lazy(watch_url, requester)
                    t.title = entry.get("title") or watch_url
                    t.duration = entry.get("duration") or 0
                    t.webpage_url = watch_url
                    t.thumbnail = entry.get("thumbnail") or ""
                    state.queue.append(t)
                    added += 1
                await interaction.followup.send(f"Added **{added}** tracks from playlist to the queue.")
            except Exception as e:
                return await interaction.followup.send(f"Error loading playlist: {e}")

        else:
            track_info = await self._fetch_track(query, ytm_only=state.ytm_only)
            if not track_info:
                msg = ("Couldn't find that track on YouTube Music." if state.ytm_only
                       else "Couldn't find that track.")
                return await interaction.followup.send(msg)
            track = Track(track_info, requester)
            state.queue.append(track)
            if already_playing:
                await interaction.followup.send(
                    f"➕ Added to queue: **{track.title}**"
                    f"{f' — {track.artist}' if track.artist else ''} (#{len(state.queue)})")
            else:
                await interaction.followup.send(f"Loading **{track.title}**...")

        if not vc.is_playing() and not vc.is_paused() and state.queue:
            await self.play_next(interaction.guild_id)

    async def _search_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if len(current) < 3:
            return []
        try:
            ytm_only = self.get_state(interaction.guild_id).ytm_only
            results = await self._search(current, count=5, ytm_only=ytm_only)
            return [
                app_commands.Choice(
                    name=r.get("title", "Unknown")[:100],
                    value=(f"https://music.youtube.com/watch?v={r['id']}"
                           if ytm_only and r.get("id")
                           else (r.get("url") or r.get("webpage_url") or current)),
                )
                for r in results if r.get("title")
            ]
        except Exception:
            return []

    @play.autocomplete("query")
    async def play_autocomplete(self, interaction: discord.Interaction,
                                current: str) -> list[app_commands.Choice[str]]:
        return await self._search_autocomplete(interaction, current)

    @app_commands.command(name="playnext", description="Queue a song to play next, right after the current track")
    @app_commands.describe(query="Song name or YouTube URL")
    @app_commands.guild_only()
    async def playnext(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first!", ephemeral=True)
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        state.channel = interaction.channel
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect(self_deaf=True)
        self._end_radio(state, vc)

        track_info = await self._fetch_track(query, ytm_only=state.ytm_only)
        if not track_info:
            msg = ("Couldn't find that track on YouTube Music." if state.ytm_only
                   else "Couldn't find that track.")
            return await interaction.followup.send(msg)
        track = Track(track_info, interaction.user.display_name)

        if vc.is_playing() or vc.is_paused() or state.play_lock.locked():
            # Cancel prefetch and drop autoplay-queued track so this song plays right after current
            if state.autoplay:
                self._interrupt_autoplay(state)
            state.queue.appendleft(track)
            await interaction.followup.send(
                f"⏭️ Playing next: **{track.title}**"
                f"{f' — {track.artist}' if track.artist else ''}")
        else:
            state.queue.appendleft(track)
            await interaction.followup.send(f"Loading **{track.title}**...")
        # Also covers a track change that was underway and ended with nothing to play;
        # play_next returns at once if something is playing by the time it gets the lock.
        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(interaction.guild_id)

    @playnext.autocomplete("query")
    async def playnext_autocomplete(self, interaction: discord.Interaction,
                                    current: str) -> list[app_commands.Choice[str]]:
        return await self._search_autocomplete(interaction, current)

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if await self._deny_non_dj(interaction):
            return
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            self.get_state(interaction.guild_id).pause_start = time.time()
            await interaction.response.send_message("⏸️ Paused.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if await self._deny_non_dj(interaction):
            return
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            state = self.get_state(interaction.guild_id)
            if state.pause_start and state.start_time:
                state.start_time += time.time() - state.pause_start
            state.pause_start = None
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("Not paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        self._clear_playback(state)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message("⏹️ Stopped and queue cleared.")
        await self._retire_np(state)

    @app_commands.command(name="skip", description="Skip the current track")
    @app_commands.describe(to="Skip ahead to queue position N (default: next track)")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction, to: int = 1):
        if await self._deny_non_dj(interaction):
            return
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        self._skip(self.get_state(interaction.guild_id), vc, to)
        await interaction.response.send_message("⏭️ Skipped.")

    @staticmethod
    def _parse_time(text: str) -> int | None:
        """'90', '1:30' or '1:02:30' → seconds."""
        parts = text.strip().split(":")
        if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
            return None
        seconds = 0
        for p in parts:
            seconds = seconds * 60 + int(p)
        return seconds

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(position="Where to jump to, e.g. 90 or 1:30")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, position: str):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        if not state.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        seconds = self._parse_time(position)
        if seconds is None:
            return await interaction.response.send_message(
                "Use seconds or minutes:seconds, e.g. `90` or `1:30`.", ephemeral=True)
        if state.current.duration and seconds >= state.current.duration:
            return await interaction.response.send_message(
                "Seek position is past the end of the track.", ephemeral=True)
        state.seek_to = float(seconds)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message(f"⏩ Seeked to **{fmt_dur(seconds)}**.")

    @app_commands.command(name="queue", description="Show the current queue")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current and not state.queue:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        await interaction.response.send_message(embed=build_queue_embed(state))

    @app_commands.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(position="Queue position to remove (1 = next track)")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: int):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        if not 1 <= position <= len(state.queue):
            return await interaction.response.send_message(
                f"Position must be between 1 and {len(state.queue)}.", ephemeral=True)
        items = list(state.queue)
        removed = items.pop(position - 1)
        state.queue = deque(items)
        self._start_prefetch(interaction.guild_id, state)
        await interaction.response.send_message(f"🗑️ Removed **{removed.title}**.")

    @app_commands.command(name="move", description="Move a track to another place in the queue")
    @app_commands.describe(source="Current queue position of the track",
                           target="New queue position (1 = plays next)")
    @app_commands.guild_only()
    async def move(self, interaction: discord.Interaction, source: int, target: int):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        n = len(state.queue)
        if not (1 <= source <= n and 1 <= target <= n):
            msg = "The queue is empty." if not n else f"Positions must be between 1 and {n}."
            return await interaction.response.send_message(msg, ephemeral=True)
        items = list(state.queue)
        track = items.pop(source - 1)
        items.insert(target - 1, track)
        state.queue = deque(items)
        await interaction.response.send_message(f"↕️ Moved **{track.title}** to position **{target}**.")

    @app_commands.command(name="clear", description="Clear the queue")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        self._start_prefetch(interaction.guild_id, state)
        await interaction.response.send_message("🗑️ Queue cleared.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        if not state.queue:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)
        await interaction.response.send_message("🔀 Queue shuffled.")

    @app_commands.command(name="loop", description="Set loop mode (omit to cycle: off → track → queue)")
    @app_commands.describe(mode="Loop mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="track", value="track"),
        app_commands.Choice(name="queue", value="queue"),
    ])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: str | None = None):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        if mode is None:
            cycle = ["off", "track", "queue"]
            mode = cycle[(cycle.index(state.loop_mode) + 1) % 3]
        state.loop_mode = mode
        icons = {"off": "➡️", "track": "🔂", "queue": "🔁"}
        await interaction.response.send_message(f"{icons[mode]} Loop set to **{mode}**.")

    @app_commands.command(name="volume", description="Set the playback volume (0–100)")
    @app_commands.describe(level="Volume level 0–100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: int):
        if await self._deny_non_dj(interaction):
            return
        if not 0 <= level <= 100:
            return await interaction.response.send_message(
                "Volume must be between 0 and 100.", ephemeral=True)
        state = self.get_state(interaction.guild_id)
        state.volume = level / 100
        self._save_setting(interaction.guild_id, "volume", state.volume)
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.send_message(embed=build_np_embed(state, state.current))

    @app_commands.command(name="search", description="Search YouTube Music (or YouTube) and pick a track")
    @app_commands.describe(query="Search query")
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: str):
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()
        state = self.get_state(interaction.guild_id)
        results = await self._search(query, ytm_only=state.ytm_only)
        if not results:
            return await interaction.followup.send("No results found.")

        lines = [
            f"`{i+1}.` {r.get('title', 'Unknown')} `{fmt_dur(r.get('duration', 0))}`"
            for i, r in enumerate(results[:10])
        ]
        embed = discord.Embed(
            title=f"Search: {query}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )

        async def on_pick(sel: discord.Interaction, result: dict):
            if await self._deny_non_dj(sel):
                return
            _state = self.get_state(sel.guild_id)
            vid = result.get("id", "")
            if _state.ytm_only and vid:
                url = f"https://music.youtube.com/watch?v={vid}"
            else:
                url = (result.get("url") or result.get("webpage_url") or
                       f"https://www.youtube.com/watch?v={vid}")
            track_info = await self._fetch_track(url)
            if not track_info:
                return await sel.followup.send("Couldn't load that track.", ephemeral=True)
            track = Track(track_info, sel.user.display_name)
            state = self.get_state(sel.guild_id)
            state.channel = sel.channel
            vc = sel.guild.voice_client
            if not vc:
                if sel.user.voice:
                    vc = await sel.user.voice.channel.connect(self_deaf=True)
                else:
                    return await sel.followup.send("Join a voice channel first!", ephemeral=True)
            self._end_radio(state, vc)
            state.queue.append(track)
            if vc.is_playing() or vc.is_paused() or state.play_lock.locked():
                await sel.followup.send(f"➕ Added: **{track.title}** (#{len(state.queue)})")
            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(sel.guild_id)

        await interaction.followup.send(embed=embed, view=SearchView(results, on_pick))

    # --- Lyrics ---

    def _parse_lyrics_query(self, song: str, track=None) -> tuple[str, str]:
        """Return (artist, title) cleaned for a lyrics search."""
        title = song
        artist = ""

        if track:
            title = track.title or song
            artist = track.artist or ""
            # Only keep primary artist (first before any comma)
            if "," in artist:
                artist = artist.split(",")[0].strip()

        # Split "Artist - Title" format present in many YouTube titles.
        # Always prefer the artist embedded in the title over track.artist —
        # the title is usually the authoritative "Artist - Song" label.
        if " - " in title:
            parts = title.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        # Strip YouTube junk: [Official Video], (prod. by X), [2024], etc.
        title = re.sub(r'\s*[\(\[][^\)\]]*(?:official|video|audio|lyrics?|lyric|music|hd|hq|4k|\d{4})[^\)\]]*[\)\]]', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*\(prod\..*?\)', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*[\(\[]?feat\.?\s+.*?[\)\]]?$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*\bft\.\s+.*$', '', title, flags=re.IGNORECASE)

        artist = re.sub(r'\s*feat\..*', '', artist, flags=re.IGNORECASE)
        artist = re.sub(r'\s*ft\..*', '', artist, flags=re.IGNORECASE)
        artist = artist.removesuffix(" - Topic")

        return artist.strip(), title.strip()

    async def _fetch_lyrics_text(self, artist: str, title: str) -> str | None:
        """Try lrclib.net (primary) then lyrics.ovh (fallback)."""
        async with aiohttp.ClientSession() as session:
            # 1. lrclib.net — structured search by track + artist
            if title:
                try:
                    params = {"track_name": title}
                    if artist:
                        params["artist_name"] = artist
                    async with session.get(
                        "https://lrclib.net/api/search",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status == 200:
                            for r in await resp.json():
                                text = r.get("plainLyrics", "").strip()
                                if text:
                                    return text
                except Exception:
                    pass

            # 2. lrclib.net — combined free-text query (wider net)
            combined = f"{artist} {title}".strip()
            if combined:
                try:
                    async with session.get(
                        "https://lrclib.net/api/search",
                        params={"q": combined},
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status == 200:
                            for r in await resp.json():
                                text = r.get("plainLyrics", "").strip()
                                if text:
                                    return text
                except Exception:
                    pass

            # 3. lyrics.ovh — classic fallback
            if artist and title:
                try:
                    url = (f"https://api.lyrics.ovh/v1/"
                           f"{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}")
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            text = data.get("lyrics", "").strip()
                            if text:
                                return text
                except Exception:
                    pass

        return None

    @app_commands.command(name="lyrics", description="Get lyrics for the current or a specified song")
    @app_commands.describe(song="Song name, or 'Artist - Title' format (defaults to current track)")
    @app_commands.guild_only()
    async def lyrics(self, interaction: discord.Interaction, song: str | None = None):
        state = self.get_state(interaction.guild_id)
        track = None
        if not song:
            if not state.current:
                return await interaction.response.send_message(
                    "Specify a song or play something first.", ephemeral=True)
            song = state.current.title
            track = state.current

        await interaction.response.defer()

        artist, title = self._parse_lyrics_query(song, track)
        display = f"{artist} — {title}" if artist else title

        text = await self._fetch_lyrics_text(artist, title)
        if not text:
            return await interaction.followup.send(f"No lyrics found for **{display}**.")

        for msg in self._lyrics_messages(display, text):
            await interaction.followup.send(msg)

    @staticmethod
    def _lyrics_messages(display: str, text: str, limit: int = 5700) -> list[str]:
        """Split lyrics into code-block messages under Discord's 2000-char cap."""
        msgs = [f"```{text[i:i+1900]}```" for i in range(0, min(len(text), limit), 1900)]
        msgs[0] = f"**{display}**\n{msgs[0]}"
        if len(text) > limit:
            msgs[-1] += "\n*(truncated)*"
        return msgs

    # --- Admin commands ---

    @app_commands.command(name="djrole", description="Set or clear the DJ role")
    @app_commands.describe(role="The DJ role (omit to clear)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def djrole(self, interaction: discord.Interaction, role: discord.Role | None = None):
        state = self.get_state(interaction.guild_id)
        state.dj_role = role.id if role else None
        self._save_setting(interaction.guild_id, "dj_role", state.dj_role)
        if role is None:
            await interaction.response.send_message("DJ role cleared — everyone can use all commands.")
        else:
            await interaction.response.send_message(f"DJ role set to **{role.name}**.")

    @app_commands.command(name="ytmonly", description="Toggle strict YouTube Music mode — no YouTube fallback")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ytmonly(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.ytm_only = not state.ytm_only
        self._save_setting(interaction.guild_id, "ytm_only", state.ytm_only)
        if state.ytm_only:
            await interaction.response.send_message(
                "🎵 **YouTube Music only mode enabled.** All searches, autoplay, and suggestions now use "
                "YouTube Music exclusively. Direct video links still play as normal."
            )
        else:
            await interaction.response.send_message(
                "🎵 **YouTube Music only mode disabled.** Searches will use YouTube Music with YouTube as fallback."
            )

    # --- Radio ---

    async def _start_radio(self, guild_id: int):
        state = self.get_state(guild_id)
        station = state.radio_station
        if not station:
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc or vc.is_playing() or vc.is_paused():
            return

        url = RADIO_STATIONS[station]
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(url, before_options=FFMPEG_RECONNECT, options="-vn"),
            volume=state.volume,
        )
        started = time.time()

        def after(error):
            if error:
                print(f"[musicbot] Radio error: {error}")
            self._schedule(self._radio_ended(guild_id, station, started))

        vc.play(source, after=after)
        self._mark_busy(True)

    async def _radio_ended(self, guild_id: int, station: str, started: float):
        """Reconnect a dropped stream, backing off when it keeps dying straight away
        so a dead station URL cannot spin ffmpeg in a tight loop."""
        state = self.get_state(guild_id)
        if state.radio_station != station:
            return  # stopped or switched station
        if time.time() - started < 10:
            state.radio_failures += 1
        else:
            state.radio_failures = 0
        if state.radio_failures >= 5:
            state.radio_station = None
            state.radio_failures = 0
            await self._notify(state, f"📻 **{station}** is not reachable right now — radio stopped.")
            return
        if state.radio_failures:
            await asyncio.sleep(2 ** state.radio_failures)
            if state.radio_station != station:
                return
        await self._start_radio(guild_id)

    @app_commands.command(name="radio", description="Play a 24/7 live radio station")
    @app_commands.describe(station="Radio station to play")
    @app_commands.choices(station=[
        app_commands.Choice(name=name, value=name)
        for name in RADIO_STATIONS
    ])
    @app_commands.guild_only()
    async def radio(self, interaction: discord.Interaction, station: str):
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first!", ephemeral=True)
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        state.channel = interaction.channel

        # Stop anything currently playing and clear queue
        self._clear_playback(state)  # also stops the old station from reconnecting
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await self._retire_np(state)

        if not vc:
            await interaction.user.voice.channel.connect(self_deaf=True)

        state.radio_station = station
        state.radio_failures = 0
        await self._start_radio(interaction.guild_id)

        embed = discord.Embed(
            title="📻 Radio",
            description=f"**{station}**",
            color=discord.Color.red(),
        )
        embed.add_field(name="Status", value="🔴 LIVE", inline=True)
        embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
        embed.set_footer(text=f"Started by {interaction.user.display_name} • Use /stop to end")
        state.np_message = await interaction.followup.send(embed=embed)

    # --- Playlist subcommand group ---

    playlist = app_commands.Group(name="playlist", description="Save and load custom playlists",
                                  guild_only=True)

    @staticmethod
    def _playlist_path(guild_id: int, name: str) -> str:
        safe = name.replace(" ", "_").replace("/", "_")
        return os.path.join(PLAYLISTS_DIR, f"{guild_id}_{safe}.json")

    @staticmethod
    def _playlist_names(guild_id: int) -> list[str]:
        prefix = f"{guild_id}_"
        files = [f for f in os.listdir(PLAYLISTS_DIR) if f.startswith(prefix) and f.endswith(".json")]
        return sorted(f[len(prefix):-5].replace("_", " ") for f in files)

    async def _playlist_autocomplete(self, interaction: discord.Interaction,
                                     current: str) -> list[app_commands.Choice[str]]:
        try:
            return [
                app_commands.Choice(name=n, value=n)
                for n in self._playlist_names(interaction.guild_id) if current.lower() in n.lower()
            ][:25]
        except Exception:
            return []

    @playlist.command(name="save", description="Save the current queue as a named playlist")
    @app_commands.describe(name="Playlist name", overwrite="Replace a playlist with the same name")
    async def playlist_save(self, interaction: discord.Interaction, name: str, overwrite: bool = False):
        state = self.get_state(interaction.guild_id)

        def _save_url(t: Track) -> str:
            # Prefer the resolved YouTube URL; fall back to the search query for lazy tracks
            return t.webpage_url or t.query or ""

        tracks = []
        if state.current:
            tracks.append({"title": state.current.title, "url": _save_url(state.current)})
        for t in state.queue:
            url = _save_url(t)
            if url:
                tracks.append({"title": t.title, "url": url})
        if not tracks:
            return await interaction.response.send_message("Nothing to save.", ephemeral=True)
        path = self._playlist_path(interaction.guild_id, name)
        if os.path.exists(path):
            if not overwrite:
                return await interaction.response.send_message(
                    f"A playlist named **{name}** already exists. "
                    "Pick another name, or save again with `overwrite: True`.", ephemeral=True)
            if await self._deny_non_dj(interaction):
                return
        with open(path, "w") as f:
            json.dump({"name": name, "tracks": tracks}, f)
        await interaction.response.send_message(f"💾 Saved **{len(tracks)}** tracks as **{name}**.")

    @playlist.command(name="load", description="Load a saved playlist into the queue")
    @app_commands.describe(name="Playlist name")
    async def playlist_load(self, interaction: discord.Interaction, name: str):
        path = self._playlist_path(interaction.guild_id, name)
        if not os.path.exists(path):
            return await interaction.response.send_message(
                f"No playlist named **{name}**. Use `/playlist list` to see saved ones.",
                ephemeral=True)
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first!", ephemeral=True)
        if await self._deny_non_dj(interaction):
            return

        await interaction.response.defer()
        state = self.get_state(interaction.guild_id)
        state.channel = interaction.channel
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect(self_deaf=True)
        self._end_radio(state, vc)

        with open(path) as f:
            data = json.load(f)

        requester = interaction.user.display_name
        added = 0
        for entry in data["tracks"]:
            url = entry.get("url", "")
            if not url:
                continue
            t = Track.lazy(url, requester)
            t.title = entry.get("title") or url
            t.webpage_url = url if url.startswith("http") else ""
            state.queue.append(t)
            added += 1
        await interaction.followup.send(f"📂 Loaded **{added}** tracks from **{name}**.")
        if not vc.is_playing() and not vc.is_paused() and state.queue:
            await self.play_next(interaction.guild_id)

    @playlist_load.autocomplete("name")
    async def playlist_load_autocomplete(self, interaction: discord.Interaction,
                                         current: str) -> list[app_commands.Choice[str]]:
        return await self._playlist_autocomplete(interaction, current)

    @playlist.command(name="delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist name")
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        if await self._deny_non_dj(interaction):
            return
        path = self._playlist_path(interaction.guild_id, name)
        if not os.path.exists(path):
            return await interaction.response.send_message(
                f"No playlist named **{name}**.", ephemeral=True)
        os.remove(path)
        await interaction.response.send_message(f"🗑️ Deleted playlist **{name}**.")

    @playlist_delete.autocomplete("name")
    async def playlist_delete_autocomplete(self, interaction: discord.Interaction,
                                           current: str) -> list[app_commands.Choice[str]]:
        return await self._playlist_autocomplete(interaction, current)

    @playlist.command(name="list", description="List all saved playlists for this server")
    async def playlist_list(self, interaction: discord.Interaction):
        names = self._playlist_names(interaction.guild_id)
        if not names:
            return await interaction.response.send_message(
                "No saved playlists for this server.", ephemeral=True)
        await interaction.response.send_message(
            "**Saved Playlists:**\n" + "\n".join(f"• {n}" for n in names))

    @app_commands.command(name="autoplay", description="Toggle autoplay — queues a related track when the queue runs out")
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction):
        if await self._deny_non_dj(interaction):
            return
        state = self.get_state(interaction.guild_id)
        self._set_autoplay(interaction.guild_id, state, not state.autoplay)
        if state.autoplay:
            await interaction.response.send_message(
                "🎲 Autoplay **enabled** — a related track will queue when the list runs out.")
        else:
            await interaction.response.send_message("🎲 Autoplay **disabled**.")

    @app_commands.command(name="restart", description="Restart the bot (owner only)")
    async def restart(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.send_message("♻️ Restarting — back in a few seconds.")
        # ponytail: exit and let systemd Restart=always bring it back — no sudo, no SSH.
        await self.bot.close()

    @app_commands.command(name="help", description="Show all available bot commands")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎵 Music Bot — Command Reference",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🔊 Voice", value=(
            "`/join` — Join your voice channel\n"
            "`/leave` — Leave the voice channel and clear the queue"
        ), inline=False)
        embed.add_field(name="▶️ Playback", value=(
            "`/play <query>` — Play a song, playlist, or URL (YouTube / Spotify)\n"
            "`/playnext <query>` — Queue a song to play immediately after the current track\n"
            "`/pause` — Pause playback\n"
            "`/resume` — Resume playback\n"
            "`/stop` — Stop playback and clear the queue\n"
            "`/skip` — Skip to the next track\n"
            "`/seek <time>` — Jump to a position in the current track (e.g. `90` or `1:30`)\n"
            "`/volume <0-100>` — Set playback volume"
        ), inline=False)
        embed.add_field(name="📋 Queue", value=(
            "`/queue` — Show the current queue\n"
            "`/remove <position>` — Remove a track from the queue\n"
            "`/move <from> <to>` — Move a track to another place in the queue\n"
            "`/clear` — Clear all tracks from the queue\n"
            "`/shuffle` — Shuffle the queue\n"
            "`/loop <off|track|queue>` — Set loop mode"
        ), inline=False)
        embed.add_field(name="ℹ️ Info", value=(
            "`/nowplaying` — Show the currently playing track with progress bar\n"
            "`/search <query>` — Search YouTube and pick from 10 results\n"
            "`/lyrics [song]` — Fetch lyrics for the current or a specified track\n"
            "`/autoplay` — Toggle autoplay (queues a related track when the queue is empty)"
        ), inline=False)
        stations = ", ".join(RADIO_STATIONS.keys())
        embed.add_field(name="📻 Radio", value=(
            f"`/radio <station>` — Play a 24/7 live radio stream\n"
            f"Available stations: {stations}\n"
            "Radio keeps the bot in channel even when alone. `/play`, `/stop` or `/leave` end it."
        ), inline=False)
        embed.add_field(name="💾 Playlists", value=(
            "`/playlist save <name>` — Save the current queue as a playlist\n"
            "`/playlist load <name>` — Load and play a saved playlist\n"
            "`/playlist delete <name>` — Delete a saved playlist\n"
            "`/playlist list` — List all saved playlists for this server"
        ), inline=False)
        embed.add_field(name="🛡️ Admin", value=(
            "`/djrole <role>` — Restrict music commands to a specific role (omit to clear)\n"
            "`/ytmonly` — Toggle strict YouTube Music mode (no YouTube fallback; direct links still work)\n"
            "Requires **Manage Server** permission.\n"
            "`/restart` — Restart the bot (bot owner only)"
        ), inline=False)
        embed.set_footer(text="Supports YouTube, Spotify tracks/albums/playlists, and direct stream URLs.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
