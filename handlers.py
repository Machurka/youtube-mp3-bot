import re
import uuid
import logging
import json
import os
import asyncio
import yt_dlp
from cachetools import TTLCache
from downloader import download_and_send, get_ydl_opts, check_duration
from tiktok import download_tiktok
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import RetryAfter, TelegramError
from config import ADMIN_IDS, MP3_BITRATES, DEFAULT_MP3_BITRATE, STATS_FILE
from startup import add_chat

logger = logging.getLogger(__name__)

# In-memory caches for pending links, bitrate choices and search
# results, keyed by short random ids. TTLCache auto-expires old entries
# instead of growing forever.
_CACHE_TTL_SECONDS = 15 * 60  # ссылка/результат поиска "живёт" 15 минут

pending_urls = TTLCache(maxsize=5000, ttl=_CACHE_TTL_SECONDS)
pending_bitrate = TTLCache(maxsize=5000, ttl=_CACHE_TTL_SECONDS)
search_results = TTLCache(maxsize=5000, ttl=_CACHE_TTL_SECONDS)
search_pages = TTLCache(maxsize=2000, ttl=_CACHE_TTL_SECONDS)


# --- Message intake: detect a YouTube/TikTok link and route it ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, config):
    if update.message is None or not update.message.text:
        return

    await asyncio.to_thread(add_chat, update.effective_chat.id)
    text = update.message.text

    # --- TikTok ---
    tiktok_urls = re.findall(config.get("TIKTOK_REGEX", ""), text)
    if tiktok_urls:
        url = tiktok_urls[0]
        logger.info(f"TikTok от {update.effective_user.username}: {url}")
        await download_tiktok(
            chat_id=update.effective_chat.id,
            url=url,
            context=context,
            youtube_proxy=config["YOUTUBE_PROXY"],
            cookies_file=config.get("TIKTOK_COOKIES_FILE", "")
        )
        return

    # --- YouTube ---
    urls = re.findall(config["YOUTUBE_REGEX"], text)
    if not urls:
        return

    url = urls[0]
    chat_id = update.effective_chat.id
    logger.info(f"Ссылка от {update.effective_user.username}: {url}")

    link_id = str(uuid.uuid4())[:8]
    pending_urls[link_id] = url
    pending_bitrate[link_id] = DEFAULT_MP3_BITRATE

    keyboard = build_format_keyboard(link_id)

    try:
        await update.message.reply_text(
            f"Выбери формат (битрейт MP3: {DEFAULT_MP3_BITRATE} kbps):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Выбери формат (битрейт MP3: {DEFAULT_MP3_BITRATE} kbps):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# --- Inline keyboard builders ---

def build_format_keyboard(link_id):
    br = pending_bitrate.get(link_id, DEFAULT_MP3_BITRATE)
    keyboard = [
        [
            InlineKeyboardButton("MP3", callback_data=f"fmt_mp3|{link_id}"),
            InlineKeyboardButton("WAV", callback_data=f"fmt_wav|{link_id}"),
        ],
        [
            InlineKeyboardButton("MP3 + WAV", callback_data=f"fmt_both|{link_id}"),
        ],
        [
            InlineKeyboardButton(f"Битрейт: {br} kbps", callback_data=f"bitrate_menu|{link_id}"),
        ],
        [
            InlineKeyboardButton("Отмена", callback_data=f"fmt_cancel|{link_id}"),
        ],
    ]
    return keyboard

def build_bitrate_keyboard(link_id):
    current = pending_bitrate.get(link_id, DEFAULT_MP3_BITRATE)
    keyboard = []
    for br in MP3_BITRATES:
        prefix = "> " if br == current else "  "
        keyboard.append([InlineKeyboardButton(
            f"{prefix}{br} kbps",
            callback_data=f"bitrate_set|{link_id}|{br}"
        )])
    keyboard.append([InlineKeyboardButton(
        "Назад к форматам",
        callback_data=f"bitrate_back|{link_id}"
    )])
    return keyboard

# --- Callback query dispatch: every inline button press lands here ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, config):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # --- Меню битрейта ---
    if data.startswith("bitrate_menu|"):
        parts = data.split("|")
        link_id = parts[1]
        if link_id not in pending_urls:
            await query.edit_message_text("Ссылка устарела.")
            return
        keyboard = build_bitrate_keyboard(link_id)
        await query.edit_message_text(
            "Выбери битрейт MP3:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("bitrate_set|"):
        parts = data.split("|")
        link_id = parts[1]
        bitrate = int(parts[2])
        if link_id not in pending_urls:
            await query.edit_message_text("Ссылка устарела.")
            return
        pending_bitrate[link_id] = bitrate
        keyboard = build_format_keyboard(link_id)
        await query.edit_message_text(
            f"Выбери формат (битрейт MP3: {bitrate} kbps):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("bitrate_back|"):
        parts = data.split("|")
        link_id = parts[1]
        if link_id not in pending_urls:
            await query.edit_message_text("Ссылка устарела.")
            return
        keyboard = build_format_keyboard(link_id)
        br = pending_bitrate.get(link_id, DEFAULT_MP3_BITRATE)
        await query.edit_message_text(
            f"Выбери формат (битрейт MP3: {br} kbps):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- Выбор формата ---
    elif data.startswith("fmt_"):
        parts = data.split("|")
        if len(parts) != 2:
            return
        action = parts[0][4:]
        link_id = parts[1]

        url = pending_urls.pop(link_id, None)
        bitrate = pending_bitrate.pop(link_id, DEFAULT_MP3_BITRATE)
        if not url:
            await query.edit_message_text("Ссылка устарела.")
            return
        if action == "cancel":
            await query.edit_message_text("Отменено.")
            return

        max_duration = config.get("MAX_DURATION_MINUTES", 0)
        if max_duration > 0:
            duration_ok, duration_msg = await check_duration(
                url,
                youtube_proxy=config["YOUTUBE_PROXY"],
                cookies_file=config["COOKIES_FILE"],
                max_minutes=max_duration
            )
            if not duration_ok:
                await query.edit_message_text(duration_msg)
                return

        await download_and_send(
            chat_id=query.message.chat_id,
            url=url,
            format_type=action,
            context=context,
            youtube_proxy=config["YOUTUBE_PROXY"],
            cookies_file=config["COOKIES_FILE"],
            save_dir=config["SAVE_DIR"],
            status_message=query.message,
            mp3_bitrate=bitrate,
        )

    # --- Выбор результата поиска ---
    elif data.startswith("search_result|"):
        parts = data.split("|")
        if len(parts) != 2:
            return
        url = search_results.pop(parts[1], None)
        if not url:
            await query.edit_message_text("Результат устарел.")
            return

        max_duration = config.get("MAX_DURATION_MINUTES", 0)
        if max_duration > 0:
            duration_ok, duration_msg = await check_duration(
                url,
                youtube_proxy=config["YOUTUBE_PROXY"],
                cookies_file=config["COOKIES_FILE"],
                max_minutes=max_duration
            )
            if not duration_ok:
                await query.edit_message_text(duration_msg)
                return

        await query.message.delete()

        link_id = str(uuid.uuid4())[:8]
        pending_urls[link_id] = url
        pending_bitrate[link_id] = DEFAULT_MP3_BITRATE

        keyboard = build_format_keyboard(link_id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"{url}\n\nВыбери формат (битрейт MP3: {DEFAULT_MP3_BITRATE} kbps):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- Пагинация ---
    elif data.startswith("page_prev") or data.startswith("page_next"):
        parts = data.split("|")
        action = parts[0]
        search_query = parts[1]

        if user_id not in search_pages:
            await query.answer("Данные устарели, повторите поиск.")
            return

        page_data = search_pages[user_id]
        current_page = page_data["page"]
        total_pages = page_data["total_pages"]

        if action == "page_prev":
            new_page = max(0, current_page - 1)
        else:
            new_page = min(total_pages - 1, current_page + 1)

        if new_page == current_page:
            return

        page_data["page"] = new_page
        entries = page_data["entries"]
        keyboard = build_search_keyboard(new_page, total_pages, search_query, entries)

        await query.edit_message_text(
            f"Результаты: {search_query} (стр. {new_page + 1}/{total_pages})",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("search_cancel"):
        await query.edit_message_text("Поиск отменён.")
        if user_id in search_pages:
            del search_pages[user_id]


# --- /search: YouTube search with paginated results ---

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE, config):
    await asyncio.to_thread(add_chat, update.effective_chat.id)

    query_text = " ".join(context.args)
    if not query_text:
        await update.message.reply_text("Использование: /search запрос")
        return

    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("Ищу...")

    try:
        search_opts = get_ydl_opts({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": True,
        }, youtube_proxy=config["YOUTUBE_PROXY"], cookies_file=config["COOKIES_FILE"])

        def _search_sync():
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                return ydl.extract_info(f"ytsearch15:{query_text}", download=False)

        # Blocking network call — run off the event loop.
        info = await asyncio.to_thread(_search_sync)

        entries = info.get("entries", []) if info else []

        valid_entries = []
        for e in entries:
            if e is None:
                continue
            if e.get("_type") == "playlist":
                continue
            url = e.get("webpage_url") or e.get("url") or e.get("id")
            if url and e.get("title"):
                valid_entries.append(e)

        if not valid_entries:
            await status_msg.edit_text("Ничего не найдено.")
            return

        total_pages = (len(valid_entries) + 4) // 5

        search_pages[user_id] = {
            "query": query_text,
            "entries": valid_entries,
            "page": 0,
            "total_pages": total_pages
        }

        keyboard = build_search_keyboard(0, total_pages, query_text, valid_entries)

        await status_msg.edit_text(
            f"Результаты: {query_text} (стр. 1/{total_pages})",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        error_text = re.sub(r'\[[0-9;]*m', '', str(e))[:200]
        await status_msg.edit_text(f"Ошибка поиска: {error_text}")


def build_search_keyboard(page, total_pages, query_text, entries):
    start = page * 5
    end = min(start + 5, len(entries))
    page_entries = entries[start:end]

    keyboard = []

    for i, entry in enumerate(page_entries):
        vid_id = str(uuid.uuid4())[:8]
        search_results[vid_id] = entry.get("webpage_url") or entry.get("url") or f"https://youtu.be/{entry.get('id')}"

        full_title = entry.get("title", "Без названия")
        if len(full_title) > 40:
            short_title = full_title[:37] + "..."
        else:
            short_title = full_title

        duration = entry.get("duration") or 0
        mins, secs = divmod(int(duration), 60)

        keyboard.append([InlineKeyboardButton(
            f"{start + i + 1}. {short_title} [{mins}:{secs:02d}]",
            callback_data=f"search_result|{vid_id}"
        )])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("<< Назад", callback_data=f"page_prev|{query_text}"))

    nav_buttons.append(InlineKeyboardButton(f"Стр. {page + 1}/{total_pages}", callback_data="page_info"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперед >>", callback_data=f"page_next|{query_text}"))

    keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="search_cancel")])

    return keyboard


# --- Admin commands: /stats, /help ---

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(add_chat, update.effective_chat.id)

    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    if not os.path.exists(STATS_FILE):
        await update.message.reply_text("Статистика пока пуста.")
        return

    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        await update.message.reply_text("Ошибка чтения статистики.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    total = stats.get("total_downloads", 0)
    today_dl = stats.get("daily", {}).get(today, 0)
    users_count = len(stats.get("users", {}))
    formats = stats.get("formats", {})

    msg = (
        f"Статистика бота:\n"
        f"Всего скачиваний: {total}\n"
        f"Сегодня: {today_dl}\n"
        f"Пользователей: {users_count}\n\n"
        f"По форматам:\n"
    )
    for fmt, count in sorted(formats.items(), key=lambda x: -x[1]):
        msg += f"  {fmt}: {count}\n"

    await update.message.reply_text(msg)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(add_chat, update.effective_chat.id)
    await update.message.reply_text(
        "/search запрос - поиск видео на YouTube (до 15 результатов)\n"
        "/help - справка\n\n"
        "Отправь ссылку и выбери формат (MP3, WAV, оба).\n"
        "Можно выбрать битрейт MP3: 128, 192 или 320 kbps.\n"
        "Файлы сохраняются в папки по датам."
    )


# --- /clear: bulk-delete text messages, skipping media and links ---

async def _safe_forward_and_probe(context, chat_id, message_id):
    """Forward a message to itself to read its content (the Bot API has
    no other way to fetch an arbitrary message by id), then delete the
    forward. Retries once on Telegram flood control (RetryAfter)."""
    for attempt in range(2):
        try:
            forwarded = await context.bot.forward_message(
                chat_id=chat_id,
                from_chat_id=chat_id,
                message_id=message_id,
                disable_notification=True
            )
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=forwarded.message_id)
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=forwarded.message_id)
                except Exception:
                    pass
            return forwarded
        except RetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(e.retry_after)
                continue
            raise
    return None


async def _safe_delete(context, chat_id, message_id):
    """Delete a message, retrying once on Telegram flood control."""
    for attempt in range(2):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True, None
        except RetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(e.retry_after)
                continue
            return False, "flood"
        except TelegramError as e:
            return False, str(e)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет текстовые сообщения, пропуская ссылки и медиа (только для админов)"""
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Ответь этой командой на сообщение, с которого начать удаление.\n"
            "Пример: ответь на сообщение и напиши /clear 50"
        )
        return

    count = 50
    if context.args:
        try:
            count = int(context.args[0])
            if count < 1:
                count = 1
            if count > 200:
                count = 200
        except Exception:
            pass

    chat_id = update.effective_chat.id
    start_msg_id = update.message.reply_to_message.message_id

    status_msg = await update.message.reply_text(
        f"Сканирую {count} сообщений, начиная с #{start_msg_id} (включительно)..."
    )

    deleted = 0
    failed = 0
    skipped_links = 0
    skipped_media = 0

    current_id = start_msg_id
    checked = 0

    # Сначала удаляем команду /clear
    try:
        await update.message.delete()
    except Exception:
        pass

    while checked < count:
        forwarded = None
        try:
            forwarded = await _safe_forward_and_probe(context, chat_id, current_id)
        except Exception:
            forwarded = None

        if forwarded is not None:
            has_media = bool(
                forwarded.audio or forwarded.voice or forwarded.video or
                forwarded.document or forwarded.photo or forwarded.video_note
            )
            has_links = False
            if forwarded.text or forwarded.caption:
                text = forwarded.text or forwarded.caption or ""
                if ("http://" in text or "https://" in text or
                    "youtube.com" in text or "youtu.be" in text or
                    "t.me/" in text or "telegram.org" in text):
                    has_links = True

            if has_media:
                skipped_media += 1
                checked += 1
                current_id -= 1
                continue

            if has_links:
                skipped_links += 1
                checked += 1
                current_id -= 1
                continue

        ok, err = await _safe_delete(context, chat_id, current_id)
        if ok:
            deleted += 1
        elif err:
            low = err.lower()
            if "too old" in low:
                break
            failed += 1

        checked += 1
        current_id -= 1

        if checked % 10 == 0:
            await asyncio.sleep(0.5)

    await status_msg.edit_text(
        f"Очистка завершена.\n"
        f"Удалено: {deleted}\n"
        f"Пропущено ссылок: {skipped_links}\n"
        f"Пропущено медиа: {skipped_media}\n"
        f"Ошибок: {failed}"
    )
