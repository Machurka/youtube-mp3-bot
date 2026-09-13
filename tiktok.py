import os
import re
import shutil
import logging
import asyncio
import tempfile
import yt_dlp
import httpx

logger = logging.getLogger(__name__)


# --- Main entry point: try each method in turn, one retry per method ---
async def download_tiktok(chat_id, url, context, youtube_proxy="", cookies_file=""):
    """Downloads a TikTok video, trying multiple independent methods with retries."""
    status_msg = await context.bot.send_message(chat_id=chat_id, text="Скачиваю видео с TikTok...")
    logger.info(f"TikTok запрос: {url}")

    methods = [
        ("мобильный yt-dlp", _try_ytdlp_mobile),
        ("прямой парсинг", _try_direct),
        ("внешнее API", _try_external_api),
    ]

    for method_name, method in methods:
        for attempt in range(2):
            try:
                logger.info(f"TikTok: пробую {method_name}, попытка {attempt+1}")
                result = await method(chat_id, url, context, status_msg, youtube_proxy, cookies_file)
                if result:
                    logger.info(f"TikTok: успех через {method_name}")
                    return
            except Exception as e:
                logger.warning(f"TikTok {method_name} попытка {attempt+1}: {e}")
            if attempt == 0:
                await asyncio.sleep(1)

    logger.error(f"TikTok: все методы провалились для {url}")
    await status_msg.edit_text("Не удалось скачать видео. TikTok заблокировал запрос.")


# --- Method 1: yt-dlp with mobile (iOS) headers ---
def _ytdlp_mobile_sync(url, youtube_proxy, cookies_file, work_dir):
    """Blocking part — runs in a worker thread so it doesn't block the bot's event loop."""
    opts = {
        "format": "mp4/best",
        "outtmpl": os.path.join(work_dir, "%(title).100s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 1,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if youtube_proxy:
        opts["proxy"] = youtube_proxy
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            return None, None
        filename = ydl.prepare_filename(info)

    if not os.path.exists(filename):
        base = os.path.splitext(filename)[0]
        for ext in [".mp4", ".webm", ".mkv"]:
            if os.path.exists(base + ext):
                filename = base + ext
                break

    if not os.path.exists(filename):
        return None, info

    return filename, info


async def _try_ytdlp_mobile(chat_id, url, context, status_msg, youtube_proxy="", cookies_file=""):
    """yt-dlp with an iOS User-Agent."""
    # Separate directory per attempt — avoids collisions between concurrent
    # TikTok downloads that happen to produce similar filenames.
    work_dir = tempfile.mkdtemp(prefix="tiktok_job_")
    try:
        try:
            filename, info = await asyncio.to_thread(
                _ytdlp_mobile_sync, url, youtube_proxy, cookies_file, work_dir
            )
        except Exception as e:
            logger.warning(f"TikTok yt-dlp: {e}")
            return False

        if info is None:
            logger.warning("TikTok yt-dlp: info is None")
            return False
        if filename is None:
            logger.warning("TikTok yt-dlp: файл не найден после скачивания")
            return False

        file_size = os.path.getsize(filename)
        if file_size > 50 * 1024 * 1024:
            await status_msg.edit_text("Видео слишком большое (>50 МБ).")
            return True

        await _send_video(chat_id, context, filename, info.get("title", ""))
        await status_msg.delete()
        return True
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --- Method 2: parse the TikTok page HTML directly (no yt-dlp) ---
async def _try_direct(chat_id, url, context, status_msg, youtube_proxy="", cookies_file=""):
    """Direct HTML parsing."""
    try:
        proxy = youtube_proxy if youtube_proxy else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        async with httpx.AsyncClient(proxy=proxy, follow_redirects=True, timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"TikTok прямой: статус {resp.status_code}")
                return False

            html = resp.text

            # Look for a direct video URL (try a few known page-data patterns)
            video_url = None
            for pattern in [
                r'"playAddr":"(https:[^"]+)"',
                r'"downloadAddr":"(https:[^"]+)"',
                r'"play_addr":\{"url_list":\["(https:[^"]+)"\]',
                r'"bit_rate":\{[^}]+"play_addr":\{"url_list":\["(https:[^"]+)"\]',
            ]:
                match = re.search(pattern, html)
                if match:
                    video_url = match.group(1).replace("\\u002F", "/")
                    break

            if not video_url:
                logger.warning("TikTok прямой: ссылка на видео не найдена в HTML")
                return False

            logger.info(f"TikTok прямой: нашли видео {video_url[:80]}...")
            video_resp = await client.get(video_url, headers={
                "User-Agent": headers["User-Agent"],
                "Referer": "https://www.tiktok.com/",
            })

            if video_resp.status_code != 200 or len(video_resp.content) < 1000:
                logger.warning(f"TikTok прямой: не удалось скачать видео, статус {video_resp.status_code}, размер {len(video_resp.content)}")
                return False

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
                f.write(video_resp.content)
                tmp_path = f.name

            file_size = os.path.getsize(tmp_path)
            if file_size > 50 * 1024 * 1024:
                await status_msg.edit_text("Видео слишком большое (>50 МБ).")
                os.remove(tmp_path)
                return True

            await _send_video(chat_id, context, tmp_path, "Скачано с TikTok")
            await status_msg.delete()
            return True

    except Exception as e:
        logger.warning(f"TikTok прямой: {e}")
        return False


# --- Method 3: backup external API (tikwm.com) ---
async def _try_external_api(chat_id, url, context, status_msg, youtube_proxy="", cookies_file=""):
    """Backup method via tikwm.com."""
    try:
        proxy = youtube_proxy if youtube_proxy else None

        async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
            api_url = "https://www.tikwm.com/api/"
            resp = await client.post(api_url, data={"url": url}, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            })

            if resp.status_code != 200:
                logger.warning(f"TikTok API: статус {resp.status_code}")
                return False

            data = resp.json()
            if data.get("code") != 0:
                logger.warning(f"TikTok API: код ошибки {data.get('code')}: {data.get('msg', '')}")
                return False

            video_url = data.get("data", {}).get("play") or data.get("data", {}).get("hdplay")
            if not video_url:
                logger.warning("TikTok API: нет ссылки на видео в ответе")
                return False

            logger.info(f"TikTok API: скачиваю {video_url[:80]}...")
            video_resp = await client.get(video_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tikwm.com/",
            })

            if video_resp.status_code != 200 or len(video_resp.content) < 1000:
                logger.warning(f"TikTok API: не удалось скачать, статус {video_resp.status_code}, размер {len(video_resp.content)}")
                return False

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
                f.write(video_resp.content)
                tmp_path = f.name

            file_size = os.path.getsize(tmp_path)
            if file_size > 50 * 1024 * 1024:
                await status_msg.edit_text("Видео слишком большое (>50 МБ).")
                os.remove(tmp_path)
                return True

            title = data.get("data", {}).get("title", "Скачано с TikTok")
            await _send_video(chat_id, context, tmp_path, title)
            await status_msg.delete()
            return True

    except Exception as e:
        logger.warning(f"TikTok API: {e}")
        return False


# --- Shared helper: send the file, then always clean it up ---
async def _send_video(chat_id, context, filepath, title):
    """Sends the video, then removes the local file."""
    try:
        with open(filepath, "rb") as video:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video,
                caption=title[:200] if title else "Скачано с TikTok",
                supports_streaming=True
            )
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)