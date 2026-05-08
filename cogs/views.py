from __future__ import annotations

import random
import time
from collections import deque
from typing import TYPE_CHECKING

import discord

from .models import GuildState, Track
from .utils import fmt_dur, progress_bar

if TYPE_CHECKING:
    from .music import MusicCog


def build_np_embed(state: GuildState, track: Track) -> discord.Embed:
    pos = state.position()
    artist_line = f"\n{track.artist}" if track.artist else ""
    embed = discord.Embed(
        title="Now Playing",
        description=f"[{track.title}]({track.webpage_url}){artist_line}",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Progress",
        value=f"`{fmt_dur(pos)}` {progress_bar(pos, track.duration)} `{fmt_dur(track.duration)}`",
        inline=False,
    )
    embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
    embed.add_field(name="Loop", value=state.loop_mode, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_footer(text=f"Requested by {track.requester}")
    return embed


def build_queue_embed(state: GuildState, truncate_current: bool = False) -> discord.Embed:
    embed = discord.Embed(title="Queue", color=discord.Color.blurple())
    if state.current:
        pos = state.position()
        title = state.current.title[:50] if truncate_current else state.current.title
        embed.add_field(
            name="Now Playing",
            value=f"[{title}]({state.current.webpage_url}) "
                  f"`{fmt_dur(pos)} / {fmt_dur(state.current.duration)}`",
            inline=False,
        )
    if state.queue:
        lines = []
        char_budget = 950
        for i, t in enumerate(state.queue):
            title = (t.title[:50] + "…") if len(t.title) > 50 else t.title
            link = f"[{title}]({t.webpage_url})" if t.webpage_url else f"**{title}**"
            artist = f" — {t.artist}" if t.artist else ""
            dur = f" `{fmt_dur(t.duration)}`" if t.duration else ""
            line = f"`{i+1}.` {link}{artist}{dur}"
            if char_budget - len(line) - 1 < 0:
                break
            lines.append(line)
            char_budget -= len(line) + 1
        remaining = len(state.queue) - len(lines)
        if remaining:
            lines.append(f"*...and {remaining} more*")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Loop: {state.loop_mode} | Volume: {int(state.volume * 100)}%")
    return embed


class SearchView(discord.ui.View):
    def __init__(self, results: list[dict], on_pick):
        super().__init__(timeout=30)
        self._on_pick = on_pick
        self.results = results
        options = [
            discord.SelectOption(
                label=r.get("title", "Unknown")[:100],
                description=fmt_dur(r.get("duration", 0)),
                value=str(i),
            )
            for i, r in enumerate(results[:10])
        ]
        sel = discord.ui.Select(placeholder="Choose a track...", options=options)
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self._on_pick(interaction, self.results[int(interaction.data["values"][0])])
        self.stop()


class NowPlayingView(discord.ui.View):
    def __init__(self, cog: MusicCog, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self._sync_buttons()

    def _sync_buttons(self):
        state = self.cog.get_state(self.guild_id)
        guild = self.cog.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        paused = vc is not None and vc.is_paused()

        # Pause button: blue when paused so it stands out
        pause_btn = self.children[0]
        pause_btn.emoji = "▶️" if paused else "⏸"
        pause_btn.style = discord.ButtonStyle.primary if paused else discord.ButtonStyle.secondary

        # Autoplay button: green when on
        self.children[3].style = (
            discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        )

        # Loop button: 🔂 blue for track loop, 🔁 green for queue loop, gray when off
        loop_btn = self.children[4]
        if state.loop_mode == "track":
            loop_btn.emoji = "🔂"
            loop_btn.style = discord.ButtonStyle.primary
        elif state.loop_mode == "queue":
            loop_btn.emoji = "🔁"
            loop_btn.style = discord.ButtonStyle.success
        else:
            loop_btn.emoji = "🔁"
            loop_btn.style = discord.ButtonStyle.secondary

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = self.cog.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        if not vc:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        state = self.cog.get_state(self.guild_id)
        if vc.is_playing():
            vc.pause()
            state.pause_start = time.time()
            button.emoji = "▶️"
            button.style = discord.ButtonStyle.primary
        elif vc.is_paused():
            vc.resume()
            if state.pause_start and state.start_time:
                state.start_time += time.time() - state.pause_start
            state.pause_start = None
            button.emoji = "⏸"
            button.style = discord.ButtonStyle.secondary
        else:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = self.cog.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.secondary)
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        self.cog._cancel_prefetch(state)
        state.queue.clear()
        state.current = None
        state.radio_station = None
        guild = self.cog.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        if vc:
            vc.stop()
        await interaction.response.send_message("⏹️ Stopped.", ephemeral=True)

    @discord.ui.button(emoji="🎲", style=discord.ButtonStyle.secondary)
    async def toggle_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        state.autoplay = not state.autoplay
        button.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def cycle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        cycle = ["off", "track", "queue"]
        state.loop_mode = cycle[(cycle.index(state.loop_mode) + 1) % 3]
        if state.loop_mode == "track":
            button.emoji = "🔂"
            button.style = discord.ButtonStyle.primary
        elif state.loop_mode == "queue":
            button.emoji = "🔁"
            button.style = discord.ButtonStyle.success
        else:
            button.emoji = "🔁"
            button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        if not state.queue:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)
        await interaction.response.send_message(f"🔀 Shuffled {len(items)} tracks.", ephemeral=True)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def show_queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        if not state.current and not state.queue:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        await interaction.response.send_message(
            embed=build_queue_embed(state, truncate_current=True), ephemeral=True
        )

    @discord.ui.button(emoji="🎤", style=discord.ButtonStyle.secondary, row=1)
    async def show_lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        if not state.current:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        artist, title = self.cog._parse_lyrics_query(state.current.title, state.current)
        display = f"{artist} — {title}" if artist else title
        text = await self.cog._fetch_lyrics_text(artist, title)
        if not text:
            return await interaction.followup.send(f"No lyrics found for **{display}**.", ephemeral=True)
        chunks = [text[i:i+1900] for i in range(0, min(len(text), 5700), 1900)]
        await interaction.followup.send(f"**{display}**\n```{chunks[0]}```", ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(f"```{chunk}```", ephemeral=True)
