import asyncio
import json
import os
import random
import re
import time
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
    AUTOPLAY_MAX_COOLDOWN,
    FFMPEG_RECONNECT,
    PLAYLISTS_DIR,
    RADIO_STATIONS,
    YTDL_OPTIONS,
    YTM_SONGS_SP,
    _AUTOPLAY_DEPRIORITIZE_RE,
)
from .models import GuildState, Track
from .utils import _autoplay_artist_ok, _autoplay_title_ok, _extract_artist, fmt_dur
from .views import NowPlayingView, SearchView, build_np_embed, build_queue_embed


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        self._idle_check.start()
        self._progress_update.start()

    def cog_unload(self):
        self._idle_check.cancel()
        self._progress_update.cancel()

    def get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildState()
        return self.states[guild_id]

    def _is_dj(self, interaction: discord.Interaction) -> bool:
        state = self.get_state(interaction.guild_id)
        if state.dj_role is None:
            return True
        role = interaction.guild.get_role(state.dj_role)
        return role in interaction.user.roles if role else True

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
        state.seek_offset = seek
        state.seek_to = None

        def after(error):
            if error:
                print(f"[musicbot] Player error: {error}")
            asyncio.run_coroutine_threadsafe(self.play_next(guild_id), self.bot.loop)

        vc.play(source, after=after)

        # Pre-fetch the next autoplay track in the background so it's ready
        # the moment this track ends — avoids the search+extract gap between songs.
        if state.autoplay and not state.queue and not state.radio_station:
            if state._prefetch_task and not state._prefetch_task.done():
                state._prefetch_task.cancel()
            state._prefetch_task = asyncio.create_task(self._prefetch_autoplay(guild_id))

        if announce and state.channel:
            view = NowPlayingView(self, guild_id)
            state.np_view = view
            state.np_message = await state.channel.send(embed=build_np_embed(state, track), view=view)

    async def play_next(self, guild_id: int):
        state = self.get_state(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc:
            return

        # Pending seek: replay current track from a new position
        if state.seek_to is not None and state.current:
            seek_pos = state.seek_to
            state.seek_to = None
            await self._play_track(guild_id, state.current, state, seek=seek_pos, announce=False)
            return

        if state.loop_mode == "track" and state.current:
            next_track = state.current
        else:
            if state.loop_mode == "queue" and state.current:
                state.queue.append(state.current)
            if state.queue:
                next_track = state.queue.popleft()
            else:
                if state.autoplay and state.current and not state.radio_station:
                    task = state._prefetch_task
                    state._prefetch_task = None
                    if task and not task.done():
                        # Pre-fetch is still running — wait for it
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                            task.cancel()
                    if not state.queue:
                        # Pre-fetch failed or was cancelled — fall back to a synchronous fetch
                        fetched = await self._autoplay_next(state)
                        if not fetched:
                            state.current = None
                            state.start_time = None
                            state.np_message = None
                            state.np_view = None
                            return
                    next_track = state.queue.popleft()
                else:
                    state.current = None
                    state.start_time = None
                    state.np_message = None
                    state.np_view = None
                    return

        state.current = next_track

        # Resolve lazy Spotify tracks just before playback.
        if not next_track.is_resolved:
            info = await self._fetch_track(next_track.query, ytm_only=state.ytm_only)
            if not info:
                if state.channel:
                    await state.channel.send(f"⚠️ Couldn't find **{next_track.title}**, skipping...")
                state.current = None
                await self.play_next(guild_id)
                return
            next_track.resolve(info)

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
            album = await loop.run_in_executor(None, lambda: sp.album_tracks(url))
            return [_track_query(item) for item in album["items"] if item]

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

    def _cancel_prefetch(self, state: GuildState):
        if state._prefetch_task and not state._prefetch_task.done():
            state._prefetch_task.cancel()
        state._prefetch_task = None

    def _interrupt_autoplay(self, state: GuildState):
        """Cancel prefetch and remove any autoplay-queued tracks so user's addition plays next."""
        self._cancel_prefetch(state)
        state.queue = deque(t for t in state.queue if t.requester != "Autoplay 🎲")

    async def _autoplay_next(self, state: GuildState) -> bool:
        """Queue one song by the same artist via YTM, using a weight-based cooldown to vary picks."""
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

        ytm_url = f"https://music.youtube.com/search?q={urllib.parse.quote(artist)}&sp={YTM_SONGS_SP}"
        loop = asyncio.get_running_loop()
        opts = {**YTDL_OPTIONS, "extract_flat": True}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                results = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(ytm_url, download=False)
                )
        except Exception as e:
            print(f"[musicbot] Autoplay YTM error: {e}")
            return False

        # Candidates: valid YTM song entries, excluding the current track, obvious non-studio content,
        # and very short clips (< 60 s) which are typically teasers, trailers, or intros.
        candidates = [
            e for e in (results.get("entries") or [])
            if e.get("ie_key") == "Youtube"
            and e.get("id")
            and e["id"] != current_id
            and _autoplay_title_ok(e.get("title", ""))
            and (e.get("duration") or 999) >= 60
        ]
        if not candidates:
            return False

        # --- Weight-based cooldown ---
        # Decay all cooldowns by 1 now that one more song is transitioning.
        for vid in list(state.autoplay_weights.keys()):
            state.autoplay_weights[vid] -= 1
            if state.autoplay_weights[vid] <= 0:
                del state.autoplay_weights[vid]

        # Cooldown for the chosen track scales with pool size so small pools
        # (e.g. artist with only 4 search results) don't block every song for too long.
        pool_size = len(candidates)
        cooldown = min(AUTOPLAY_MAX_COOLDOWN, max(1, pool_size - 1))

        # Popularity factor: prefer tracks with more views.
        view_counts = [e.get("view_count") or 0 for e in candidates]
        if any(v > 0 for v in view_counts):
            max_vc = max(view_counts) or 1
            # Normalise to [0.1, 1.0] so even the least-viewed song still has some chance.
            pop_factor = [0.1 + 0.9 * (v / max_vc) for v in view_counts]
        else:
            # Search order is relevance-ranked; gently prefer earlier positions [1.0 → 0.6].
            n = len(candidates)
            pop_factor = [1.0 - 0.4 * (i / max(n - 1, 1)) for i in range(n)]

        # Combined weight: cooldown penalty × popularity boost × version penalty.
        # Fresh tracks (cooldown 0): full weight. Recently played: weight approaches 0.
        # Alternate versions (acoustic, piano, slowed, etc.): weight × 0.1.
        ver_factor = [
            0.1 if _AUTOPLAY_DEPRIORITIZE_RE.search(e.get("title", "")) else 1.0
            for e in candidates
        ]
        sel_weights = [
            (1.0 / (1 + state.autoplay_weights.get(e["id"], 0))) * pf * vf
            for e, pf, vf in zip(candidates, pop_factor, ver_factor)
        ]

        # Try candidates in weighted-random order until one fetches successfully.
        remaining = list(zip(candidates, sel_weights))
        while remaining:
            entries_r, weights_r = zip(*remaining)
            chosen = random.choices(entries_r, weights=weights_r, k=1)[0]
            remaining = [(e, w) for e, w in remaining if e["id"] != chosen["id"]]
            try:
                info = await self._fetch_info(f"https://music.youtube.com/watch?v={chosen['id']}")
                if "entries" in info:
                    info = info["entries"][0]
                # Post-fetch checks: title (full version may reveal "live" etc.) and artist match
                if not _autoplay_title_ok(info.get("title", "")):
                    continue
                if not _autoplay_artist_ok(artist, _extract_artist(info)):
                    continue
                state.autoplay_weights[chosen["id"]] = cooldown
                state.queue.append(Track(info, "Autoplay 🎲"))
                state.last_artist = artist
                return True
            except Exception:
                pass

        return False

    # --- Background tasks ---

    @tasks.loop(seconds=30)
    async def _idle_check(self):
        for guild_id, state in list(self.states.items()):
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            vc = guild.voice_client
            if not vc:
                continue
            humans = [m for m in vc.channel.members if not m.bot]
            if not humans and not state.radio_station:
                state.idle_ticks += 1
                if state.idle_ticks >= 6:  # 3 min with no humans in channel
                    await vc.disconnect()
                    del self.states[guild_id]
                continue
            if vc.is_playing() or vc.is_paused() or state.queue or state.radio_station:
                state.idle_ticks = 0
            else:
                state.idle_ticks += 1
                if state.idle_ticks >= 10:  # 5 min idle
                    await vc.disconnect()
                    del self.states[guild_id]

    @_idle_check.before_loop
    async def _before_idle_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5)
    async def _progress_update(self):
        for guild_id, state in list(self.states.items()):
            if not state.np_message or not state.current or state.radio_station:
                continue
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            vc = guild.voice_client
            if not vc or (not vc.is_playing() and not vc.is_paused()):
                continue
            try:
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
        ch = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc:
            await vc.move_to(ch)
        else:
            await ch.connect(self_deaf=True)
        await interaction.response.send_message(f"Joined **{ch.name}**.")

    @app_commands.command(name="leave", description="Leave the voice channel and clear the queue")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message(
                "I'm not in a voice channel.", ephemeral=True)
        state = self.get_state(interaction.guild_id)
        self._cancel_prefetch(state)
        state.queue.clear()
        state.current = None
        state.radio_station = None
        state.np_view = None
        await vc.disconnect()
        await interaction.response.send_message("👋 Disconnected.")

    @app_commands.command(name="play", description="Play a song or add it to the queue")
    @app_commands.describe(query="Song name, YouTube URL, or playlist URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first!", ephemeral=True)
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        if state.radio_station:
            return await interaction.followup.send(
                "📻 Radio is active — use `/stop` to end it before queuing songs.", ephemeral=True)
        state.channel = interaction.channel
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect(self_deaf=True)
        requester = interaction.user.display_name
        already_playing = vc.is_playing() or vc.is_paused()

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
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        if state.radio_station:
            return await interaction.followup.send(
                "📻 Radio is active — use `/stop` to end it before queuing songs.", ephemeral=True)
        state.channel = interaction.channel
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect(self_deaf=True)

        track_info = await self._fetch_track(query, ytm_only=state.ytm_only)
        if not track_info:
            msg = ("Couldn't find that track on YouTube Music." if state.ytm_only
                   else "Couldn't find that track.")
            return await interaction.followup.send(msg)
        track = Track(track_info, interaction.user.display_name)

        if vc.is_playing() or vc.is_paused():
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
            await self.play_next(interaction.guild_id)

    @playnext.autocomplete("query")
    async def playnext_autocomplete(self, interaction: discord.Interaction,
                                    current: str) -> list[app_commands.Choice[str]]:
        return await self._search_autocomplete(interaction, current)

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
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
        if not self._is_dj(interaction):
            return await interaction.response.send_message(
                "You need the DJ role to use this.", ephemeral=True)
        state = self.get_state(interaction.guild_id)
        self._cancel_prefetch(state)
        state.queue.clear()
        state.current = None
        state.radio_station = None
        state.np_view = None
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
        await interaction.response.send_message("⏹️ Stopped and queue cleared.")

    @app_commands.command(name="skip", description="Skip the current track")
    @app_commands.describe(to="Skip ahead to queue position N (default: next track)")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction, to: int = 1):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        state = self.get_state(interaction.guild_id)
        for _ in range(min(max(0, to - 1), len(state.queue))):
            state.queue.popleft()
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.")

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(seconds="Position to seek to in seconds")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, seconds: int):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
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
        state = self.get_state(interaction.guild_id)
        if not 1 <= position <= len(state.queue):
            return await interaction.response.send_message(
                f"Position must be between 1 and {len(state.queue)}.", ephemeral=True)
        items = list(state.queue)
        removed = items.pop(position - 1)
        state.queue = deque(items)
        await interaction.response.send_message(f"🗑️ Removed **{removed.title}**.")

    @app_commands.command(name="clear", description="Clear the queue")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction):
        if not self._is_dj(interaction):
            return await interaction.response.send_message(
                "You need the DJ role to use this.", ephemeral=True)
        self.get_state(interaction.guild_id).queue.clear()
        await interaction.response.send_message("🗑️ Queue cleared.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
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
        if not 0 <= level <= 100:
            return await interaction.response.send_message(
                "Volume must be between 0 and 100.", ephemeral=True)
        state = self.get_state(interaction.guild_id)
        state.volume = level / 100
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
            state.queue.append(track)
            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(sel.guild_id)
            else:
                await sel.followup.send(f"➕ Added: **{track.title}** (#{len(state.queue)})")

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

        chunks = [text[i:i+1900] for i in range(0, min(len(text), 5700), 1900)]
        await interaction.followup.send(f"**{display}**\n```{chunks[0]}```")
        for chunk in chunks[1:]:
            await interaction.followup.send(f"```{chunk}```")

    # --- Admin commands ---

    @app_commands.command(name="djrole", description="Set or clear the DJ role")
    @app_commands.describe(role="The DJ role (omit to clear)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def djrole(self, interaction: discord.Interaction, role: discord.Role | None = None):
        state = self.get_state(interaction.guild_id)
        if role is None:
            state.dj_role = None
            await interaction.response.send_message("DJ role cleared — everyone can use all commands.")
        else:
            state.dj_role = role.id
            await interaction.response.send_message(f"DJ role set to **{role.name}**.")

    @app_commands.command(name="ytmonly", description="Toggle strict YouTube Music mode — no YouTube fallback")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ytmonly(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.ytm_only = not state.ytm_only
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
        if not state.radio_station:
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc:
            return

        url = RADIO_STATIONS[state.radio_station]
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(url, before_options=FFMPEG_RECONNECT, options="-vn"),
            volume=state.volume,
        )

        def after(error):
            if error:
                print(f"[musicbot] Radio error: {error}")
            if state.radio_station:  # still in radio mode — reconnect
                asyncio.run_coroutine_threadsafe(self._start_radio(guild_id), self.bot.loop)

        vc.play(source, after=after)

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

        state = self.get_state(interaction.guild_id)
        state.channel = interaction.channel

        # Stop anything currently playing and clear queue
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state.radio_station = None  # prevent reconnect of old radio
            vc.stop()
        state.queue.clear()
        state.current = None
        state.np_message = None

        if not vc:
            await interaction.user.voice.channel.connect(self_deaf=True)

        state.radio_station = station
        await self._start_radio(interaction.guild_id)

        embed = discord.Embed(
            title="📻 Radio",
            description=f"**{station}**",
            color=discord.Color.red(),
        )
        embed.add_field(name="Status", value="🔴 LIVE", inline=True)
        embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
        embed.set_footer(text=f"Started by {interaction.user.display_name} • Use /stop to end")
        state.np_message = await interaction.response.send_message(embed=embed)

    # --- Playlist subcommand group ---

    playlist = app_commands.Group(name="playlist", description="Save and load custom playlists")

    @playlist.command(name="save", description="Save the current queue as a named playlist")
    @app_commands.describe(name="Playlist name")
    async def playlist_save(self, interaction: discord.Interaction, name: str):
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
        safe = name.replace(" ", "_").replace("/", "_")
        path = os.path.join(PLAYLISTS_DIR, f"{interaction.guild_id}_{safe}.json")
        with open(path, "w") as f:
            json.dump({"name": name, "tracks": tracks}, f)
        await interaction.response.send_message(f"💾 Saved **{len(tracks)}** tracks as **{name}**.")

    @playlist.command(name="load", description="Load a saved playlist into the queue")
    @app_commands.describe(name="Playlist name")
    async def playlist_load(self, interaction: discord.Interaction, name: str):
        safe = name.replace(" ", "_").replace("/", "_")
        path = os.path.join(PLAYLISTS_DIR, f"{interaction.guild_id}_{safe}.json")
        if not os.path.exists(path):
            return await interaction.response.send_message(
                f"No playlist named **{name}**. Use `/playlist list` to see saved ones.",
                ephemeral=True)
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "Join a voice channel first!", ephemeral=True)

        await interaction.response.defer()
        state = self.get_state(interaction.guild_id)
        state.channel = interaction.channel
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect(self_deaf=True)

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
        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(interaction.guild_id)

    @playlist_load.autocomplete("name")
    async def playlist_load_autocomplete(self, interaction: discord.Interaction,
                                         current: str) -> list[app_commands.Choice[str]]:
        try:
            prefix = f"{interaction.guild_id}_"
            files = [f for f in os.listdir(PLAYLISTS_DIR)
                     if f.startswith(prefix) and f.endswith(".json")]
            names = [f[len(prefix):-5].replace("_", " ") for f in files]
            return [
                app_commands.Choice(name=n, value=n)
                for n in names if current.lower() in n.lower()
            ][:25]
        except Exception:
            return []

    @playlist.command(name="list", description="List all saved playlists for this server")
    async def playlist_list(self, interaction: discord.Interaction):
        prefix = f"{interaction.guild_id}_"
        files = [f for f in os.listdir(PLAYLISTS_DIR)
                 if f.startswith(prefix) and f.endswith(".json")]
        if not files:
            return await interaction.response.send_message(
                "No saved playlists for this server.", ephemeral=True)
        names = [f[len(prefix):-5].replace("_", " ") for f in files]
        await interaction.response.send_message(
            "**Saved Playlists:**\n" + "\n".join(f"• {n}" for n in names))

    @app_commands.command(name="autoplay", description="Toggle autoplay — queues a related track when the queue runs out")
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.autoplay = not state.autoplay
        if state.autoplay:
            await interaction.response.send_message(
                "🎲 Autoplay **enabled** — a related track will queue when the list runs out.")
        else:
            await interaction.response.send_message("🎲 Autoplay **disabled**.")

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
            "`/seek <seconds>` — Jump to a position in the current track\n"
            "`/volume <0-100>` — Set playback volume"
        ), inline=False)
        embed.add_field(name="📋 Queue", value=(
            "`/queue` — Show the current queue\n"
            "`/remove <position>` — Remove a track from the queue\n"
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
            "Radio keeps the bot in channel even when alone. Use `/stop` or `/leave` to end it."
        ), inline=False)
        embed.add_field(name="💾 Playlists", value=(
            "`/playlist save <name>` — Save the current queue as a playlist\n"
            "`/playlist load <name>` — Load and play a saved playlist\n"
            "`/playlist list` — List all saved playlists for this server"
        ), inline=False)
        embed.add_field(name="🛡️ Admin", value=(
            "`/djrole <role>` — Restrict music commands to a specific role (omit to clear)\n"
            "`/ytmonly` — Toggle strict YouTube Music mode (no YouTube fallback; direct links still work)\n"
            "Requires **Manage Server** permission."
        ), inline=False)
        embed.set_footer(text="Supports YouTube, Spotify tracks/albums/playlists, and direct stream URLs.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
