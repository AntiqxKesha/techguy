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
  - во время работы показывает живой прогресс-бар [####------] 40% в сообщении
  - /status -> состояние бота и диска

Безопасность:
  - отвечает только владельцу (ALLOWED_USER_ID)
  - любое действие требует подтверждения кнопкой
  - чёрный список опасных команд (sudo, rm -rf /, dd в /dev и т.п.)
"""

import asyncio
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
BASE_DIR = Path(os.getenv("BASE_DIR", "~/TechGuy")).expanduser()
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "~/Downloads")).expanduser()
REPOS_DIR = BASE_DIR / "repos"
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "3600"))  # секунд на одну команду
PROGRESS_EDIT_INTERVAL = 4  # как часто (сек) обновлять прогресс-бар в сообщении

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
REPOS_DIR.mkdir(parents=True, exist_ok=True)

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

START_TIME = time.time()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# task_id -> {"plan": [(описание, команда), ...]}
PENDING: dict[str, dict] = {}


def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id == ALLOWED_USER_ID


def forbidden_reason(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in FORBIDDEN_PATTERNS)


def render_bar(pct: float) -> str:
    filled = max(0, min(BAR_WIDTH, round(pct / 100 * BAR_WIDTH)))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def classify(text: str) -> list[tuple[str, str]] | None:
    """Превращает сообщение в план: список (человеческое описание, shell-команда)."""
    text = text.strip()

    # 0) Клип из стрима: "<ссылка> 05:00:00-06:30:00" (YouTube/Twitch VOD и т.п.)
    m = re.search(
        r"(https?://\S+)\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)",
        text,
    )
    if m:
        url, t_start, t_end = m.group(1), m.group(2), m.group(3)
        safe_name = f"clip_{t_start.replace(':', '')}-{t_end.replace(':', '')}"
        dest_tpl = str(DOWNLOADS_DIR / f"{safe_name}.%(ext)s")
        ytdlp_cmd = (
            f"{shlex.quote(YTDLP)} --newline --download-sections \"*{t_start}-{t_end}\" "
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


async def run_plan(plan: list[tuple[str, str]], status: Message | None = None) -> tuple[list[str], bool]:
    """Выполняет план по шагам. Останавливается на первой ошибке.
    Если передано сообщение status — обновляет в нём прогресс-бар / спиннер."""
    reports: list[str] = []
    for desc, cmd in plan:
        if forbidden_reason(cmd):
            reports.append(f"⛔️ <b>{escape(desc)}</b>\nЗаблокировано политикой безопасности.")
            return reports, False

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
        if timed_out:
            reports.append(f"⏱ <b>{escape(desc)}</b>\nТаймаут {COMMAND_TIMEOUT} сек — процесс убит.")
            return reports, False

        output = "".join(buf).strip()[-2500:]
        ok = proc.returncode == 0
        icon = "✅" if ok else "❌"
        reports.append(f"{icon} <b>{escape(desc)}</b>\n<code>{escape(output) or '(нет вывода)'}</code>")
        if not ok:
            return reports, False
    return reports, True


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
        "• <b>ссылку на стрим + время</b> — вырежу фрагмент, напр.:\n"
        "<code>https://twitch.tv/videos/123 05:00:00-06:30:00</code>\n\n"
        "Во время работы показываю прогресс-бар прямо в сообщении. "
        "Каждое действие сначала покажу планом и спрошу подтверждение. /status — состояние."
    )


@dp.message(Command("status"))
async def on_status(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    uptime_min = int(time.time() - START_TIME) // 60
    usage = shutil.disk_usage("/")
    await message.answer(
        f"🤖 Живой, аптайм {uptime_min} мин.\n"
        f"💾 Диск: свободно {usage.free / 2**30:.1f} ГБ из {usage.total / 2**30:.1f} ГБ\n"
        f"📁 Загрузки: <code>{DOWNLOADS_DIR}</code>\n"
        f"📁 Репозитории: <code>{REPOS_DIR}</code>"
    )


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return  # чужих молча игнорируем

    plan = classify(message.text or "")
    if not plan:
        await message.answer(
            "Не понял, что с этим делать 🤔\n"
            "Пришли ссылку на GitHub, ссылку на файл, команду вида "
            "<code>brew install htop</code> или ссылку на стрим с отрезком: "
            "<code>https://twitch.tv/videos/123 05:00:00-06:30:00</code>."
        )
        return

    task_id = uuid.uuid4().hex[:8]
    PENDING[task_id] = {"plan": plan}

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
    reports, ok = await run_plan(task["plan"], status=call.message)
    header = "✅ <b>Готово.</b>" if ok else "⚠️ <b>Остановился на ошибке:</b>"
    await call.message.edit_text(header + "\n\n" + "\n\n".join(reports))


async def main() -> None:
    if not BOT_TOKEN or not ALLOWED_USER_ID:
        raise SystemExit("Заполни BOT_TOKEN и ALLOWED_USER_ID в файле .env")
    print(f"TechGuy запущен. Владелец: {ALLOWED_USER_ID}. База: {BASE_DIR}. Загрузки: {DOWNLOADS_DIR}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
