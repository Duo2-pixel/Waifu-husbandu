# waifu_catcher.py
"""
Standalone Waifu/Husbando catcher game — ported from the WAIFU-HUSBANDO-CATCHER
bot (shivu/modules/*), rebuilt on python-telegram-bot + MongoDB (motor).

How characters get into the game
---------------------------------
This system does NOT auto-fetch characters from anywhere. A sudo user adds
each one either with:
  1. /upload <img_url> <character-name> <anime-name> <rarity 1-4>, or
  2. by posting a photo with caption "Name | Anime | Rarity" into the group
     set as WAIFU_SOURCE_CHAT_ID (see handle_source_photo below).
Once characters exist in the DB, the bot randomly spawns one of them in a
group every ~100 messages (tweak SPAWN_EVERY below), and the first person to
/guess the name correctly adds it to their harem.

Call `waifu_catcher.init(mongo_client, owner_id=..., sudo_ids=[...])` once at
startup, before any of these handlers are used.
"""
import asyncio
import logging
import math
import os
import random
import html
from difflib import SequenceMatcher
from itertools import groupby

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# --- Config knobs -----------------------------------------------------
SPAWN_EVERY = 100          # spawn a new character every N group messages
GUESS_SIMILARITY_THRESHOLD = 0.82  # 0-1, lower = more typo-tolerant
RARITY_MAP = {1: "⚪ Common", 2: "🟣 Rare", 3: "🟡 Legendary", 4: "🟢 Medium", 5: "💮 Special edition"}

# --- Module state (populated by init()) --------------------------------
db = None
collection = None                  # all characters
user_collection = None             # per-user harems
group_user_totals_collection = None  # per-(user,group) guess counts
top_global_groups_collection = None  # per-group guess counts
sequences_collection = None

_message_counts = {}      # chat_id -> int
_last_characters = {}     # chat_id -> character dict currently spawned
_first_correct = {}       # chat_id -> user_id who already claimed the active spawn

SOURCE_CHAT_ID = None      # the group/channel people post character photos into
ARCHIVE_CHAT_ID = None      # permanent channel every character photo gets copied into
OWNER_ID = None
SUDO_IDS = set()


def is_ready() -> bool:
    return collection is not None


def is_sudo(user_id: int) -> bool:
    return OWNER_ID is not None and (user_id == OWNER_ID or user_id in SUDO_IDS)


def init(mongo_client, owner_id: int = None, sudo_ids=None) -> None:
    """Wire this module up to the bot's Mongo client.

    owner_id: your Telegram numeric user ID (get it from @userinfobot) — always
    treated as sudo.
    sudo_ids: iterable of additional Telegram user IDs allowed to /upload,
    /delete, /wedit, /spawn, and post into the source group.
    """
    global db, collection, user_collection, group_user_totals_collection
    global top_global_groups_collection, sequences_collection, SOURCE_CHAT_ID
    global ARCHIVE_CHAT_ID, OWNER_ID, SUDO_IDS

    OWNER_ID = owner_id
    SUDO_IDS = set(sudo_ids or [])

    raw_source = os.getenv("WAIFU_SOURCE_CHAT_ID")
    if raw_source:
        try:
            SOURCE_CHAT_ID = int(raw_source)
            logger.info(f"📸 Waifu source group configured: {SOURCE_CHAT_ID}")
        except ValueError:
            logger.warning(f"WAIFU_SOURCE_CHAT_ID is not a valid integer: {raw_source!r}")

    raw_archive = os.getenv("WAIFU_ARCHIVE_CHANNEL_ID")
    if raw_archive:
        try:
            ARCHIVE_CHAT_ID = int(raw_archive)
            logger.info(f"🗄️ Waifu archive channel configured: {ARCHIVE_CHAT_ID}")
        except ValueError:
            logger.warning(f"WAIFU_ARCHIVE_CHANNEL_ID is not a valid integer: {raw_archive!r}")

    if mongo_client is None:
        logger.info("ℹ️ No Mongo client available — waifu catcher game is disabled.")
        return

    db = mongo_client["waifu_catcher_db"]
    collection = db["characters"]
    user_collection = db["user_harems"]
    group_user_totals_collection = db["group_user_totals"]
    top_global_groups_collection = db["top_global_groups"]
    sequences_collection = db["sequences"]
    logger.info("💞 Waifu catcher game connected (MongoDB).")


def _not_ready_text():
    return ("💔 The waifu catcher game isn't set up yet — needs a `MONGO_URI` "
            "environment variable to be configured.")


async def _next_sequence(name: str) -> int:
    doc = await sequences_collection.find_one_and_update(
        {"_id": name}, {"$inc": {"value": 1}}, upsert=True, return_document=True
    )
    return doc["value"]


# --- Auto-spawn (called from main.py's group text handler on every message) ---

async def maybe_auto_spawn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_ready():
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    chat_id = chat.id

    if chat_id in _last_characters:
        return  # a character is already up, waiting to be guessed

    _message_counts[chat_id] = _message_counts.get(chat_id, 0) + 1
    if _message_counts[chat_id] < SPAWN_EVERY:
        return
    _message_counts[chat_id] = 0

    await _spawn(chat_id, context)


async def force_spawn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sudo-only: force a spawn right now."""
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo users only.")
        return
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    chat_id = update.effective_chat.id
    if chat_id in _last_characters:
        await update.message.reply_text("There's already an active character here — someone needs to /guess them first!")
        return
    ok = await _spawn(chat_id, context)
    if not ok:
        await update.message.reply_text("⚠️ No characters in the database yet — a sudo user needs to /upload some first.")


async def _spawn(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    all_characters = await collection.find({}).to_list(length=None)
    if not all_characters:
        return False

    character = random.choice(all_characters)
    _last_characters[chat_id] = character
    _first_correct.pop(chat_id, None)

    caption = (
        f"✨ A new {character['rarity']} character appeared!\n\n"
        f"/guess their name to add them to your harem 💫"
    )
    try:
        await context.bot.send_photo(chat_id=chat_id, photo=character["img_url"], caption=caption)
    except Exception as e:
        logger.warning(f"Could not send spawn photo: {e}")
        await context.bot.send_message(chat_id=chat_id, text=caption)
    return True


# --- Guessing / claiming ---

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _is_correct_guess(guess: str, character_name: str) -> bool:
    """Typo-tolerant check: exact/partial match OR close enough (>= GUESS_SIMILARITY_THRESHOLD)."""
    name_lower = character_name.lower()
    name_parts = name_lower.split()
    guess_parts = guess.split()

    # exact full-name or exact single-word match (fast path, no fuzziness needed)
    if sorted(name_parts) == sorted(guess_parts) or any(part == guess for part in name_parts):
        return True

    # fuzzy: whole guess vs full name, and vs each individual name part
    if _similar(guess, name_lower) >= GUESS_SIMILARITY_THRESHOLD:
        return True
    if any(_similar(guess, part) >= GUESS_SIMILARITY_THRESHOLD for part in name_parts):
        return True
    return False


async def guess_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in _last_characters:
        await update.message.reply_text("No character is currently up — wait for one to spawn, or ask a sudo user to /spawn one.")
        return
    if chat_id in _first_correct:
        await update.message.reply_text("❌ Already guessed by someone — try the next one!")
        return

    guess_text = " ".join(context.args).lower().strip() if context.args else ""
    if not guess_text:
        await update.message.reply_text("Usage: /guess <character name>")
        return
    if "()" in guess_text or "&" in guess_text:
        await update.message.reply_text("❌ That guess isn't valid.")
        return

    character = _last_characters[chat_id]

    if not _is_correct_guess(guess_text, character["name"]):
        await update.message.reply_text("❌ Not quite — try again!")
        return

    _first_correct[chat_id] = user.id
    claimed_character = _last_characters.pop(chat_id)
    _message_counts[chat_id] = 0

    # Add to the user's harem
    existing = await user_collection.find_one({"user_id": user.id})
    if existing:
        update_fields = {}
        if user.username != existing.get("username"):
            update_fields["username"] = user.username
        if user.first_name != existing.get("first_name"):
            update_fields["first_name"] = user.first_name
        if update_fields:
            await user_collection.update_one({"user_id": user.id}, {"$set": update_fields})
        await user_collection.update_one({"user_id": user.id}, {"$push": {"characters": claimed_character}})
    else:
        await user_collection.insert_one({
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "characters": [claimed_character],
            "favorites": [],
        })

    # Per-group per-user counter (for /ctop)
    await group_user_totals_collection.update_one(
        {"user_id": user.id, "group_id": chat_id},
        {"$set": {"username": user.username, "first_name": user.first_name},
         "$inc": {"count": 1}},
        upsert=True,
    )
    # Global per-group counter (for /gtop)
    await top_global_groups_collection.update_one(
        {"group_id": chat_id},
        {"$set": {"group_name": update.effective_chat.title}, "$inc": {"count": 1}},
        upsert=True,
    )

    keyboard = [[InlineKeyboardButton("See Harem", callback_data=f"whrm:0:{user.id}")]]
    await update.message.reply_text(
        f'<b><a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a></b> guessed correctly! ✅\n\n'
        f'<b>Name:</b> {claimed_character["name"]}\n'
        f'<b>Anime:</b> {claimed_character["anime"]}\n'
        f'<b>Rarity:</b> {claimed_character["rarity"]}\n\n'
        f'Added to your harem — use /harem to view it.',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# --- Harem viewing ---

async def harem_command(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return

    user_id = update.effective_user.id
    user = await user_collection.find_one({"user_id": user_id})
    if not user or not user.get("characters"):
        target = update.message or update.callback_query.message
        text = "💔 You haven't guessed any characters yet — keep an eye on your groups!"
        if update.message:
            await update.message.reply_text(text)
        else:
            await update.callback_query.edit_message_text(text)
        return

    characters = sorted(user["characters"], key=lambda c: (c["anime"], c["id"]))
    counts = {k: len(list(v)) for k, v in groupby(characters, key=lambda c: c["id"])}
    unique_characters = list({c["id"]: c for c in characters}.values())

    total_pages = max(1, math.ceil(len(unique_characters) / 15))
    page = max(0, min(page, total_pages - 1))
    page_chars = unique_characters[page * 15:(page + 1) * 15]

    lines = [f"<b>{html.escape(update.effective_user.first_name)}'s Harem — Page {page + 1}/{total_pages}</b>\n"]
    for anime, group in groupby(page_chars, key=lambda c: c["anime"]):
        group = list(group)
        lines.append(f"\n<b>{html.escape(anime)}</b>")
        for c in group:
            lines.append(f'{c["id"]} {c["name"]} ×{counts[c["id"]]}')
    text = "\n".join(lines)

    keyboard_rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"whrm:{page-1}:{user_id}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"whrm:{page+1}:{user_id}"))
    if nav:
        keyboard_rows.append(nav)
    reply_markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    fav_ids = user.get("favorites") or []
    fav_char = next((c for c in user["characters"] if c["id"] == fav_ids[0]), None) if fav_ids else None
    photo = fav_char["img_url"] if fav_char else random.choice(user["characters"])["img_url"]

    if update.message:
        await update.message.reply_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        try:
            await update.callback_query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass


async def harem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, page, owner_id = query.data.split(":")
    if query.from_user.id != int(owner_id):
        await query.answer("This isn't your harem!", show_alert=True)
        return
    await query.answer()
    await harem_command(update, context, page=int(page))


async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    if not context.args:
        await update.message.reply_text("Usage: /fav <character id>")
        return

    user_id = update.effective_user.id
    character_id = context.args[0]
    user = await user_collection.find_one({"user_id": user_id})
    if not user:
        await update.message.reply_text("You haven't guessed any characters yet.")
        return
    character = next((c for c in user["characters"] if c["id"] == character_id), None)
    if not character:
        await update.message.reply_text("That character isn't in your harem.")
        return

    await user_collection.update_one({"user_id": user_id}, {"$set": {"favorites": [character_id]}})
    await update.message.reply_text(f'⭐ {character["name"]} is now your favorite.')


# --- Source group intake: post a photo + caption there instead of typing /upload ---

def _parse_character_line(text: str):
    """Parse 'Name | Anime | Rarity(1-4)' (also accepts '/' or newline as separator).
    Returns (name, anime, rarity_label) or None if the text doesn't match.
    """
    text = (text or "").strip()
    parts = None
    for sep in ("|", "\n", "/"):
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            break
    if not parts or len(parts) < 3:
        return None
    raw_name, raw_anime, raw_rarity = parts[0], parts[1], parts[2]
    try:
        rarity = RARITY_MAP[int(raw_rarity)]
    except (KeyError, ValueError):
        return None
    return raw_name.title(), raw_anime.title(), rarity


_FORMAT_HELP = (
    "⚠️ Format galat/missing hai. Ye format use karo:\n"
    "`Character Name | Anime Name | Rarity(1-4)`\n"
    "e.g. `Muzan Kibutsuji | Demon Slayer | 3`"
)


async def _archive_photo(context: ContextTypes.DEFAULT_TYPE, file_id_or_url: str, caption: str) -> str:
    """If an archive channel is configured, re-send the photo there and store
    THAT copy's file_id instead of the original — so the character survives
    even if the original photo/message gets deleted anywhere else.
    Falls back to the original file_id/url if no archive channel is set, or
    if the archive send fails for some reason (e.g. bot not admin there).
    """
    if not ARCHIVE_CHAT_ID:
        return file_id_or_url
    try:
        msg = await context.bot.send_photo(chat_id=ARCHIVE_CHAT_ID, photo=file_id_or_url, caption=caption)
        return msg.photo[-1].file_id
    except Exception as e:
        logger.warning(f"⚠️ Could not archive photo, keeping original: {e}")
        return file_id_or_url


async def _insert_character(context: ContextTypes.DEFAULT_TYPE, file_id: str, name: str, anime: str, rarity: str) -> str:
    stored_file_id = await _archive_photo(context, file_id, f"{name} | {anime} | {rarity}")
    char_id = str(await _next_sequence("character_id")).zfill(2)
    character = {"id": char_id, "img_url": stored_file_id, "name": name, "anime": anime, "rarity": rarity}
    await collection.insert_one(character)
    return f"✅ Added — ID {char_id}: {name} ({anime}, {rarity})"


async def handle_source_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register this on filters.PHOTO (any chat). It only acts on messages that
    land in the configured WAIFU_SOURCE_CHAT_ID, so it's safe to leave attached
    everywhere else.
    Expected caption format: `Name | Anime | Rarity(1-4)`
    (also accepts `/` or a newline as the separator).
    """
    if not is_ready() or not SOURCE_CHAT_ID:
        return
    message = update.effective_message
    if not message or update.effective_chat.id != SOURCE_CHAT_ID or not message.photo:
        return

    user = update.effective_user
    if not is_sudo(user.id):
        await message.reply_text("🚫 Only sudo users can add characters from this group.")
        return

    # If there's no caption at all, this photo was probably dumped without one —
    # someone will reply to it later with the name/anime/rarity line instead.
    if not (message.caption or "").strip():
        return

    parsed = _parse_character_line(message.caption)
    if not parsed:
        await message.reply_text(_FORMAT_HELP, parse_mode="Markdown")
        return

    name, anime, rarity = parsed
    file_id = message.photo[-1].file_id  # highest-resolution version Telegram kept
    reply = await _insert_character(context, file_id, name, anime, rarity)
    await message.reply_text(reply)


async def handle_source_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register this on filters.TEXT (any chat). Lets you reply to an
    already-posted photo (e.g. one dumped without a caption) with a plain
    text line like `Kaori Miyazono | Shigatsu wa Kimi no Uso | 3` to add it
    — no need to re-post the photo with a caption.
    """
    if not is_ready() or not SOURCE_CHAT_ID:
        return
    message = update.effective_message
    if not message or update.effective_chat.id != SOURCE_CHAT_ID:
        return
    replied = message.reply_to_message
    if not replied or not replied.photo or not message.text:
        return

    user = update.effective_user
    if not is_sudo(user.id):
        return  # stay quiet for non-sudo replies in the group, avoid noise

    parsed = _parse_character_line(message.text)
    if not parsed:
        await message.reply_text(_FORMAT_HELP, parse_mode="Markdown")
        return

    name, anime, rarity = parsed
    file_id = replied.photo[-1].file_id
    reply = await _insert_character(context, file_id, name, anime, rarity)
    await message.reply_text(reply)


# --- Admin: upload / delete / edit ---

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sudo-only. Usage: /upload <img_url> <character-name> <anime-name> <rarity 1-4>"""
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo users only.")
        return
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Usage: /upload <img_url> <character-name> <anime-name> <rarity>\n"
            "e.g. /upload https://example.com/img.jpg muzan-kibutsuji demon-slayer 3\n\n"
            "rarity: 1 ⚪ Common, 2 🟣 Rare, 3 🟡 Legendary, 4 🟢 Medium"
        )
        return

    img_url, raw_name, raw_anime, raw_rarity = args
    character_name = raw_name.replace("-", " ").title()
    anime = raw_anime.replace("-", " ").title()
    try:
        rarity = RARITY_MAP[int(raw_rarity)]
    except (KeyError, ValueError):
        await update.message.reply_text("Invalid rarity. Use 1, 2, 3, or 4.")
        return

    reply = await _insert_character(context, img_url, character_name, anime, rarity)
    await update.message.reply_text(reply)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sudo-only. Usage: /delete <id>"""
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo users only.")
        return
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /delete <character id>")
        return
    result = await collection.find_one_and_delete({"id": context.args[0]})
    if result:
        await update.message.reply_text(f"🗑️ Deleted {result['name']} (ID {result['id']}).")
    else:
        await update.message.reply_text("No character found with that ID.")


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sudo-only. Usage: /wedit <id> <field> <new_value>  (field: img_url, name, anime, rarity)"""
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo users only.")
        return
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    args = context.args
    if len(args) != 3:
        await update.message.reply_text("Usage: /wedit <id> <field> <new_value>\nfields: img_url, name, anime, rarity")
        return

    char_id, field, raw_value = args
    character = await collection.find_one({"id": char_id})
    if not character:
        await update.message.reply_text("Character not found.")
        return

    valid_fields = ["img_url", "name", "anime", "rarity"]
    if field not in valid_fields:
        await update.message.reply_text(f"Invalid field. Use one of: {', '.join(valid_fields)}")
        return

    if field in ("name", "anime"):
        new_value = raw_value.replace("-", " ").title()
    elif field == "rarity":
        try:
            new_value = RARITY_MAP[int(raw_value)]
        except (KeyError, ValueError):
            await update.message.reply_text("Invalid rarity. Use 1, 2, 3, or 4.")
            return
    else:
        new_value = raw_value

    await collection.update_one({"id": char_id}, {"$set": {field: new_value}})
    await update.message.reply_text(f"✅ Updated {field} for character {char_id}.")


# --- Leaderboards / stats ---

async def ctop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top 10 users in THIS group by characters guessed."""
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    chat_id = update.effective_chat.id
    cursor = group_user_totals_collection.aggregate([
        {"$match": {"group_id": chat_id}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ])
    rows = await cursor.to_list(length=10)
    if not rows:
        await update.message.reply_text("No one has guessed a character in this group yet.")
        return
    lines = ["<b>🏆 Top guessers in this group</b>\n"]
    for i, r in enumerate(rows, start=1):
        name = html.escape((r.get("first_name") or "Unknown")[:15])
        lines.append(f"{i}. <b>{name}</b> ➾ {r['count']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def gtop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top 10 groups globally by characters guessed."""
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    cursor = top_global_groups_collection.aggregate([
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ])
    rows = await cursor.to_list(length=10)
    if not rows:
        await update.message.reply_text("No group activity yet.")
        return
    lines = ["<b>🏆 Top groups globally</b>\n"]
    for i, r in enumerate(rows, start=1):
        name = html.escape((r.get("group_name") or "Unknown")[:20])
        lines.append(f"{i}. <b>{name}</b> ➾ {r['count']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def chartop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top 10 users globally by total characters collected."""
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    cursor = user_collection.aggregate([
        {"$project": {"first_name": 1, "character_count": {"$size": {"$ifNull": ["$characters", []]}}}},
        {"$sort": {"character_count": -1}},
        {"$limit": 10},
    ])
    rows = await cursor.to_list(length=10)
    if not rows:
        await update.message.reply_text("No one has collected any characters yet.")
        return
    lines = ["<b>🏆 Top collectors globally</b>\n"]
    for i, r in enumerate(rows, start=1):
        name = html.escape((r.get("first_name") or "Unknown")[:15])
        lines.append(f"{i}. <b>{name}</b> ➾ {r['character_count']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def wstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sudo-only: overall waifu-catcher DB stats."""
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo users only.")
        return
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    char_count = await collection.count_documents({})
    user_count = await user_collection.count_documents({})
    group_ids = await group_user_totals_collection.distinct("group_id")
    await update.message.reply_text(
        f"📊 Waifu catcher stats\nCharacters: {char_count}\nPlayers: {user_count}\nActive groups: {len(group_ids)}"
    )


async def archiveall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sudo-only, one-time migration: re-sends every existing character's photo
    into WAIFU_ARCHIVE_CHANNEL_ID and updates the DB to point at that permanent
    copy. Safe to run more than once — already-archived characters just get
    re-archived (harmless, just wastes a little time).
    """
    if not is_sudo(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo users only.")
        return
    if not is_ready():
        await update.message.reply_text(_not_ready_text())
        return
    if not ARCHIVE_CHAT_ID:
        await update.message.reply_text(
            "⚠️ WAIFU_ARCHIVE_CHANNEL_ID isn't set. Add the archive channel's ID "
            "to your .env, make sure the bot is an admin there, and restart the bot first."
        )
        return

    characters = await collection.find({}).to_list(length=None)
    total = len(characters)
    if total == 0:
        await update.message.reply_text("No characters in the database yet.")
        return

    status_msg = await update.message.reply_text(f"⏳ Archiving {total} characters... this'll take a bit (rate-limited).")
    migrated, failed = 0, 0
    for i, c in enumerate(characters, start=1):
        try:
            caption = f"{c['name']} | {c['anime']} | {c['rarity']}"
            new_file_id = await _archive_photo(context, c["img_url"], caption)
            if new_file_id != c["img_url"]:
                await collection.update_one({"_id": c["_id"]}, {"$set": {"img_url": new_file_id}})
                migrated += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Archive failed for character {c.get('id')}: {e}")
        await asyncio.sleep(1.2)  # stay well under Telegram's per-chat rate limit
        if i % 20 == 0:
            try:
                await status_msg.edit_text(f"⏳ Archiving... {i}/{total} done ({migrated} migrated, {failed} failed)")
            except Exception:
                pass

    await status_msg.edit_text(f"✅ Migration done — {migrated} archived, {failed} failed, {total} total.")
