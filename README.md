# YouTube MP3 Bot

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

[🇷🇺 Читать по-русски](README.ru.md)

A Telegram bot built on `python-telegram-bot` + `yt-dlp`: downloads audio from YouTube (MP3/WAV, choice of bitrate), downloads TikTok videos, searches YouTube right from the chat, and keeps simple usage stats for the admin.

## Features

- **YouTube → MP3 / WAV.** Send a link, the bot offers a format and bitrate (128/192/320 kbps).
- **YouTube search.** `/search query` — up to 15 paginated results, pick one with a button.
- **TikTok.** Send a TikTok link and the video is downloaded straight into the chat — three independent methods are tried in case one gets blocked.
- **Duration limit.** Configurable cap in minutes, so an hour-long video isn't downloaded by mistake.
- **Admin stats & moderation.** `/stats` — totals and per-user breakdown, `/clear N` — bulk-delete the last N text messages in a chat (media and links are skipped).
- **Resilient downloads.** If the primary download method fails, the bot automatically retries, then falls back to an alternate format, then to a backup external service.

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) — must be available on `PATH` (needed to convert audio to MP3/WAV)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (optional) [Deno](https://deno.com/) — modern `yt-dlp` uses it to solve YouTube's JS challenges; without it some formats may be unavailable

## Installation

```bash
git clone https://github.com/<your-username>/youtube-mp3-bot.git
cd youtube-mp3-bot

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your token:

```bash
cp .env.example .env
```

```env
BOT_TOKEN=your_botfather_token
ADMIN_IDS=your_telegram_id
```

Run it:

```bash
python main.py
```

On Windows you can just run `start.bat`; on Linux/macOS, `./start.sh`.

## Cookies (optional)

For private or age-restricted videos, YouTube may require authentication. Export your browser cookies (e.g. with the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension) into a `cookies.txt` file next to the bot — it's picked up automatically. The bot works fine without it too; some videos just won't be available.

**Never publish `cookies.txt`** — it's effectively a key to your Google account. It's already covered by `.gitignore`.

## Behind a restrictive network / VPN

If YouTube or Telegram are throttled or blocked where you run the bot, `config.py` supports two independent proxies:

- `TG_PROXY` — a SOCKS5 proxy just for the Telegram Bot API (e.g. a local VPN client's SOCKS port).
- `YOUTUBE_PROXY` — a proxy just for YouTube traffic (yt-dlp). Leave it empty if you're using a system-level DPI-circumvention tool (like [Zapret](https://github.com/bol-van/zapret), popular in Russia) instead — in that case YouTube traffic already bypasses restrictions at the OS level and yt-dlp doesn't need its own proxy.

Both are set via `.env` (see `.env.example`) and can be used independently or together.

## Commands

| Command | Description |
|---|---|
| `/start` | greeting and a short how-to |
| `/help` | command reference |
| `/search query` | search YouTube (up to 15 results) |
| `/stats` | download stats (admins only, see `ADMIN_IDS`) |
| `/clear N` | delete the last N text messages, starting from the one you reply to (admins only) |

Just send a YouTube or TikTok link — the bot handles the rest.

## Project structure

```
main.py        — entry point, logging setup, handler registration
handlers.py     — command/message/button handlers
downloader.py   — all YouTube download & conversion logic
tiktok.py       — TikTok video downloading (3 independent methods)
startup.py      — chat tracking, startup notification
config.py       — configuration (secrets come from .env)
```

## Known limitations

- The backup external API for YouTube (only used if yt-dlp fails completely) is an unofficial third-party service and can be unstable on its own.
- YouTube regularly changes its anti-bot defenses — if downloads suddenly stop working across the board, try `pip install -U yt-dlp` first.
- The bot doesn't check content rights — using it to download material you don't have rights to is on whoever runs it.

## License

[MIT](LICENSE)
