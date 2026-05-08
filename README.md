# Discord Music Bot 🎵

A self-hosted Discord music bot with YouTube Music, Spotify, live radio, autoplay, lyrics, and playlist support.

## Features

- **YouTube & YouTube Music** — play songs, albums, and playlists by name or URL
- **Spotify** — play tracks and albums via URL (playlists via yt-dlp fallback)
- **Autoplay** — automatically queues related songs when the queue runs out, with cooldown and popularity weighting to avoid repetition
- **Live Radio** — built-in radio streams
- **Lyrics** — fetches lyrics via lrclib.net with lyrics.ovh as fallback
- **Playlists** — save and load per-server playlists
- **Now Playing panel** — interactive buttons for pause, skip, stop, autoplay, loop, shuffle, queue, and lyrics
- **DJ role** — optionally restrict controls to a specific role

## Commands

| Command | Description |
|---------|-------------|
| `/play <query>` | Play a song, playlist, or URL (YouTube / Spotify) |
| `/playnext <query>` | Queue a song to play right after the current track |
| `/pause` / `/resume` | Pause or resume playback |
| `/stop` | Stop playback and clear the queue |
| `/skip [n]` | Skip to the next track (or ahead to position n) |
| `/seek <seconds>` | Jump to a position in the current track |
| `/volume <0-100>` | Set playback volume |
| `/nowplaying` | Show the currently playing track with progress bar |
| `/queue` | Show the current queue |
| `/remove <position>` | Remove a track from the queue |
| `/clear` | Clear the queue |
| `/shuffle` | Shuffle the queue |
| `/loop [off\|track\|queue]` | Set loop mode |
| `/search <query>` | Search and pick from results |
| `/lyrics [song]` | Fetch lyrics for the current or a specified song |
| `/autoplay` | Toggle autoplay |
| `/radio <station>` | Play a 24/7 live radio stream |
| `/playlist save <name>` | Save the current queue as a playlist |
| `/playlist load <name>` | Load a saved playlist |
| `/playlist list` | List saved playlists for this server |
| `/join` / `/leave` | Join or leave a voice channel |
| `/djrole <role>` | Restrict commands to a role (Manage Server required) |
| `/ytmonly` | Toggle strict YouTube Music mode (Manage Server required) |
