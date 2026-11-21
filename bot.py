#!/usr/bin/env python3
"""
TechGuy — личный Telegram-агент для предустановки софта на macOS (Apple Silicon).

Кидаешь боту ссылку или команду — он показывает план действий,
ты подтверждаешь кнопкой — он выполняет и присылает отчёт.

Умеет (MVP):
  - ссылка на GitHub-репозиторий -> git clone / git pull в ~/TechGuy/repos
  - прямая ссылка на файл -> скачивание в ~/Downloads
  - сообщение вида "brew install htop" / "brew install --cask firefox" -> установка
  - ссылка на стрим + отрезок времени -> вырезает фрагмент (yt-dlp) в ~/Downloads
      пример: https://twitch.tv/videos/123456 05:00:00-06:30:00
  - "нарежь <ссылка> [старт-конец]" -> режет видео на части по 90 минут (720p)
      и складывает готовые файлы в ~/Downloads (в чат приходит только отчёт)
      пример: нарежь https://www.youtube.com/watch?v=XXXXX
              нарежь https://www.youtube.com/watch?v=XXXXX 03:00:00-06:30:00
  - скачивание ускорено через aria2 (16 параллельных потоков) — обходит
    троттлинг YouTube на одно соединение; резка частей идёт локально ffmpeg
  - во время работы показывает живой прогресс-бар [####------] 40% в сообщении
  - /status -> состояние бота и диска

Безопасность:
  - отвечает только владельцу (ALLOWED_USER_ID)
  - любое действие требует подтверждения кнопкой
  - чёрный список опасных команд (sudo, rm -rf /, dd в /dev и т.п.)
"""

import asyncio
import math
import os
import re
import shlex
import shutil
import time
import uuid
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
BASE_DIR = Path(os.getenv("BASE_DIR", "~/TechGuy")).expanduser()
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "~/Downloads")).expanduser()
REPOS_DIR = BASE_DIR / "repos"
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "7200"))  # секунд на одну команду
PROGRESS_EDIT_INTERVAL = 4  # как часто (сек) обновлять прогресс-бар в сообщении
CHUNK_SECONDS = int(os.getenv("CHUNK_SECONDS", "5400"))  # длина одной части (90 мин)
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/techguy_clips"))  # временная папка нарезки

# Видео короче этого порога скачиваем ЦЕЛИКОМ через aria2 (быстро: 16 потоков),
# а части режем локально ffmpeg. Видео длиннее — качаем кусками по месту (медленнее).
FULL_DOWNLOAD_MAX_SECONDS = int(os.getenv("FULL_DOWNLOAD_MAX_SECONDS", "21600"))  # 6 часов

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
REPOS_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

# Homebrew на Apple Silicon живёт в /opt/homebrew
BREW = (
    "/opt/homebrew/bin/brew"
    if Path("/opt/homebrew/bin/brew").exists()
    else (shutil.which("brew") or "brew")
)

# yt-dlp и ffmpeg (полные пути, если стоят через Homebrew на Apple Silicon)
YTDLP = (
    "/opt/homebrew/bin/yt-dlp"
    if Path("/opt/homebrew/bin/yt-dlp").exists()
    else (shutil.which("yt-dlp") or "yt-dlp")
)
FFMPEG = (
    "/opt/homebrew/bin/ffmpeg"
    if Path("/opt/homebrew/bin/ffmpeg").exists()
    else (shutil.which("ffmpeg") or "ffmpeg")
)
ENSURE_TOOLS_CMD = (
    f"command -v yt-dlp >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1 "
    f"&& command -v aria2c >/dev/null 2>&1 "
    f"|| {shlex.quote(BREW)} install yt-dlp ffmpeg aria2"
)

# Cookies браузера для YouTube ("Sign in to confirm you're not a bot"):
# YTDLP_COOKIES=safari|chrome|firefox|... в .env -> флаг --cookies-from-browser
_YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "").strip().lower()
COOKIES_OPT = f" --cookies-from-browser {shlex.quote(_YTDLP_COOKIES)}" if _YTDLP_COOKIES else ""

# Команды, которые бот откажется выполнять при любом раскладе
FORBIDDEN_PATTERNS = [
    r"\bsudo\b",
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+/",
    r"\bmkfs\b",
    r"\bdd\b[^\n]*of=/dev/",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\s+777\s+/",
]

BAR_WIDTH = 12  # длина прогресс-бара в символах: [######------]
SPINNER = "⠋⠙⠹⠸⠼⠴⦋⦧⦇⦏"

# "нарежь <ссылка> [HH:MM:SS-HH:MM:SS]"
SPLIT_RE = re.compile(
    r"нарежь\s+(https?://\S+)"
    r"(?:\s+(\d{1,3}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,3}:\d{2}(?::\d{2})?))?",
    re.IGNORECASE,
)

SAFE_NAME_RE = re.compile(r"[^\w\s.()-]+", re.UNICODE)

START_TIME = time.time()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# task_id -> {"type": "plan"|"split", ...}
PENDING: dict[str, dict] = {}


def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id == ALLOWED_USER_ID


def forbidden_reason(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in FORBIDDEN_PATTERNS)


def render_bar(pct: float) -> str:
    filled = max(0, min(BAR_WIDTH, round(pct / 100 * BAR_WIDTH)))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def clean_tmp() -> None:
    """Сносит всё содержимое временной папки нарезки (недокачки, .part, фрагменты)."""
    for p in TMP_DIR.glob("*"):
        try:
            p.unlink() if p.is_file() or p.is_symlink() else shutil.rmtree(p)
        except Exception:
            pass


def parse_hms(s: str) -> int:
    """'1:23:45' или '23:45' -> секунды."""
    sec = 0
    for part in s.split(":"):
        sec = sec * 60 + int(part)
    return sec


def fmt_hms(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_name(s: str, limit: int = 60) -> str:
    """Делает из названия видео безопасное имя файла."""
    s = SAFE_NAME_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].strip() or "video")


def move_to_downloads(src: Path, name: str) -> Path:
    """Переносит файл в ~/Downloads, не затирая существующий (добавляет -2, -3...)."""
    dest = DOWNLOADS_DIR / name
    n = 2
    while dest.exists():
        dest = DOWNLOADS_DIR / f"{dest.stem}-{n}{dest.suffix}"
        n += 1
    shutil.move(str(src), dest)
    return dest


async def exec_cmd(cmd: str, desc: str, status: Message | None = None) -> tuple[str, bool, bool]:
    """Выполняет shell-команду, обновляя прогресс-бар/спиннер в сообщении status.
    Возвращает (вывод, успех, был_таймаут)."""
    if forbidden_reason(cmd):
        return "Заблокировано политикой безопасности.", False, False

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    buf: list[str] = []
    pct: float | None = None
    spin = 0
    last_edit = 0.0
    t0 = time.time()
    timed_out = False

    while True:
        if time.time() - t0 > COMMAND_TIMEOUT:
            proc.kill()
            timed_out = True
            break
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), 1.0)
        except asyncio.TimeoutError:
            line = b""
        if line:
            chunk = line.decode(errors="replace")
            buf.append(chunk)
            # yt-dlp печатает прогресс как "[download]  42.3% of ...",
            # aria2 — как "[#abc 12MiB/500MiB(2%) CN:16 ...]"
            m = (re.search(r"\[download\]\s+~?\s*([\d.]+)%", chunk)
                 or re.search(r"\(([\d.]+)%\)", chunk))
            if m:
                pct = float(m.group(1))
        elif proc.stdout.at_eof():
            break

        now = time.time()
        if status is not None and now - last_edit >= PROGRESS_EDIT_INTERVAL:
            last_edit = now
            elapsed = int(now - t0)
            if pct is not None:
                text = (f"⚙️ <b>{escape(desc)}</b>\n"
                        f"<code>{render_bar(pct)} {pct:5.1f}%</code> ({elapsed} сек)")
            else:
                text = (f"⚙️ <b>{escape(desc)}</b>\n"
                        f"{SPINNER[spin % len(SPINNER)]} работаю… ({elapsed} сек)")
                spin += 1
            try:
                await status.edit_text(text)
            except Exception:
                pass  # Telegram ругается на одинаковый текст/флуд — просто пропускаем

    await proc.wait()
    return "".join(buf).strip(), proc.returncode == 0 and not timed_out, timed_out


async def run_plan(plan: list[tuple[str, str]], status: Message | None = None) -> tuple[list[str], bool]:
    """Выполняет план по шагам. Останавливается на первой ошибке."""
    reports: list[str] = []
    for desc, cmd in plan:
        if forbidden_reason(cmd):
            reports.append(f"⛔️ <b>{escape(desc)}</b>\nЗаблокировано политикой безопасности.")
            return reports, False
        output, ok, timed_out = await exec_cmd(cmd, desc, status)
        if timed_out:
            reports.append(f"⏱ <b>{escape(desc)}</b>\nТаймаут {COMMAND_TIMEOUT} сек — процесс убит.")
            return reports, False
        icon = "✅" if ok else "❌"
        reports.append(f"{icon} <b>{escape(desc)}</b>\n<code>{escape(output[-2500:]) or '(нет вывода)'}</code>")
        if not ok:
            return reports, False
    return reports, True


async def report_part(status: Message, fpath: Path, i: int, n: int, s: int, e: int, title: str) -> bool:
    """Переносит готовую часть в ~/Downloads и присылает отчёт. True = успех."""
    size_mb = fpath.stat().st_size / 2**20
    dest = move_to_downloads(fpath, f"{safe_name(title)} — часть {i + 1} из {n}.mp4")
    await status.reply(
        f"✅ Часть {i + 1}/{n}: {fmt_hms(s)}–{fmt_hms(e)} ({size_mb:.0f} МБ)\n"
        f"📁 <code>{dest}</code>")
    return True


async def run_split(url: str, t_start: int, t_end: int | None, status: Message) -> None:
    """Режет видео на части по CHUNK_SECONDS и складывает их в ~/Downloads."""
    clean_tmp()  # на случай прошлого обрыва — стираем старые недокачки

    # 0) убедиться, что yt-dlp, ffmpeg и aria2 на месте
    out, ok, _ = await exec_cmd(ENSURE_TOOLS_CMD, "Проверяю yt-dlp, ffmpeg и aria2 (поставлю, если нет)", status)
    if not ok:
        await status.edit_text(f"❌ Не смог поставить yt-dlp/ffmpeg/aria2:\n<code>{escape(out[-1500:])}</code>")
        return

    # 1) полная длительность и название видео
    out, ok, _ = await exec_cmd(
        f"{shlex.quote(YTDLP)} --no-playlist --no-warnings --no-cache-dir{COOKIES_OPT} "
        f"--print \"%(duration)s\" --print \"%(title)s\" {shlex.quote(url)}",
        "Определяю длительность видео", status)
    full_dur: float | None = None
    title = "video"
    lines = (out or "").strip().splitlines()
    for idx, ln in enumerate(lines):
        ln = ln.strip()
        if re.fullmatch(r"\d+(\.\d+)?", ln):
            full_dur = float(ln)
            if idx + 1 < len(lines):
                title = lines[idx + 1].strip() or "video"
            break
        if re.fullmatch(r"\d{1,3}:\d{2}(:\d{2})?", ln):
            full_dur = float(parse_hms(ln))
            if idx + 1 < len(lines):
                title = lines[idx + 1].strip() or "video"
            break

    if t_end is not None:
        total = t_end - t_start
    else:
        if full_dur is None:
            await status.edit_text(
                f"❌ Не смог получить длительность (это точно не прямой эфир?):\n"
                f"<code>{escape(out[-1500:]) or '(нет вывода)'}</code>")
            return
        t_start = 0
        t_end = int(full_dur)
        total = full_dur

    if total <= 0:
        await status.edit_text("❌ Длительность получилась нулевой или отрицательной — проверь таймкоды.")
        return

    n = math.ceil(total / CHUNK_SECONDS)
    if n > 12:
        await status.edit_text(
            f"😅 Это {n} частей ({fmt_hms(total)} видео) — скачиваться будет ЧАСАМИ "
            f"и сожрёт десятки гигабайт. Лучше укажи диапазон, напр.:\n"
            f"<code>нарежь {escape(url)} 03:00:00-06:00:00</code>")
        return

    # 2) скачивание
    # Быстрый путь: видео целиком короче FULL_DOWNLOAD_MAX_SECONDS — качаем его одним
    # файлом через aria2 (16 потоков), а части вырезаем локально ffmpeg за секунды.
    fast = full_dur is not None and full_dur <= FULL_DOWNLOAD_MAX_SECONDS
    if fast:
        await status.edit_text(
            f"📼 «{escape(title)}»: {fmt_hms(full_dur)} → частей: <b>{n}</b> "
            f"(по {CHUNK_SECONDS // 60} мин, 720p).\n"
            f"Качаю целиком через aria2 в 16 потоков — так YouTube не режет скорость.")
        src_tpl = str(TMP_DIR / "source.%(ext)s")
        cmd = (
            f"{shlex.quote(YTDLP)} --newline --no-cache-dir "
            f"-f \"bv*[height<=720]+ba/b[height<=720]/b\" --merge-output-format mp4 "
            f"--downloader aria2c "
            f"--downloader-args \"aria2c:-x 16 -s 16 -k 1M --console-log-level=warn --summary-interval=5\" "
            f"--no-playlist{COOKIES_OPT} -o {shlex.quote(src_tpl)} {shlex.quote(url)}"
        )
        out, ok, timed_out = await exec_cmd(
            cmd, f"Качаю видео ({fmt_hms(full_dur)}, 720p, 16 потоков)", status)
        sources = [f for f in TMP_DIR.glob("source.*") if f.suffix != ".part"]
        if not ok or not sources:
            tail = escape(out[-1500:]) or "(нет вывода)"
            clean_tmp()
            await status.edit_text(
                f"❌ Видео не скачалось{' (таймаут)' if timed_out else ''}.\n<code>{tail}</code>")
            return
        src = sources[0]

        # 3) локальная резка ffmpeg — быстро, без перекодирования
        done = 0
        for i in range(n):
            s = t_start + i * CHUNK_SECONDS
            e = int(min(s + CHUNK_SECONDS, t_end))
            part = TMP_DIR / f"part_{i + 1:02d}.mp4"
            cmd = (
                f"{shlex.quote(FFMPEG)} -hide_banner -loglevel error -y "
                f"-ss {s} -to {e} -i {shlex.quote(str(src))} "
                f"-c copy -movflags +faststart {shlex.quote(str(part))}"
            )
            out, ok, _ = await exec_cmd(cmd, f"Режу часть {i + 1}/{n} ({fmt_hms(s)}–{fmt_hms(e)})", status)
            if not ok or not part.exists():
                await status.reply(
                    f"⚠️ Часть {i + 1}/{n} не вырезалась:\n<code>{escape(out[-800:]) or '(нет вывода)'}</code>")
                continue
            await report_part(status, part, i, n, s, e, title)
            done += 1
        src.unlink(missing_ok=True)
        clean_tmp()  # финальная уборка
        await status.edit_text(
            f"✅ <b>Готово:</b> {done} из {n} частей лежат в ~/Downloads.\n"
            f"Исходник и временные файлы удалены.")
        return

    # Медленный путь: видео длиннее FULL_DOWNLOAD_MAX_SECONDS (напр. суточный стрим) —
    # качаем каждый кусок отдельно через --download-sections.
    await status.edit_text(
        f"📼 Видео длинное ({fmt_hms(full_dur or 0)}) — качаю части по очереди, "
        f"всего <b>{n}</b> (по {CHUNK_SECONDS // 60} мин, 720p). Поехали!")

    sent = 0
    for i in range(n):
        s = t_start + i * CHUNK_SECONDS
        e = int(min(s + CHUNK_SECONDS, t_end))
        part_tpl = str(TMP_DIR / f"part_{i + 1:02d}.%(ext)s")
        cmd = (
            f"{shlex.quote(YTDLP)} --newline --no-cache-dir --download-sections \"*{fmt_hms(s)}-{fmt_hms(e)}\" "
            f"-f \"bv*[height<=720]+ba/b[height<=720]/b\" --merge-output-format mp4 "
            f"--no-playlist{COOKIES_OPT} -o {shlex.quote(part_tpl)} {shlex.quote(url)}"
        )
        out, ok, timed_out = await exec_cmd(cmd, f"Часть {i + 1}/{n}: качаю {fmt_hms(s)} — {fmt_hms(e)}", status)
        files = [f for f in sorted(TMP_DIR.glob(f"part_{i + 1:02d}.*")) if f.suffix != ".part"]
        if not ok or not files:
            tail = escape(out[-1500:]) or "(нет вывода)"
            clean_tmp()  # подчистить недокачанные фрагменты
            await status.edit_text(
                f"❌ Часть {i + 1}/{n} не скачалась{' (таймаут)' if timed_out else ''}.\n"
                f"<code>{tail}</code>\n\nГотово до ошибки: {sent} из {n} (лежат в ~/Downloads).")
            return
        await report_part(status, files[0], i, n, s, e, title)
        sent += 1

    clean_tmp()  # финальная уборка: не оставляем ни одного временного файла
    await status.edit_text(
        f"✅ <b>Готово:</b> {sent} из {n} частей лежат в ~/Downloads. "
        f"Временные файлы удалены.")


def classify(text: str) -> list[tuple[str, str]] | None:
    """Превращает сообщение в план: список (человеческое описание, shell-команда)."""
    text = text.strip()

    # 0) Клип из стрима: "<ссылка> 05:00:00-06:30:00" (YouTube/Twitch VOD и т.п.)
    m = re.search(
        r"(https?://\S+)\s+(\d{1,3}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,3}:\d{2}(?::\d{2})?)",
        text,
    )
    if m:
        url, t_start, t_end = m.group(1), m.group(2), m.group(3)
        safe = f"clip_{t_start.replace(':', '')}-{t_end.replace(':', '')}"
        dest_tpl = str(DOWNLOADS_DIR / f"{safe}.%(ext)s")
        ytdlp_cmd = (
            f"{shlex.quote(YTDLP)} --newline --no-cache-dir --download-sections \"*{t_start}-{t_end}\" "
            f"-f \"bv*[height<=1080]+ba/b[height<=1080]/b\" --merge-output-format mp4 "
            f"--no-playlist{COOKIES_OPT} -o {shlex.quote(dest_tpl)} {shlex.quote(url)}"
        )
        return [
            ("Проверяю yt-dlp, ffmpeg и aria2 (поставлю, если нет)", ENSURE_TOOLS_CMD),
            (f"Скачиваю фрагмент {t_start} — {t_end} в {DOWNLOADS_DIR}", ytdlp_cmd),
            ("Показываю готовый файл",
             f"ls -lh {shlex.quote(str(DOWNLOADS_DIR))}/{safe}.*"),
        ]

    # 1) GitHub-репозиторий
    m = re.search(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", text)
    if m:
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
        dest = REPOS_DIR / repo
        url = f"https://github.com/{owner}/{repo}.git"
        if dest.exists():
            return [(f"Репозиторий уже есть — обновляю {dest}",
                     f"git -C {shlex.quote(str(dest))} pull --ff-only")]
        return [(f"Клонирую {owner}/{repo} в {dest}",
                 f"git clone {shlex.quote(url)} {shlex.quote(str(dest))}")]

    # 2) Homebrew: "brew install htop" или "brew install --cask firefox"
    m = re.match(r"brew\s+(install|reinstall|upgrade)\s+(--cask\s+)?([\w@.+-]+)\s*$", text)
    if m:
        action, cask, pkg = m.group(1), m.group(2) or "", m.group(3)
        return [(f"Ставлю через Homebrew: {pkg}",
                 f"{shlex.quote(BREW)} {action} {cask}{shlex.quote(pkg)}")]

    # 3) Любая другая http(s)-ссылка -> скачать файл
    m = re.search(r"https?://\S+", text)
    if m:
        url = m.group(0)
        name = Path(urlparse(url).path).name or "downloaded_file"
        dest = DOWNLOADS_DIR / name
        return [(f"Скачиваю файл в {dest}",
                 f"curl -fL --retry 2 -o {shlex.quote(str(dest))} {shlex.quote(url)}")]

    return None


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "🤖 <b>TechGuy на связи.</b>\n\n"
        "Кидай:\n"
        "• ссылку на GitHub — склонирую в ~/TechGuy/repos\n"
        "• ссылку на файл — скачаю в ~/Downloads\n"
        "• <code>brew install &lt;пакет&gt;</code> — поставлю через Homebrew\n"
        "• <b>ссылку на стрим + время</b> — вырежу фрагмент:\n"
        "<code>https://twitch.tv/videos/123 05:00:00-06:30:00</code>\n"
        "• <b>нарежь &lt;ссылка&gt;</b> — порежу видео на части по 90 мин "
        "и сложу их в ~/Downloads:\n"
        "<code>нарежь https://youtube.com/watch?v=XXX</code>\n"
        "<code>нарежь https://youtube.com/watch?v=XXX 03:00:00-06:00:00</code>\n\n"
        "Качаю быстро (aria2, 16 потоков), режу локально, мусор удаляю.\n"
        "Прогресс-бар — прямо в сообщении. /status — состояние."
    )


@dp.message(Command("status"))
async def on_status(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    uptime_min = int(time.time() - START_TIME) // 60
    usage = shutil.disk_usage("/")
    cookies_mode = _YTDLP_COOKIES or "выключены"
    await message.answer(
        f"🤖 Живой, аптайм {uptime_min} мин.\n"
        f"💾 Диск: свободно {usage.free / 2**30:.1f} ГБ из {usage.total / 2**30:.1f} ГБ\n"
        f"📁 Загрузки: <code>{DOWNLOADS_DIR}</code>\n"
        f"📁 Репозитории: <code>{REPOS_DIR}</code>\n"
        f"🍪 Cookies для YouTube: {cookies_mode}"
    )


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return  # чужих молча игнорируем

    text = message.text or ""

    # "нарежь <ссылка> [старт-конец]" — нарезка видео на части в ~/Downloads
    m = SPLIT_RE.search(text)
    if m:
        url = m.group(1)
        t_start = parse_hms(m.group(2)) if m.group(2) else 0
        t_end = parse_hms(m.group(3)) if m.group(3) else None
        task_id = uuid.uuid4().hex[:8]
        PENDING[task_id] = {"type": "split", "url": url, "t_start": t_start, "t_end": t_end}
        rng = f" диапазон {m.group(2)}–{m.group(3)}" if t_end else " целиком"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выполнить", callback_data=f"run:{task_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{task_id}"),
        ]])
        await message.answer(
            f"<b>План действий:</b>\n"
            f"1. Определю длительность и название видео{rng}\n"
            f"2. Скачаю в 720p через aria2 (16 потоков — быстро)\n"
            f"3. Нарежу на части по {CHUNK_SECONDS // 60} мин и сложу в ~/Downloads\n"
            f"4. Пришлю сюда список готовых файлов, мусор удалю",
            reply_markup=kb)
        return

    plan = classify(text)
    if not plan:
        await message.answer(
            "Не понял, что с этим делать 🤔\n"
            "Пришли ссылку на GitHub, ссылку на файл, команду вида "
            "<code>brew install htop</code>, ссылку на стрим с отрезком "
            "<code>https://twitch.tv/videos/123 05:00:00-06:30:00</code> "
            "или <code>нарежь &lt;ссылка&gt;</code>."
        )
        return

    task_id = uuid.uuid4().hex[:8]
    PENDING[task_id] = {"type": "plan", "plan": plan}

    lines = ["<b>План действий:</b>"]
    for i, (desc, cmd) in enumerate(plan, 1):
        lines.append(f"{i}. {escape(desc)}\n<code>{escape(cmd)}</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнить", callback_data=f"run:{task_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{task_id}"),
    ]])
    await message.answer("\n".join(lines), reply_markup=kb)


@dp.callback_query(F.data.startswith(("run:", "cancel:")))
async def on_decision(call: CallbackQuery) -> None:
    if not is_owner(call.from_user.id if call.from_user else None):
        await call.answer("Это не твой бот.", show_alert=True)
        return

    action, task_id = call.data.split(":", 1)
    task = PENDING.pop(task_id, None)
    if task is None:
        await call.answer("Задача устарела или уже выполнена.", show_alert=True)
        return

    if action == "cancel":
        await call.message.edit_text("🚫 Отменено.")
        return

    await call.message.edit_text("⚙️ Запускаю…")

    if task.get("type") == "split":
        await run_split(task["url"], task["t_start"], task["t_end"], call.message)
        return

    reports, ok = await run_plan(task["plan"], status=call.message)
    header = "✅ <b>Готово.</b>" if ok else "⚠️ <b>Остановился на ошибке:</b>"
    await call.message.edit_text(header + "\n\n" + "\n\n".join(reports))


async def main() -> None:
    if not BOT_TOKEN or not ALLOWED_USER_ID:
        raise SystemExit("Заполни BOT_TOKEN и ALLOWED_USER_ID в файле .env")
    print(f"TechGuy запущен. Владелец: {ALLOWED_USER_ID}. База: {BASE_DIR}. Загрузки: {DOWNLOADS_DIR}")
    print(f"Cookies для YouTube: {_YTDLP_COOKIES or 'выключены'}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
