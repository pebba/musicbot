import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True


class MusicBot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("cogs.music")

    async def on_ready(self):
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[musicbot] Failed to sync guild {guild.id}: {e}")
        await self.tree.sync()
        print(f"Logged in as {self.user} — synced to {len(self.guilds)} guild(s).")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/help",
            )
        )


bot = MusicBot(command_prefix=commands.when_mentioned, intents=intents)
bot.run(os.getenv("DISCORD_TOKEN"))
