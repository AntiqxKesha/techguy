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
      и присылает каждую часть прямо в чат (файлы на диске не хранит)
      пример: нарежь https://twitch.tv/videos/123456
              нарежь https://twitch.tv/videos/123456 03:00:00-06:30:00
  - во время работы показывает живой прогресс-бар [####------] 40% в сообщении
  - /status -> состояние бота и диска

Отправка больших видео (>50 МБ) требует локального Bot API сервера:
  docker run -d --name tg-bot-api -p 127.0.0.1:8081:8081 \
    -e TELEGRAM_API_ID=... -e TELEGRAM_API_HASH=... -e TELEGRAM_LOCAL=1 \
    aiogram/telegram-bot-api:latest
  и TELEGRAM_API_ID / TELEGRAM_API_HASH в .env (ключи с my.telegram.org).
Без него части больше 49 МБ бот отправить не сможет — оставит файл в ~/Downloads.

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
    FSInputFile,
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
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "3600"))  # секунд на одну команду
PROGRESS_EDIT_INTERVAL = 4  # как часто (сек) обновлять прогресс-бар в сообщении
CHUNK_SECONDS = int(os.getenv("CHUNK_SECONDS", "5400"))  # длина одной части (90 мин)
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/techguy_clips"))  # временная папка нарезки

# Локальный Bot API сервер (для отправки файлов > 50 МБ, лимит 2000 МБ)
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API", "http://127.0.0.1:8081").strip()

MAX_SEND_MB = 2000 if (TELEGRAM_API_ID and TELEGRAM_API_HASH) else 49

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
ENSURE_YTDLP_CMD = (
    f"command -v yt-dlp >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1 "
    f"|| {shlex.quote(BREW)} install yt-dlp ffmpeg"
)

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

START_TIME = time.time()

session = None
if TELEGRAM_API_ID and TELEGRAM_API_HASH:
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL))

bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
            # yt-dlp печатает прогресс как "[download]  42.3% of ..."
            m = re.search(r"\[download\]\s+~?\s*([\d.]+)%", chunk)
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


async def run_split(url: str, t_start: int, t_end: int | None, status: Message) -> None:
    """Режет видео на части по CHUNK_SECONDS и присылает каждую в чат."""
    clean_tmp()  # на случай прошлого обрыва — стираем старые недокачки

    # 0) убедиться, что yt-dlp и ffmpeg на месте
    out, ok, _ = await exec_cmd(ENSURE_YTDLP_CMD, "Проверяю yt-dlp и ffmpeg (поставлю, если нет)", status)
    if not ok:
        await status.edit_text(f"❌ Не смог поставить yt-dlp/ffmpeg:\n<code>{escape(out[-1500:])}</code>")
        return

    # 1) длительность
    if t_end is not None:
        total = t_end - t_start
    else:
        out, ok, _ = await exec_cmd(
            f"{shlex.quote(YTDLP)} --no-playlist --no-warnings --no-cache-dir --get-duration {shlex.quote(url)}",
            "Определяю длительность видео", status)
        if not ok or not out:
            await status.edit_text(
                f"❌ Не смог получить длительность (это точно не прямой эфир?):\n"
                f"<code>{escape(out[-1500:]) or '(нет вывода)'}</code>")
            return
        try:
            total = parse_hms(out.strip().splitlines()[-1])
        except ValueError:
            await status.edit_text(f"❌ Странный ответ yt-dlp: <code>{escape(out[-500:])}</code>")
            return
        t_start = 0

    if total <= 0:
        await status.edit_text("❌ Длительность получилась нулевой или отрицательной — проверь таймкоды.")
        return

    n = math.ceil(total / CHUNK_SECONDS)
    if n > 12:
        await status.edit_text(
            f"😅 Это {n} частей ({fmt_hms(total)} видео) — скачиваться и заливаться будет ЧАСАМИ "
            f"и сожрёт десятки гигабайт трафика. Лучше укажи диапазон, напр.:\n"
            f"<code>нарежь {escape(url)} 03:00:00-06:00:00</code>")
        return

    await status.edit_text(
        f"📼 Длительность {fmt_hms(total)} → частей: <b>{n}</b> "
        f"(по {CHUNK_SECONDS // 60} мин, 720p). Поехали!")

    sent = 0
    for i in range(n):
        s = t_start + i * CHUNK_SECONDS
        e = min(s + CHUNK_SECONDS, t_start + total)
        part_tpl = str(TMP_DIR / f"part_{i + 1:02d}.%(ext)s")
        cmd = (
            f"{shlex.quote(YTDLP)} --newline --no-cache-dir --download-sections \"*{fmt_hms(s)}-{fmt_hms(e)}\" "
            f"-f \"bv*[height<=720]+ba/b[height<=720]/b\" --merge-output-format mp4 "
            f"--no-playlist -o {shlex.quote(part_tpl)} {shlex.quote(url)}"
        )
        out, ok, timed_out = await exec_cmd(cmd, f"Часть {i + 1}/{n}: качаю {fmt_hms(s)} — {fmt_hms(e)}", status)
        files = sorted(TMP_DIR.glob(f"part_{i + 1:02d}.*"))
        if not ok or not files:
            tail = escape(out[-1500:]) or "(нет вывода)"
            clean_tmp()  # подчистить недокачанные фрагменты
            await status.edit_text(
                f"❌ Часть {i + 1}/{n} не скачалась{' (таймаут)' if timed_out else ''}.\n"
                f"<code>{tail}</code>\n\nОтправлено до ошибки: {sent} из {n}.")
            return

        fpath = files[0]
        size_mb = fpath.stat().st_size / 2**20
        caption = f"Часть {i + 1}/{n}: {fmt_hms(s)}–{fmt_hms(e)} ({size_mb:.0f} МБ)"

        if size_mb > MAX_SEND_MB:
            # не влезает в лимит Telegram — паркуем в Загрузки и честно говорим
            dest = DOWNLOADS_DIR / fpath.name
            shutil.move(str(fpath), dest)
            await status.reply(
                f"⚠️ {caption} — не влезает в лимит Telegram ({MAX_SEND_MB} МБ). "
                f"Оставил файл здесь: <code>{dest}</code>")
            continue

        try:
            await status.edit_text(f"📤 Отправляю часть {i + 1}/{n} ({size_mb:.0f} МБ)…")
        except Exception:
            pass
        try:
            await bot.send_video(status.chat.id, FSInputFile(fpath),
                                 caption=caption, supports_streaming=True)
            sent += 1
        except Exception as exc:
            dest = DOWNLOADS_DIR / fpath.name
            shutil.move(str(fpath), dest)
            await status.reply(
                f"⚠️ Часть {i + 1}/{n}: не смог отправить ({escape(str(exc)[:200])}). "
                f"Файл здесь: <code>{dest}</code>")
            continue
        finally:
            fpath.unlink(missing_ok=True)

    clean_tmp()  # финальная уборка: не оставляем ни одного временного файла
    await status.edit_text(
        f"✅ <b>Готово:</b> отправил {sent} из {n} частей. "
        f"Временные файлы удалены, на диске ничего не храню.")


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
        safe_name = f"clip_{t_start.replace(':', '')}-{t_end.replace(':', '')}"
        dest_tpl = str(DOWNLOADS_DIR / f"{safe_name}.%(ext)s")
        ytdlp_cmd = (
            f"{shlex.quote(YTDLP)} --newline --no-cache-dir --download-sections \"*{t_start}-{t_end}\" "
            f"-f \"bv*[height<=1080]+ba/b[height<=1080]/b\" --merge-output-format mp4 "
            f"--no-playlist -o {shlex.quote(dest_tpl)} {shlex.quote(url)}"
        )
        return [
            ("Проверяю yt-dlp и ffmpeg (поставлю, если нет)", ENSURE_YTDLP_CMD),
            (f"Скачиваю фрагмент {t_start} — {t_end} в {DOWNLOADS_DIR}", ytdlp_cmd),
            ("Показываю готовый файл",
             f"ls -lh {shlex.quote(str(DOWNLOADS_DIR))}/{safe_name}.*"),
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
        "и пришлю каждую прямо сюда:\n"
        "<code>нарежь https://twitch.tv/videos/123</code>\n"
        "<code>нарежь https://twitch.tv/videos/123 03:00:00-06:00:00</code>\n\n"
        "Во время работы показываю прогресс-бар прямо в сообщении. /status — состояние."
    )


@dp.message(Command("status"))
async def on_status(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    uptime_min = int(time.time() - START_TIME) // 60
    usage = shutil.disk_usage("/")
    api_mode = f"локальный Bot API (до {MAX_SEND_MB} МБ)" if session else f"облачный API (до {MAX_SEND_MB} МБ)"
    await message.answer(
        f"🤖 Живой, аптайм {uptime_min} мин.\n"
        f"💾 Диск: свободно {usage.free / 2**30:.1f} ГБ из {usage.total / 2**30:.1f} ГБ\n"
        f"📁 Загрузки: <code>{DOWNLOADS_DIR}</code>\n"
        f"📁 Репозитории: <code>{REPOS_DIR}</code>\n"
        f"📤 Отправка файлов: {api_mode}"
    )


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return  # чужих молча игнорируем

    text = message.text or ""

    # "нарежь <ссылка> [старт-конец]" — особый сценарий с отправкой видео в чат
    m = SPLIT_RE.search(text)
    if m:
        url = m.group(1)
        t_start = parse_hms(m.group(2)) if m.group(2) else 0
        t_end = parse_hms(m.group(3)) if m.group(3) else None
        task_id = uuid.uuid4().hex[:8]
        PENDING[task_id] = {"type": "split", "url": url, "t_start": t_start, "t_end": t_end}
        rng = f" диапазон {m.group(2)}–{m.group(3)}" if t_end else " целиком"
        mode = (f"части до {MAX_SEND_MB} МБ пришлю прямо сюда"
                if session else
                f"⚠️ локальный Bot API не настроен — части больше {MAX_SEND_MB} МБ "
                f"не смогу отправить, оставлю в ~/Downloads")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выполнить", callback_data=f"run:{task_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{task_id}"),
        ]])
        await message.answer(
            f"<b>План действий:</b>\n"
            f"1. Определю длительность видео{rng}\n"
            f"2. Нарежу на части по {CHUNK_SECONDS // 60} мин (720p)\n"
            f"3. Каждую часть пришлю ответом в этот чат, временные файлы удалю\n\n"
            f"📤 {mode}",
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
    if session is not None:
        try:
            await bot.log_out()  # перенос бота с облачного API на локальный сервер
        except Exception as e:
            print(f"Предупреждение: log_out на локальный Bot API не удался: {e}")
    print(f"TechGuy запущен. Владелец: {ALLOWED_USER_ID}. База: {BASE_DIR}. Загрузки: {DOWNLOADS_DIR}")
    print(f"Отправка файлов: {'локальный Bot API, до 2000 МБ' if session else 'облачный API, до 49 МБ'}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
