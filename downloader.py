import os
import re
import time
import shutil
import json
import asyncio
import tempfile
import threading
import yt_dlp
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# --- Retry settings for transient YouTube download failures (HTTP 403) ---
_RETRYABLE_ERROR_MARKERS = ("403", "forbidden")
_DOWNLOAD_RETRY_ATTEMPTS = 3
_DOWNLOAD_RETRY_DELAY_SECONDS = 3


def _is_retryable_download_error(err_text: str) -> bool:
    low = err_text.lower()
    return any(marker in low for marker in _RETRYABLE_ERROR_MARKERS)


def _extract_with_retries(ydl, url, what: str):
    """Run extract_info(download=True), retrying on transient YouTube
    errors (403/Forbidden). Other errors are raised immediately."""
    last_err = None
    for attempt in range(1, _DOWNLOAD_RETRY_ATTEMPTS + 1):
        try:
            return ydl.extract_info(url, download=True)
        except Exception as e:
            last_err = e
            if attempt < _DOWNLOAD_RETRY_ATTEMPTS and _is_retryable_download_error(str(e)):
                logger.warning(
                    f"{what}: looks like a transient YouTube failure, "
                    f"attempt {attempt}/{_DOWNLOAD_RETRY_ATTEMPTS}, waiting {_DOWNLOAD_RETRY_DELAY_SECONDS}s..."
                )
                time.sleep(_DOWNLOAD_RETRY_DELAY_SECONDS)
                continue
            raise
    raise last_err


# Guards stats.json against concurrent read-modify-write from multiple
# asyncio.to_thread worker threads.
_stats_lock = threading.Lock()


# --- yt-dlp option builders ---

def get_ydl_opts(extra_opts=None, youtube_proxy="", cookies_file=""):
    """Base yt-dlp options shared by info/search lookups."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
    }
    if youtube_proxy:
        opts["proxy"] = youtube_proxy
    if os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file
    if extra_opts:
        opts.update(extra_opts)
    return opts


def clean_filename(name):
    """Strip characters that are illegal in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


# --- Video duration check ---

def _check_duration_sync(url, youtube_proxy="", cookies_file="", max_minutes=30):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 30,
        "extract_flat": True,
    }
    if youtube_proxy:
        opts["proxy"] = youtube_proxy
    if os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            return False, "Не удалось получить информацию о видео."

        duration = info.get("duration")
        if duration is None:
            return False, "Не удалось определить длительность видео."

        minutes = duration / 60
        if minutes > max_minutes:
            mins = int(minutes)
            secs = int(duration % 60)
            return False, f"Видео слишком длинное: {mins}:{secs:02d} (максимум {max_minutes} мин)."

        return True, None

    except Exception as e:
        logger.warning(f"Ошибка проверки длительности: {e}")
        return False, f"Не удалось проверить видео: {str(e)[:100]}"


async def check_duration(url, youtube_proxy="", cookies_file="", max_minutes=30):
    """Async wrapper: runs the blocking yt-dlp call in a worker thread
    so it doesn't stall the bot for other users."""
    return await asyncio.to_thread(
        _check_duration_sync, url, youtube_proxy, cookies_file, max_minutes
    )


# --- Stats persistence ---

def _atomic_write_json(path, data):
    """Write to a temp file then rename over the target, so a crash
    mid-write never leaves a corrupted JSON file."""
    dir_name = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _update_stats_sync(user_id, username, format_type, bitrate=None):
    from config import STATS_FILE

    with _stats_lock:
        stats = {}
        try:
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    stats = json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось прочитать статистику, начинаю заново: {e}")

        today = datetime.now().strftime("%Y-%m-%d")
        stats["total_downloads"] = stats.get("total_downloads", 0) + 1

        if "daily" not in stats:
            stats["daily"] = {}
        stats["daily"][today] = stats["daily"].get(today, 0) + 1

        if "users" not in stats:
            stats["users"] = {}
        uid = str(user_id)
        if uid not in stats["users"]:
            stats["users"][uid] = {"username": username or "unknown", "first_seen": today, "downloads": 0}
        stats["users"][uid]["username"] = username or stats["users"][uid]["username"]
        stats["users"][uid]["downloads"] += 1

        if "formats" not in stats:
            stats["formats"] = {}
        key = format_type
        if bitrate and format_type == "mp3":
            key = f"mp3_{bitrate}"
        stats["formats"][key] = stats["formats"].get(key, 0) + 1

        try:
            _atomic_write_json(STATS_FILE, stats)
        except Exception as e:
            logger.warning(f"Не удалось сохранить статистику: {e}")


async def update_stats(user_id, username, format_type, bitrate=None):
    """Record one completed download in stats.json (off the event loop)."""
    await asyncio.to_thread(_update_stats_sync, user_id, username, format_type, bitrate)


# --- Backup download path (used only if yt-dlp fails completely) ---

async def download_via_api(chat_id, url, format_type, context, mp3_bitrate=192):
    """Fall back to a third-party YouTube-to-MP3 API. Unofficial and
    undocumented, so treated as best-effort only."""
    import aiohttp

    try:
        video_id = None
        for pattern in [r"watch\?v=([\w\-]+)", r"youtu\.be/([\w\-]+)", r"shorts/([\w\-]+)"]:
            match = re.search(pattern, url)
            if match:
                video_id = match.group(1)
                break

        if not video_id:
            raise Exception("Не удалось извлечь ID видео")

        from urllib.parse import quote

        api_url = f"https://api.vevioz.com/api/single/mp3?url={quote(url, safe='')}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=60, headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise Exception(f"API ответил {resp.status}: {body}")
                data = await resp.json()
                # Different API versions use different field names for
                # the download link — try the known variants.
                download_url = (
                    data.get("link") or data.get("url") or data.get("dlink")
                    or data.get("download") or data.get("mp3")
                )
                if not download_url:
                    raise Exception(f"API не вернул ссылку, ответ: {str(data)[:200]}")

                async with session.get(download_url, timeout=120) as dl:
                    if dl.status != 200:
                        raise Exception(f"Не удалось скачать с API: {dl.status}")
                    content = await dl.read()
                    if len(content) < 1000:
                        raise Exception("Скачанный файл слишком маленький")

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                        f.write(content)
                        tmp_path = f.name

                    try:
                        with open(tmp_path, "rb") as audio:
                            await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=audio,
                                title="YouTube Audio (MP3)",
                                performer="YouTube",
                            )
                    finally:
                        os.remove(tmp_path)
                    return True

    except Exception as e:
        logger.error(f"API метод не сработал: {e}")
        raise


# --- Main download pipeline ---

def _resolve_output_path(work_dir, predicted_path, ext):
    """Fallback lookup if the predicted filename doesn't match what
    yt-dlp/ffmpeg actually wrote. work_dir is exclusive to one job, so
    a single file with the right extension is unambiguously the output."""
    if predicted_path and os.path.exists(predicted_path):
        return predicted_path
    if not os.path.isdir(work_dir):
        return predicted_path
    candidates = [
        os.path.join(work_dir, name)
        for name in os.listdir(work_dir)
        if name.lower().endswith(ext)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return predicted_path


def _download_job_sync(url, formats_to_download, mp3_bitrate, youtube_proxy, cookies_file, work_dir):
    """All the blocking yt-dlp work for one download job (metadata +
    MP3/WAV extraction, with a video-format fallback for each). Runs in
    a worker thread via asyncio.to_thread. Returns (info, mp3_path, wav_path)."""

    info = None
    mp3_path = None
    wav_path = None

    info_opts = get_ydl_opts({}, youtube_proxy, cookies_file)
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning(f"Не удалось получить инфо: {e}, пробую сразу скачать...")

    if "mp3" in formats_to_download:
        mp3_opts = get_ydl_opts({
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(mp3_bitrate),
            }],
            "outtmpl": os.path.join(work_dir, "%(title).100s_mp3.%(ext)s"),
            # A real download failure must raise so the fallback chain
            # below can kick in — ignoreerrors=True would swallow it.
            "ignoreerrors": False,
        }, youtube_proxy, cookies_file)
        try:
            with yt_dlp.YoutubeDL(mp3_opts) as ydl:
                # Always build the filename from THIS extraction's info,
                # not an earlier metadata-only one — titles can differ
                # slightly between requests.
                mp3_info = _extract_with_retries(ydl, url, "MP3")
                if info is None:
                    info = mp3_info
                filename = ydl.prepare_filename(mp3_info)
                mp3_path = filename.rsplit(".", 1)[0] + ".mp3"
        except Exception as e:
            err = str(e)
            if "Requested format is not available" in err:
                logger.warning("MP3: пробую через видео")
                mp3_opts["format"] = "best[height<=720]/best"
                try:
                    with yt_dlp.YoutubeDL(mp3_opts) as ydl:
                        fallback_info = _extract_with_retries(ydl, url, "MP3 (видео-фолбэк)")
                        if info is None:
                            info = fallback_info
                        base = os.path.splitext(ydl.prepare_filename(fallback_info))[0]
                        mp3_path = base + ".mp3"
                except Exception as e2:
                    logger.error(f"MP3 видео тоже не удалось: {e2}")
            elif _is_retryable_download_error(err):
                logger.error(f"MP3 не удалось после {_DOWNLOAD_RETRY_ATTEMPTS} попыток (403): {e}")
            else:
                logger.error(f"MP3 не удалось: {e}")

    if "wav" in formats_to_download:
        wav_opts = get_ydl_opts({
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
            }],
            "outtmpl": os.path.join(work_dir, "%(title).100s_wav.%(ext)s"),
            "ignoreerrors": False,
        }, youtube_proxy, cookies_file)
        try:
            with yt_dlp.YoutubeDL(wav_opts) as ydl:
                wav_info = _extract_with_retries(ydl, url, "WAV")
                if info is None:
                    info = wav_info
                filename = ydl.prepare_filename(wav_info)
                wav_path = filename.rsplit(".", 1)[0] + ".wav"
        except Exception as e:
            err = str(e)
            if "Requested format is not available" in err:
                logger.warning("WAV: пробую через видео")
                wav_opts["format"] = "best[height<=720]/best"
                try:
                    with yt_dlp.YoutubeDL(wav_opts) as ydl:
                        wav_info = _extract_with_retries(ydl, url, "WAV (видео-фолбэк)")
                        if info is None:
                            info = wav_info
                        base = os.path.splitext(ydl.prepare_filename(wav_info))[0]
                        wav_path = base + ".wav"
                except Exception as e2:
                    logger.error(f"WAV видео тоже не удалось: {e2}")
            elif _is_retryable_download_error(err):
                logger.error(f"WAV не удалось после {_DOWNLOAD_RETRY_ATTEMPTS} попыток (403): {e}")
            else:
                logger.error(f"WAV не удалось: {e}")

    if mp3_path:
        mp3_path = _resolve_output_path(work_dir, mp3_path, ".mp3")
    if wav_path:
        wav_path = _resolve_output_path(work_dir, wav_path, ".wav")

    return info, mp3_path, wav_path


async def download_and_send(chat_id, url, format_type, context,
                            youtube_proxy="", cookies_file="", save_dir="downloads",
                            status_message=None, mp3_bitrate=192):
    """Download the requested format(s), save them under save_dir/<date>/,
    send them to the chat, and record stats. Falls back to the external
    API (download_via_api) if yt-dlp fails entirely."""
    formats_to_download = []
    if format_type in ("mp3", "both"):
        formats_to_download.append("mp3")
    if format_type in ("wav", "both"):
        formats_to_download.append("wav")

    if status_message:
        try:
            await status_message.edit_text("Скачиваю аудио...")
        except Exception:
            pass

    # Unique per-job directory, so two simultaneous downloads (even of
    # the same video) never collide on temp filenames.
    work_dir = tempfile.mkdtemp(prefix="ytdlp_job_")

    info = None
    mp3_path = None
    wav_path = None
    try:
        try:
            info, mp3_path, wav_path = await asyncio.to_thread(
                _download_job_sync, url, formats_to_download, mp3_bitrate,
                youtube_proxy, cookies_file, work_dir
            )
        except Exception as e:
            logger.error(f"Ошибка в фоновом потоке скачивания: {e}")

        # Last-resort fallback if yt-dlp produced nothing at all.
        if not mp3_path and not wav_path and format_type in ("mp3", "both"):
            try:
                logger.info("Пробую резервный API...")
                await download_via_api(chat_id, url, format_type, context, mp3_bitrate)
                if status_message:
                    try:
                        await status_message.delete()
                    except Exception:
                        pass
                return
            except Exception as api_err:
                logger.error(f"Резервный API тоже не сработал: {api_err}")

        if not mp3_path and not wav_path:
            raise Exception("Не удалось скачать ни одним методом.")
        if not info:
            raise Exception("Не удалось получить информацию о видео")

        date_str = datetime.now().strftime("%Y-%m-%d")
        save_folder = os.path.join(save_dir, date_str)
        os.makedirs(save_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%H-%M-%S")
        safe_title = clean_filename(info.get("title", "audio"))[:80]

        saved_mp3 = None
        saved_wav = None
        if mp3_path and os.path.exists(mp3_path):
            saved_mp3 = os.path.join(save_folder, f"{safe_title} - {timestamp}.mp3")
            shutil.copy2(mp3_path, saved_mp3)
        if wav_path and os.path.exists(wav_path):
            saved_wav = os.path.join(save_folder, f"{safe_title} - {timestamp}.wav")
            shutil.copy2(wav_path, saved_wav)

        if saved_mp3 and os.path.exists(saved_mp3):
            with open(saved_mp3, "rb") as audio:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio,
                    title=f"{info.get('title', 'YouTube Audio')} (MP3)",
                    performer=info.get("uploader", "Unknown"),
                    duration=info.get("duration"),
                )

        if saved_wav and os.path.exists(saved_wav):
            wav_size = os.path.getsize(saved_wav)
            if wav_size > 45 * 1024 * 1024:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"WAV слишком большой ({wav_size // (1024*1024)} МБ), отправляю только MP3."
                )
            else:
                with open(saved_wav, "rb") as audio:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio,
                        title=f"{info.get('title', 'YouTube Audio')} (WAV)",
                        performer=info.get("uploader", "Unknown"),
                        duration=info.get("duration"),
                    )

        if not saved_mp3 and not saved_wav:
            raise Exception("Файлы не были найдены после скачивания.")

        try:
            user = context._user
            await update_stats(
                user.id, user.username, format_type,
                mp3_bitrate if format_type in ("mp3", "both") else None
            )
        except Exception:
            pass

        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Ошибка скачивания {url}: {e}")
        error_text = re.sub(r'\[[0-9;]*m', '', str(e))[:200]
        error_text = f"Не удалось скачать: {error_text}"
        if status_message:
            try:
                await status_message.edit_text(error_text)
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=error_text)
        else:
            await context.bot.send_message(chat_id=chat_id, text=error_text)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
