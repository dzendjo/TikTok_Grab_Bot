"""Set the bot's name, About, Description and command menus via the Bot API.

Applied to @Ytbvideobot on 2026-10-08; launch limits (10/day, no 500 MB cap)
on 2026-10-12. The free-tier numbers below are hardcoded: keep them in sync
with FREE_DAILY_DOWNLOADS / FREE_MAX_FILE_MB in the prod .env. Re-run after an admin presses /start
(admin menus fail with "chat not found" until then) or when texts change.

Run from the project root: .venv/bin/python scripts/set_bot_profile.py
Reads BOT_TOKEN and ADMIN_IDS from the environment or from .env.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for line in Path(".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{key} not set")


TOKEN = _env("BOT_TOKEN")
ADMINS = [int(x) for x in _env("ADMIN_IDS").replace(" ", "").split(",") if x]

# "" = default for every language without its own version (English).
NAMES = {
    "": "YouTube Video Downloader ⚡",
    "uk": "Завантажити відео з YouTube ⚡",
    "ru": "Скачать видео с YouTube ⚡",
}

ABOUT = {
    "": "YouTube video and audio right in Telegram: 360p–1080p or 🎧 audio. ⚡ Instant from cache. ⭐ Premium: 1080p, no limits.",
    "uk": "Завантажую відео та аудіо з YouTube у Telegram: 360p–1080p або 🎧 аудіо. ⚡ З кешу миттєво. ⭐ Premium: 1080p, без лімітів.",
    "ru": "Скачиваю видео и аудио с YouTube в Telegram: 360p–1080p или 🎧 аудио. ⚡ Из кэша мгновенно. ⭐ Premium: 1080p, без лимитов.",
}

DESCRIPTION = {
    "": """Send a YouTube link and get the video or audio right here in Telegram.

🎬 Pick the quality: 360p, 480p, 720p, 1080p or 🎧 audio
⚡ Popular videos arrive instantly from cache
📲 Works via "Share" from the YouTube app

🆓 Free: 10 downloads a day, up to 720p, files up to 2 GB
⭐ Premium: 1080p and 1080p60, no limits or ads, video transcript as a file

Tap "Start" and send your first link.""",
    "uk": """Надішли посилання на YouTube — отримаєш відео або аудіо прямо в Telegram.

🎬 Якість на вибір: 360p, 480p, 720p, 1080p або 🎧 аудіо
⚡ Популярні відео віддаю миттєво з кешу
📲 Працює через «Поділитися» з додатка YouTube

🆓 Безкоштовно: 10 завантажень на добу, до 720p, файли до 2 ГБ
⭐ Premium: 1080p і 1080p60, без лімітів і реклами, текст відео файлом

Натисни «Старт» і скинь перше посилання.""",
    "ru": """Отправь ссылку на YouTube — получишь видео или аудио прямо в Telegram.

🎬 Качество на выбор: 360p, 480p, 720p, 1080p или 🎧 аудио
⚡ Популярные видео отдаю мгновенно из кэша
📲 Работает через «Поделиться» из приложения YouTube

🆓 Бесплатно: 10 загрузок в сутки, до 720p, файлы до 2 ГБ
⭐ Premium: 1080p и 1080p60, без лимитов и рекламы, текст видео файлом

Нажми «Старт» и скинь первую ссылку.""",
}

USER_COMMANDS = {
    "": [("start", "Start / change language"), ("help", "How to use the bot"),
         ("premium", "⭐ Premium: 1080p, no limits or ads"), ("paysupport", "Payment and refund support")],
    "uk": [("start", "Почати / змінити мову"), ("help", "Як користуватися ботом"),
           ("premium", "⭐ Premium: 1080p, без лімітів і реклами"), ("paysupport", "Питання щодо оплати й повернень")],
    "ru": [("start", "Начать / сменить язык"), ("help", "Как пользоваться ботом"),
           ("premium", "⭐ Premium: 1080p, без лимитов и рекламы"), ("paysupport", "Вопросы по оплате и возвратам")],
}

# Shown only in admin chats (chat scope), on top of the Ukrainian user commands.
ADMIN_COMMANDS = [
    ("admin", "Адмін: список команд"), ("base", "Адмін: всього користувачів"),
    ("premiumstats", "Адмін: статистика Premium"), ("givepremium", "Адмін: USER_ID DAYS, видати Premium"),
    ("refund", "Адмін: CHARGE_ID, повернути зірки"), ("getbase", "Адмін: експорт user ID"),
    ("cleanbase", "Адмін: видалити неактивних"), ("draw", "Адмін: розсилка кампаній"),
    ("ads", "Адмін: кампанії BotAds"), ("broadcast", "Адмін: тест BotAds broadcast"),
    ("test", "Адмін: тест метаданих NATS"), ("test1", "Адмін: тестове завантаження NATS"),
]


def call(method: str, **params) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as exc:
        return json.load(exc)


def report(what: str, resp: dict) -> None:
    print(f"{what}: {'ok' if resp.get('ok') else resp.get('description')}")


def commands(pairs):
    return [{"command": c, "description": d} for c, d in pairs]


def main() -> None:
    print("bot:", call("getMe")["result"]["username"])
    for lang in NAMES:
        tag = lang or "default"
        report(f"name [{tag}]", call("setMyName", name=NAMES[lang], language_code=lang))
        report(f"about [{tag}]", call("setMyShortDescription", short_description=ABOUT[lang], language_code=lang))
        report(f"description [{tag}]", call("setMyDescription", description=DESCRIPTION[lang], language_code=lang))
        report(f"commands [{tag}]", call("setMyCommands", commands=commands(USER_COMMANDS[lang]), language_code=lang))
    for admin_id in ADMINS:
        resp = call("setMyCommands", commands=commands(USER_COMMANDS["uk"] + ADMIN_COMMANDS),
                    scope={"type": "chat", "chat_id": admin_id})
        report(f"admin commands [{admin_id}]", resp)


if __name__ == "__main__":
    main()
