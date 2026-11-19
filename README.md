# TechGuy — личный агент-предустановщик для macOS (Apple Silicon)

Telegram-бот, который живёт на твоём Маке и делает предустановки:
кидаешь ему ссылку — он показывает план, ты жмёшь «Выполнить» — он делает и отчитывается.

## Что понадобится

1. **Xcode Command Line Tools** (для `git`):
   ```bash
   xcode-select --install
   ```
2. **Homebrew** (для сценариев `brew install ...`):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
3. **Токен бота**: в Telegram открой @BotFather → `/newbot` → получи токен.
4. **Свой user id**: открой @userinfobot → он пришлёт число.

## Установка (≈10 минут)

```bash
cd ~
git clone https://github.com/AntiqxKesha/techguy.git tech-guy-bot
cd tech-guy-bot
python3 -m venv ../techguy-venv
source ../techguy-venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env        # вставь BOT_TOKEN и ALLOWED_USER_ID
python bot.py    # должен написать "TechGuy запущен"
```

Проверка: напиши боту в Telegram `/start`, затем кинь любую ссылку на GitHub-репозиторий.

## Автозапуск при включении Мака (launchd)

```bash
# в com.techguy.bot.plist замени USERNAME на своё имя (узнать: whoami)
mkdir -p ~/TechGuy
cp com.techguy.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.techguy.bot.plist
```

Теперь бот стартует сам после перезагрузки и перезапускается при падении.
Логи: `~/TechGuy/bot.log` и `~/TechGuy/bot.error.log`.
Остановить: `launchctl unload ~/Library/LaunchAgents/com.techguy.bot.plist`.

Важно: бот работает, только когда Мак включён и не спит. Чтобы он не засыпал
при закрытой крышке, либо держи его на питании («Не переводить в сон» в настройках),
либо используй `caffeinate -dims`.

## Что умеет MVP

| Ты пишешь | Бот делает |
|---|---|
| `https://github.com/user/repo` | `git clone` в `~/TechGuy/repos` (или `git pull`, если уже есть) |
| ссылка на файл (.dmg, .zip, ...) | скачивает в `~/TechGuy/downloads` |
| `brew install htop` / `brew install --cask firefox` | установка через Homebrew |
| `/status` | аптайм, свободное место, пути |

Каждое действие — только после твоего подтверждения кнопкой.

## Безопасность

- Бот отвечает только твоему Telegram ID, остальных молча игнорирует.
- Чёрный список команд: `sudo`, `rm -rf /`, `dd` в `/dev`, `shutdown` и т.п. — заблокированы.
- Все команды видны в плане ДО выполнения.
- `.env` с токеном в `.gitignore` — в репозиторий не попадёт.

## Куда расти дальше

- **LLM-мозг на Ollama** (бесплатно и локально на M2): бот сам читает README
  репозитория и проводит полную установку, а не только clone.
- Автоустановка `.dmg`: смонтировать → скопировать .app в /Applications → отмонтировать.
- Очередь задач, прогресс длинных установок, расписание.
