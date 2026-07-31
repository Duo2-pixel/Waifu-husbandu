# Waifu/Husbando Catcher Bot (standalone)

A self-contained Telegram bot: characters spawn randomly in groups, first
person to `/guess` the name correctly adds it to their harem.

## Setup

1. **New bot token** — talk to [@BotFather](https://t.me/BotFather), `/newbot`,
   copy the token into `BOT_TOKEN` in your `.env`.
2. **MongoDB** — free tier on [MongoDB Atlas](https://www.mongodb.com/cloud/atlas)
   works fine. Copy the connection string into `MONGO_URI`.
3. **Your user ID** — message [@userinfobot](https://t.me/userinfobot), copy the
   ID into `OWNER_ID`.
4. Copy `.env.example` to `.env` and fill it in.
5. Install deps: `pip install -r requirements.txt`
6. Run: `python main.py` (polling mode — works locally, no public URL needed)

## Adding characters

Two ways, both sudo-only:

- `/upload <img_url> <character-name> <anime-name> <rarity 1-4>`
- Set `WAIFU_SOURCE_CHAT_ID` to a group's chat ID, then post a photo there
  captioned `Character Name | Anime Name | Rarity(1-4)` — the bot grabs the
  photo automatically using Telegram's own file storage (no image hosting
  needed).

## Commands

**Everyone**
- `/guess <name>` (aliases: `/claim /catch /grab /hunt /collect`)
- `/harem` (alias `/mywaifus`) — view your collection
- `/fav <id>` — set a favorite
- `/ctop`, `/gtop`, `/chartop` — leaderboards

**Sudo only**
- `/spawn` — force a spawn right now
- `/upload`, `/delete`, `/wedit` — manage characters
- `/wstats` — DB stats

## Deploying (Render / Railway / etc.)

Set `WEBHOOK_URL` to your public HTTPS base URL and `PORT` if required by
the platform — the bot will automatically switch from polling to webhook
mode on startup, same as it does locally when `WEBHOOK_URL` is unset.
