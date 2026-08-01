import logging
import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

import waifu_catcher

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Required config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")                 # from @BotFather — make a NEW bot for this
MONGO_URI = os.getenv("MONGO_URI")                 # e.g. mongodb+srv://user:pass@cluster.mongodb.net
OWNER_ID = os.getenv("OWNER_ID")                    # your numeric Telegram user ID (@userinfobot)
SUDO_IDS = os.getenv("SUDO_IDS", "")                # comma-separated extra admin user IDs, optional

# --- Optional: webhook (same pattern as Laila) ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL")              # leave unset to run in polling mode locally
PORT = int(os.getenv("PORT", "8000"))


async def post_init(application: Application) -> None:
    mongo_client = AsyncIOMotorClient(MONGO_URI) if MONGO_URI else None
    owner_id = int(OWNER_ID) if OWNER_ID else None
    sudo_ids = {int(x) for x in SUDO_IDS.split(",") if x.strip().isdigit()}

    if not mongo_client:
        logger.warning("⚠️ MONGO_URI not set — the waifu game will not work until it is.")
    if not owner_id:
        logger.warning("⚠️ OWNER_ID not set — no one will be able to use sudo commands (/upload, /spawn, etc.).")

    waifu_catcher.init(mongo_client, owner_id=owner_id, sudo_ids=sudo_ids)

    bot_info = await application.bot.get_me()
    logger.info(f"✅ {bot_info.first_name} (@{bot_info.username}) is online.")


async def start_command(update: Update, context) -> None:
    await update.message.reply_text(
        "👋 Hey! I'm a waifu/husbando catcher bot.\n\n"
        "Add me to a group and characters will randomly spawn there — "
        "use /guess <name> to add them to your /harem.\n\n"
        "Sudo users can add characters with /upload, or by posting a photo "
        "captioned `Name | Anime | Rarity` into the configured source group.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context) -> None:
    await update.message.reply_text(
        "<b>Gameplay</b>\n"
        "/guess <name> — guess the spawned character (aliases: /claim /catch /grab /hunt /collect)\n"
        "/harem — view your collection (alias: /mywaifus)\n"
        "/fav <id> — set a favorite\n"
        "/ctop — top guessers in this group\n"
        "/gtop — top groups globally\n"
        "/chartop — top collectors globally\n\n"
        "<b>Sudo only</b>\n"
        "/spawn — force a spawn now\n"
        "/upload <img_url> <name> <anime> <rarity 1-4> — add a character\n"
        "/delete <id> — remove a character\n"
        "/wedit <id> <field> <value> — edit img_url/name/anime/rarity\n"
        "/wstats — DB stats\n"
        "/archiveall — one-time: migrate all existing characters' photos into the archive channel\n\n"
        "Or post a photo captioned <code>Name | Anime | Rarity</code> into the "
        "group set as WAIFU_SOURCE_CHAT_ID to add a character without typing /upload.",
        parse_mode="HTML",
    )


async def error_handler(update, context) -> None:
    logger.error("Unhandled exception", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN is not set. Get one from @BotFather and put it in your .env")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    # --- Gameplay ---
    application.add_handler(CommandHandler(["guess", "claim", "catch", "grab", "hunt", "collect"], waifu_catcher.guess_command))
    application.add_handler(CommandHandler(["harem", "mywaifus", "collection"], waifu_catcher.harem_command))
    application.add_handler(CallbackQueryHandler(waifu_catcher.harem_callback, pattern="^whrm"))
    application.add_handler(CommandHandler("fav", waifu_catcher.fav_command))
    application.add_handler(CommandHandler("ctop", waifu_catcher.ctop_command))
    application.add_handler(CommandHandler("gtop", waifu_catcher.gtop_command))
    application.add_handler(CommandHandler("chartop", waifu_catcher.chartop_command))

    # --- Sudo only ---
    application.add_handler(CommandHandler("spawn", waifu_catcher.force_spawn_command))
    application.add_handler(CommandHandler("upload", waifu_catcher.upload_command))
    application.add_handler(CommandHandler("delete", waifu_catcher.delete_command))
    application.add_handler(CommandHandler("wedit", waifu_catcher.update_command))
    application.add_handler(CommandHandler("wstats", waifu_catcher.wstats_command))
    application.add_handler(CommandHandler("archiveall", waifu_catcher.archiveall_command))

    # --- Source group photo intake (caption on the photo itself...) ---
    application.add_handler(MessageHandler(filters.PHOTO, waifu_catcher.handle_source_photo))
    # --- ...or reply to an already-posted photo with the Name | Anime | Rarity line ---
    # (own handler group so a normal reply in any other group doesn't block the
    # auto-spawn message counter below, which also matches text messages)
    application.add_handler(MessageHandler(
        filters.TEXT & filters.REPLY & (~filters.COMMAND),
        waifu_catcher.handle_source_reply,
    ), group=1)

    # --- Message counter that drives auto-spawn (group chats only, must stay last) ---
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & (~filters.COMMAND),
        waifu_catcher.maybe_auto_spawn,
    ))

    application.add_error_handler(error_handler)

    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
        logger.info(f"Bot started with webhook on port {PORT}")
    else:
        logger.info("Starting bot in polling mode.")
        application.run_polling()


if __name__ == "__main__":
    main()
