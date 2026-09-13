import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.request import HTTPXRequest
from config import (
    BOT_TOKEN, TG_PROXY, YOUTUBE_PROXY, SAVE_DIR, COOKIES_FILE, YOUTUBE_REGEX,
    TIKTOK_REGEX, TIKTOK_COOKIES_FILE,
    MAX_DURATION_MINUTES, LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT
)
from handlers import (
    handle_message, button_handler, search_command,
    help_command, stats_command, clear_command
)
from startup import notify_admins, add_chat


# --- Logging setup: file (rotating) + console, same format for both ---
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, LOG_FILE)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    return logging.getLogger(__name__)


# --- /start command ---
async def start_command(update: Update, context):
    await asyncio.to_thread(add_chat, update.effective_chat.id)
    await update.message.reply_text(
        "Привет! Я скачиваю аудио с YouTube.\n\n"
        "Как использовать:\n"
        "1. Отправь ссылку на видео\n"
        "2. Выбери формат и битрейт (128/192/320 kbps)\n\n"
        "Команды:\n"
        "/search запрос — поиск видео на YouTube\n"
        "/help — справка\n"
        "/clear N — удалить N последних текстовых сообщений (админ)"
    )


# --- Global error handler: keeps benign Telegram API hiccups out of ERROR logs ---
async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Logs benign errors (stale buttons, brief network drops) as warnings;
    everything else is logged as an error with full traceback."""
    err = context.error
    benign = (
        (isinstance(err, BadRequest) and (
            "Message is not modified" in str(err) or
            "Query is too old" in str(err) or
            "message to edit not found" in str(err).lower()
        )) or
        isinstance(err, (NetworkError, TimedOut))
    )
    if benign:
        logger = logging.getLogger(__name__)
        logger.warning(f"Некритичная ошибка Telegram API: {err}")
    else:
        logger = logging.getLogger(__name__)
        logger.error("Необработанная ошибка", exc_info=err)


# --- Entry point: build the Application, register handlers, start polling ---
def main():
    logger = setup_logging()

    os.makedirs(SAVE_DIR, exist_ok=True)
    logger.info(f"Папка сохранения: {os.path.abspath(SAVE_DIR)}")
    logger.info(f"Куки: {'найдены' if os.path.exists(COOKIES_FILE) else 'не найдены!'}")
    logger.info(f"Макс. длительность видео: {MAX_DURATION_MINUTES} мин" if MAX_DURATION_MINUTES > 0 else "Ограничение длительности: выключено")

    config = {
        "YOUTUBE_PROXY": YOUTUBE_PROXY,
        "SAVE_DIR": SAVE_DIR,
        "COOKIES_FILE": COOKIES_FILE,
        "YOUTUBE_REGEX": YOUTUBE_REGEX,
        "TIKTOK_REGEX": TIKTOK_REGEX,
        "TIKTOK_COOKIES_FILE": TIKTOK_COOKIES_FILE,
        "MAX_DURATION_MINUTES": MAX_DURATION_MINUTES,
    }

    if TG_PROXY:
        request = HTTPXRequest(proxy=TG_PROXY, connect_timeout=30, read_timeout=120)
        app = Application.builder().token(BOT_TOKEN).request(request).build()
    else:
        app = Application.builder().token(BOT_TOKEN).build()

    app.post_init = lambda app: notify_admins(app)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler(
        "search",
        lambda u, c: search_command(u, c, config)
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        lambda u, c: handle_message(u, c, config)
    ))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: button_handler(u, c, config)
    ))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()