import os
import json
import logging
import tempfile
import threading

logger = logging.getLogger(__name__)

CHATS_FILE = "chats.json"

# --- chats.json persistence: tracks every chat the bot has been used in ---
# Guards concurrent read-modify-write of chats.json, since add_chat/remove_chat
# can be called from multiple worker threads at once.
_chats_lock = threading.Lock()


def load_chats():
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось прочитать {CHATS_FILE}: {e}")
    return []


def save_chats(chats):
    # Atomic write: temp file + os.replace, so a crash mid-write never
    # leaves a corrupted chats.json.
    dir_name = os.path.dirname(os.path.abspath(CHATS_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(chats, f)
        os.replace(tmp_path, CHATS_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def add_chat(chat_id):
    with _chats_lock:
        chats = load_chats()
        if chat_id not in chats:
            chats.append(chat_id)
            save_chats(chats)


def get_chats():
    return load_chats()


def remove_chat(chat_id):
    with _chats_lock:
        chats = load_chats()
        if chat_id in chats:
            chats.remove(chat_id)
            save_chats(chats)


# --- Startup notification: pings every known chat when the bot comes online ---
async def notify_admins(application):
    """Sends a "bot is up" message to every chat that has used the bot."""
    bot = application.bot
    chats = get_chats()

    if not chats:
        logger.warning("Список чатов пуст — некому отправлять уведомление")
        return

    for chat_id in chats:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Бот запущен и готов к работе."
            )
            logger.info(f"Уведомление отправлено в чат {chat_id}")
        except Exception as e:
            logger.warning(f"Не удалось отправить в чат {chat_id}: {e}")
            if "Forbidden" in str(e) or "deactivated" in str(e) or "chat not found" in str(e).lower():
                remove_chat(chat_id)
