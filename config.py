import os
from dotenv import load_dotenv

load_dotenv()

# === Секреты и личные настройки — задаются в .env (см. .env.example) ===
# .env в репозиторий не попадает (см. .gitignore), поэтому реальный
# токен и id туда не утекут.

# Токен бота, выданный @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Telegram id администраторов бота (через запятую в .env), например:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# Прокси для Telegram API, например socks5://127.0.0.1:10808. Пусто — не используется.
TG_PROXY = os.getenv("TG_PROXY", "")

# Прокси для YouTube (обычно не нужен, если обход блокировок настроен на уровне системы)
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "")

# === Остальные настройки — общие, их можно смело коммитить ===

# Папка для сохранения файлов
SAVE_DIR = "downloads"

# Файл с куками YouTube (netscape-формат, экспортируется расширением
# браузера типа "Get cookies.txt LOCALLY"). Нужен для скачивания
# приватных/возрастных видео. Опционален — без него бот тоже работает.
COOKIES_FILE = "cookies.txt"

# Регулярка для ссылок YouTube
YOUTUBE_REGEX = r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-]+"

# Регулярка для ссылок TikTok
TIKTOK_REGEX = r"https?://(?:www\.)?(?:tiktok\.com/@[\w.\-]+/video/\d+|vt\.tiktok\.com/[\w]+|vm\.tiktok\.com/[\w]+)"

# Максимальная длительность видео в минутах (0 = без ограничений)
MAX_DURATION_MINUTES = 5

# Папка для логов
LOG_DIR = "logs"

# Имя файла лога
LOG_FILE = "bot.log"

# Ротация логов: 5 МБ на файл, хранить 3 старых
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Доступные битрейты MP3 и значение по умолчанию
MP3_BITRATES = [128, 192, 320]
DEFAULT_MP3_BITRATE = 192

# Файл со статистикой
STATS_FILE = "stats.json"

# Файл с куками TikTok (опционально, для приватных/возрастных видео)
TIKTOK_COOKIES_FILE = "tiktok_cookies.txt"
