import asyncio
import html
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN or TOKEN.startswith("ВСТАВЬ"):
    raise RuntimeError("Добавь новый токен BotFather в .env: BOT_TOKEN=...")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MAX_BYTES = 49 * 1024 * 1024
SEARCH_LIMIT = 8
MAX_CONCURRENT_SEARCHES = max(1, int(os.getenv("MAX_CONCURRENT_SEARCHES", "2")))
MAX_DOWNLOADS_PER_MINUTE = max(1, int(os.getenv("MAX_DOWNLOADS_PER_MINUTE", "3")))
TEMP_MAX_AGE = 6 * 60 * 60
CLEANUP_INTERVAL = 30 * 60
SEARCH_CACHE_TTL = 10 * 60
SEARCH_CACHE_MAX = 500
RESULT_TTL = 30 * 60
DB_PATH = BASE_DIR / "bot_stats.sqlite3"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = BASE_DIR / "logs" / "music_cartelbot.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_root_logger = logging.getLogger()
_root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_log_format = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_log_format)
_root_logger.addHandler(_console)
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(_log_format)
_root_logger.addHandler(_file_handler)
log = logging.getLogger("music_cartelbot")

class TelegramErrorHandler(logging.Handler):
    """Send ERROR+ log records to admins without buffering logs in memory."""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.bot = None
        self.loop = None
        self.last_sent = {}
        self.cooldown_seconds = 60

    def bind(self, bot, loop):
        self.bot = bot
        self.loop = loop

    def emit(self, record):
        # Never log from this handler itself: that could cause a notification loop.
        try:
            if not self.bot or not self.loop or not self.loop.is_running() or not ADMIN_IDS:
                return
            message_text = record.getMessage()
            exception_text = ""
            if record.exc_info and record.exc_info[1]:
                exc = record.exc_info[1]
                exception_text = f"\\nПричина: {type(exc).__name__}: {exc}"
            # Limit message length and coalesce repeated errors to avoid spam.
            key = f"{record.name}:{record.levelname}:{message_text[:180]}:{exception_text[:120]}"
            now = time.monotonic()
            previous = self.last_sent.get(key, 0)
            if now - previous < self.cooldown_seconds:
                return
            self.last_sent[key] = now
            if len(self.last_sent) > 200:
                cutoff = now - self.cooldown_seconds
                self.last_sent = {k: t for k, t in self.last_sent.items() if t >= cutoff}
                if len(self.last_sent) > 200:
                    self.last_sent.clear()
                    self.last_sent[key] = now
            details = (
                "🚨 <b>MUSIC_CARTELBOT — ошибка</b>\\n"
                f"Уровень: <code>{html.escape(record.levelname)}</code>\\n"
                f"Модуль: <code>{html.escape(record.name[:100])}</code>\\n"
                f"Сообщение: {html.escape(message_text[:1200])}"
                f"{html.escape(exception_text[:1000])}"
            )
            asyncio.run_coroutine_threadsafe(self._send_to_admins(details), self.loop)
        except Exception:
            pass

    async def _send_to_admins(self, message_text):
        for admin_id in ADMIN_IDS:
            try:
                await self.bot.send_message(admin_id, message_text)
            except Exception:
                # Do not log notification failures; avoid recursive alerts.
                pass

_telegram_error_handler = TelegramErrorHandler()
_root_logger.addHandler(_telegram_error_handler)

dp = Dispatcher()
WORK = BASE_DIR / "downloads"
WORK.mkdir(parents=True, exist_ok=True)
search_slots = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
SEARCH_CACHE = {}
RESULTS = {}
CARD_QUERY = {}
USER_DOWNLOAD_TIMES = defaultdict(deque)
USER_ACTIVE = set()
ACTIVE_TASK_DIRS = set()
SEARCH_CACHE_LOCK = asyncio.Lock()
LAST_ALERTS = {}
DISK_WARN_BYTES = 2 * 1024**3
DISK_CRITICAL_BYTES = 1 * 1024**3

VARIANTS = [
    ("🎧 Оригинал", ""), ("🔥 Remix", " remix"), ("📱 TikTok Remix", " tiktok remix"),
    ("⚡ Sped Up", " sped up"), ("🌙 Slowed", " slowed"),
    ("🌌 Slowed + Reverb", " slowed reverb"), ("🎶 Nightcore", " nightcore"),
    ("🎤 Live", " live"), ("🎹 Instrumental", " instrumental"),
]

def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            searches INTEGER NOT NULL DEFAULT 0, downloads INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            kind TEXT NOT NULL, created_at REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")

def record_user(user, kind=None):
    if not user:
        return
    now = time.time()
    with db() as con:
        con.execute("""INSERT INTO users(user_id,username,first_name,first_seen,last_seen)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen""",
            (user.id, user.username, user.first_name, now, now))
        if kind:
            field = {"search": "searches", "download": "downloads", "success": "successes", "failure": "failures"}.get(kind)
            if field:
                con.execute(f"UPDATE users SET {field}={field}+1 WHERE user_id=?", (user.id,))
            con.execute("INSERT INTO events(user_id,kind,created_at) VALUES(?,?,?)", (user.id, kind, now))

def is_admin(user_id):
    return user_id in ADMIN_IDS

def cleanup_old_files(active_paths=None):
    """Remove stale task files but never touch directories owned by live tasks."""
    now = time.time()
    active_paths = {Path(x).resolve() for x in (active_paths or set())}
    try:
        for p in WORK.iterdir():
            try:
                resolved = p.resolve()
                if resolved in active_paths:
                    continue
                if now - p.stat().st_mtime > TEMP_MAX_AGE:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not clean stale path: %s", p, exc_info=True)
    except OSError:
        log.exception("Temporary directory cleanup failed")

def get_memory_metrics():
    """Return (used_bytes, total_bytes, swap_used_bytes, swap_total_bytes), or None."""
    try:
        values = {}
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    values[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        swap_total = values.get("SwapTotal", 0)
        swap_free = values.get("SwapFree", 0)
        return total - available, total, swap_total - swap_free, swap_total
    except (OSError, ValueError, KeyError):
        return None


async def notify_admins(bot: Bot, key: str, message: str, cooldown: int = 1800):
    now = time.time()
    if now - LAST_ALERTS.get(key, 0) < cooldown:
        return
    LAST_ALERTS[key] = now
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, message)
        except Exception:
            log.warning("Could not send health alert to admin_id=%s", admin_id, exc_info=True)


async def health_monitor_loop(bot: Bot):
    while True:
        await asyncio.sleep(300)
        try:
            usage = shutil.disk_usage(BASE_DIR)
            if usage.free < DISK_CRITICAL_BYTES:
                await notify_admins(bot, "disk_critical",
                    f"🚨 MUSIC_CARTELBOT: критически мало места на диске — {usage.free/1024**3:.2f} ГБ свободно. Проверь downloads и логи.", 1800)
            elif usage.free < DISK_WARN_BYTES:
                await notify_admins(bot, "disk_warning",
                    f"⚠️ MUSIC_CARTELBOT: осталось {usage.free/1024**3:.2f} ГБ на диске. Рекомендуется проверить временные файлы.", 21600)
            memory = get_memory_metrics()
            if memory and memory[1] and memory[0] / memory[1] >= 0.85:
                await notify_admins(bot, "ram_warning",
                    f"⚠️ MUSIC_CARTELBOT: используется {memory[0]/memory[1]*100:.0f}% RAM. Активных загрузок: {len(USER_ACTIVE)}.", 1800)
        except Exception:
            log.exception("Health monitoring failed")


async def cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        await asyncio.to_thread(cleanup_old_files, set(ACTIVE_TASK_DIRS))
        prune_memory_caches()

def prune_memory_caches():
    now = time.time()
    for key, entry in list(SEARCH_CACHE.items()):
        if now - entry["time"] > SEARCH_CACHE_TTL:
            SEARCH_CACHE.pop(key, None)
    if len(SEARCH_CACHE) > SEARCH_CACHE_MAX:
        oldest = sorted(SEARCH_CACHE, key=lambda k: SEARCH_CACHE[k]["time"])
        for key in oldest[:len(SEARCH_CACHE) - SEARCH_CACHE_MAX]:
            SEARCH_CACHE.pop(key, None)
    for key, entry in list(RESULTS.items()):
        if now - entry["time"] > RESULT_TTL:
            RESULTS.pop(key, None)
    if len(RESULTS) > 1000:
        oldest = sorted(RESULTS, key=lambda k: RESULTS[k]["time"])
        for key in oldest[:len(RESULTS) - 1000]:
            RESULTS.pop(key, None)
    if len(CARD_QUERY) > 1000:
        for key in list(CARD_QUERY)[:len(CARD_QUERY) - 800]:
            CARD_QUERY.pop(key, None)

def search_youtube_uncached(query: str, limit: int = SEARCH_LIMIT):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "socket_timeout": 12, "retries": 1,
            "extractor_retries": 1, "cachedir": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    found, seen = [], set()
    for item in (data or {}).get("entries") or []:
        if not item:
            continue
        vid = item.get("id")
        url = item.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
        if not url or url in seen:
            continue
        seen.add(url)
        found.append({"title": item.get("title") or "Без названия",
                      "channel": item.get("channel") or item.get("uploader") or "Автор не указан",
                      "url": url, "thumbnail": item.get("thumbnail"), "duration": item.get("duration")})
    return found

async def search_youtube(query: str):
    now = time.time()
    key = query.casefold()
    async with SEARCH_CACHE_LOCK:
        entry = SEARCH_CACHE.get(key)
        if entry and now - entry["time"] < SEARCH_CACHE_TTL:
            return [dict(x) for x in entry["results"]]
    async with search_slots:
        # Re-check after waiting; another request may have populated the cache.
        async with SEARCH_CACHE_LOCK:
            entry = SEARCH_CACHE.get(key)
            if entry and time.time() - entry["time"] < SEARCH_CACHE_TTL:
                return [dict(x) for x in entry["results"]]
        results = await asyncio.to_thread(search_youtube_uncached, query)
    async with SEARCH_CACHE_LOCK:
        SEARCH_CACHE[key] = {"time": time.time(), "results": results}
        prune_memory_caches()
    return results

def duration(seconds):
    try:
        seconds = int(seconds)
        return f"{seconds // 60}:{seconds % 60:02d}"
    except (TypeError, ValueError):
        return "—"

def safe_filename(value):
    return re.sub(r'[\\/:*?"<>|]+', "_", value)[:100].strip(" ._") or "audio"

def build_keyboard(results):
    rows = []
    for i, item in enumerate(results):
        rid = uuid.uuid4().hex[:12]
        RESULTS[rid] = {"item": item, "time": time.time()}
        rows.append([InlineKeyboardButton(
            text=f"⬇️ {i+1}. {item['title'][:45]} · {duration(item.get('duration'))}",
            callback_data=f"dl:{rid}")])
    for start in (0, 3, 6):
        row = [InlineKeyboardButton(text=VARIANTS[idx][0], callback_data=f"ver:{idx}")
               for idx in range(start, min(start + 3, len(VARIANTS)))]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔎 Искать ещё", callback_data="more")])
    prune_memory_caches()
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_results(message: Message, query: str, results):
    if not results:
        await message.answer("😿 Ничего не нашёл.\n\nПопробуй проверить написание или указать исполнителя и название.")
        return
    item = results[0]
    caption = (f"🎵 <b>{html.escape(item['channel'])} — {html.escape(item['title'])}</b>\n\n"
               f"🔎 Запрос: <code>{html.escape(query)}</code>\n"
               f"🎶 Найдено вариантов: <b>{len(results)}</b>\n\n"
               "👇 Нажми на найденный вариант, чтобы запросить отправку аудио.\n"
               "ℹ️ Скачивай и распространяй только аудио, на которое у тебя есть права.")
    keyboard = build_keyboard(results)
    sent = None
    if item.get("thumbnail"):
        try:
            sent = await message.answer_photo(item["thumbnail"], caption=caption, reply_markup=keyboard)
        except Exception:
            log.info("Could not send thumbnail; using text card")
    if sent is None:
        sent = await message.answer(caption, reply_markup=keyboard)
    CARD_QUERY[(sent.chat.id, sent.message_id)] = query
    if len(CARD_QUERY) > 1000:
        for k in list(CARD_QUERY)[:len(CARD_QUERY)-800]:
            CARD_QUERY.pop(k, None)

@dp.message(CommandStart())
async def start(message: Message):
    record_user(message.from_user)
    welcome = ("👑 <b>Добро пожаловать в @Music_CartelBot!</b> 🎶\n\n"
        "🎧 Ищу оригиналы, ремиксы и альтернативные версии.\n\n"
        "✨ <b>Как пользоваться</b>\n1️⃣ Введи <code>автор - название</code>\n"
        "2️⃣ Выбери найденный вариант кнопкой\n3️⃣ Бот попробует отправить аудио, если источник и права позволяют\n\n"
        "🚀 <b>Введите «автор - название»</b>\n🖤 Приятного прослушивания!")
    banner_setting = os.getenv("BANNER_FILE", "assets/banner.png").strip() or "assets/banner.png"
    banner_path = Path(banner_setting)
    if not banner_path.is_absolute():
        banner_path = BASE_DIR / banner_path
    if banner_path.is_file():
        await message.answer_photo(FSInputFile(banner_path), caption=welcome)
    else:
        await message.answer(welcome)

def system_memory_line():
    memory = get_memory_metrics()
    if not memory:
        return "🧠 RAM: данные недоступны"
    used, total, swap_used, swap_total = memory
    ram = f"🧠 RAM: {used/1024**3:.2f}/{total/1024**3:.2f} ГБ занято"
    swap = f" · Swap: {swap_used/1024**3:.2f}/{swap_total/1024**3:.2f} ГБ" if swap_total else " · Swap: не настроен"
    try:
        load_text = f" · Load avg: {os.getloadavg()[0]:.2f}"
    except (AttributeError, OSError):
        load_text = ""
    return ram + swap + load_text


@dp.message(Command("stats"))
async def stats(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return
    now = time.time()
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        totals = {r["kind"]: r["n"] for r in con.execute("SELECT kind,COUNT(*) n FROM events GROUP BY kind")}
        day = {r["kind"]: r["n"] for r in con.execute("SELECT kind,COUNT(*) n FROM events WHERE created_at>=? GROUP BY kind", (now-86400,))}
        top = con.execute("SELECT user_id,username,first_name,downloads,successes,failures,last_seen FROM users ORDER BY downloads DESC,last_seen DESC LIMIT 10").fetchall()
    try:
        disk = shutil.disk_usage(BASE_DIR)
        disk_line = f"💽 Диск: свободно {disk.free/1024**3:.2f} ГБ из {disk.total/1024**3:.2f} ГБ"
    except OSError:
        disk_line = "💽 Диск: данные недоступны"
    active = len(USER_ACTIVE)
    msg = (f"📊 <b>MUSIC_CARTELBOT — статистика</b>\n\n"
           f"👥 Всего пользователей: <b>{users}</b>\n"
           f"🔎 Поисков: <b>{totals.get('search',0)}</b> (за 24 ч: {day.get('search',0)})\n"
           f"⬇️ Запросов загрузки: <b>{totals.get('download',0)}</b> (за 24 ч: {day.get('download',0)})\n"
           f"✅ Успешно: <b>{totals.get('success',0)}</b> · ❌ Ошибок: <b>{totals.get('failure',0)}</b>\n"
           f"⚙️ Активных загрузок: <b>{active}</b> (глобального лимита нет)\n{disk_line}\n{system_memory_line()}\n\n"
           f"<b>Топ пользователей по загрузкам:</b>")
    for u in top:
        uname = ("@" + u["username"]) if u["username"] else (u["first_name"] or "Без имени")
        msg += f"\n<code>{u['user_id']}</code> · {html.escape(uname[:30])}: {u['downloads']} (✅{u['successes']}/❌{u['failures']})"
    msg += "\n\nПодробнее по ID: <code>/user ID</code>"
    await message.answer(msg)

@dp.message(Command("user"))
async def user_stats(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/user TELEGRAM_ID</code>")
        return
    uid = int(parts[1])
    with db() as con:
        u = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        events = con.execute("SELECT kind,COUNT(*) n FROM events WHERE user_id=? GROUP BY kind", (uid,)).fetchall()
    if not u:
        await message.answer("Пользователь пока не найден в статистике.")
        return
    kinds = {r["kind"]: r["n"] for r in events}
    username = ("@" + u["username"]) if u["username"] else (u["first_name"] or "не указан")
    await message.answer(
        f"👤 <b>Пользователь</b>\nID: <code>{uid}</code>\nИмя: {html.escape(username)}\n"
        f"Первый запуск: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(u['first_seen']))}\n"
        f"Последняя активность: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(u['last_seen']))}\n"
        f"Поисков: {u['searches']} · Загрузок: {u['downloads']}\n"
        f"Успешно: {u['successes']} · Ошибок: {u['failures']}\n"
        f"Событий: {sum(kinds.values())}")

@dp.message(F.text)
async def search(message: Message):
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return
    if len(query) > 180:
        await message.answer("✂️ Запрос слишком длинный. Сократи его до 180 символов.")
        return
    record_user(message.from_user, "search")
    status = await message.answer("🔍 Ищу песни и версии… 🎧")
    try:
        results = await search_youtube(query)
    except Exception:
        log.exception("YouTube search failed")
        try:
            await status.edit_text("⚠️ Поиск не сработал. Попробуй позже; возможно, нужно обновить yt-dlp.")
        except Exception:
            pass
        return
    try:
        await status.delete()
    except Exception:
        pass
    await show_results(message, query, results)

@dp.callback_query(F.data.startswith("ver:"))
async def search_variant(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return
    query = CARD_QUERY.get((callback.message.chat.id, callback.message.message_id))
    if not query:
        await callback.answer("Сделай новый поиск песни.", show_alert=True)
        return
    try:
        idx = int(callback.data.split(":", 1)[1])
        suffix = VARIANTS[idx][1]
    except (ValueError, IndexError):
        await callback.answer("Неизвестный вариант.")
        return
    refined = query + suffix if suffix else query
    await callback.answer("Ищу эту версию…")
    try:
        results = await search_youtube(refined)
    except Exception:
        log.exception("Variant search failed")
        await callback.message.answer("⚠️ Не удалось найти версию. Попробуй ещё раз.")
        return
    await show_results(callback.message, refined, results)

@dp.callback_query(F.data == "more")
async def more(callback: CallbackQuery):
    await callback.answer("Отправь новый запрос в чат: автор - название", show_alert=True)

def download_audio(url: str, task_dir: Path):
    template = str(task_dir / "%(id)s_%(title).100s.%(ext)s")
    opts = {"format": "bestaudio/best", "outtmpl": template, "noplaylist": True,
        "quiet": True, "no_warnings": True, "socket_timeout": 20, "retries": 1,
        "fragment_retries": 1, "extractor_retries": 1, "max_filesize": MAX_BYTES,
        "cachedir": False, "continuedl": False, "concurrent_fragment_downloads": 1,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    candidates = list(task_dir.glob("*.mp3"))
    if not candidates:
        candidates = [p for p in task_dir.iterdir() if p.is_file() and p.suffix.lower() not in (".part", ".ytdl")]
    if not candidates:
        raise RuntimeError("Файл после загрузки не найден.")
    candidate = max(candidates, key=lambda p: p.stat().st_mtime)
    if candidate.stat().st_size > MAX_BYTES:
        raise RuntimeError("Файл превышает лимит 49 МБ.")
    title = info.get("title") or "Audio"
    artist = info.get("artist") or info.get("uploader") or "Unknown artist"
    return candidate, title, artist

@dp.callback_query(F.data.startswith("dl:"))
async def download_selected(callback: CallbackQuery, bot: Bot):
    if not callback.message:
        await callback.answer()
        return
    user = callback.from_user
    rid = callback.data.split(":", 1)[1]
    stored = RESULTS.get(rid)
    if not stored or time.time() - stored["time"] > RESULT_TTL:
        await callback.answer("Результат устарел. Выполни поиск заново.", show_alert=True)
        return
    if user.id in USER_ACTIVE:
        await callback.answer("Твоя загрузка уже выполняется. Дождись её завершения.", show_alert=True)
        return
    recent = USER_DOWNLOAD_TIMES[user.id]
    now = time.monotonic()
    while recent and now - recent[0] > 60:
        recent.popleft()
    if len(recent) >= MAX_DOWNLOADS_PER_MINUTE:
        await callback.answer("Слишком много запросов. Подожди минуту.", show_alert=True)
        return
    recent.append(now)
    USER_ACTIVE.add(user.id)
    record_user(user, "download")
    await callback.answer("Начинаю загрузку…")
    status = await callback.message.answer("⬇️ Подготавливаю аудио…")
    task_dir = WORK / uuid.uuid4().hex
    ACTIVE_TASK_DIRS.add(str(task_dir.resolve()))
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        await status.edit_text("⬇️ Загружаю аудио. Это может занять некоторое время…")
        path, title, artist = await asyncio.to_thread(download_audio, stored["item"]["url"], task_dir)
        await bot.send_audio(chat_id=callback.message.chat.id, audio=FSInputFile(path),
            title=title[:64], performer=artist[:64],
            caption=f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>\n\n👑 @Music_CartelBot")
        record_user(user, "success")
        try:
            await status.edit_text("✅ Готово! Аудио отправлено.")
        except Exception:
            log.info("Could not update status after successful delivery")
    except Exception as exc:
        log.exception("Audio download/send failed for user_id=%s", user.id)
        record_user(user, "failure")
        msg = str(exc)
        if "ffmpeg" in msg.lower() or "ffprobe" in msg.lower():
            reason = "На сервере не найден FFmpeg. Установи FFmpeg и перезапусти бота."
        elif "larger than" in msg.lower() or "49" in msg.lower() or "max_filesize" in msg.lower():
            reason = "Файл превышает лимит 49 МБ."
        elif "no space left" in msg.lower() or "disk" in msg.lower():
            reason = "На сервере недостаточно места на диске."
        else:
            reason = "Не удалось получить аудио. Источник может ограничивать скачивание или формат недоступен."
        try:
            await status.edit_text("⚠️ " + reason + "\nПопробуй другой найденный вариант.")
        except Exception:
            log.exception("Could not update failure status")
    finally:
        USER_ACTIVE.discard(user.id)
        # Mark the directory inactive only after removing it, so periodic cleanup won't race it.
        try:
            await asyncio.to_thread(shutil.rmtree, task_dir, True)
        finally:
            ACTIVE_TASK_DIRS.discard(str(task_dir.resolve()))

async def main():
    init_db()
    await asyncio.to_thread(cleanup_old_files, set(ACTIVE_TASK_DIRS))
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _telegram_error_handler.bind(bot, asyncio.get_running_loop())
    cleanup_task = asyncio.create_task(cleanup_loop())
    health_task = asyncio.create_task(health_monitor_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        health_task.cancel()
        await asyncio.gather(cleanup_task, health_task, return_exceptions=True)
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())