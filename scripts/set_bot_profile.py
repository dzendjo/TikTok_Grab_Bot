"""Set the bot's name, About, Description and command menus via the Bot API.

Written for @tiktok_grab_bot on 2026-10-26, not applied yet. The free-tier
number (30 a day) is hardcoded: keep it in sync with FREE_DAILY_DOWNLOADS.
Re-run after an admin presses /start (admin menus fail with "chat not found"
until then) or when texts change.

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
    "": "TikTok Video Downloader ⚡",
    "uk": "Завантажити відео з TikTok ⚡",
    "ru": "Скачать видео из TikTok ⚡",
}

ABOUT = {
    "": "TikTok videos without watermark and photo posts right in Telegram. ⚡ Instant from cache. ⭐ Premium: no limits.",
    "uk": "Відео з TikTok без водяного знака й фото-пости прямо в Telegram. ⚡ З кешу миттєво. ⭐ Premium: без лімітів.",
    "ru": "Видео из TikTok без водяного знака и фото-посты прямо в Telegram. ⚡ Из кэша мгновенно. ⭐ Premium: без лимитов.",
}

DESCRIPTION = {
    "": """Send a TikTok link and get the video without watermark right here in Telegram.

🎬 The best quality TikTok has
🖼 Photo posts arrive as an album
⚡ Popular videos arrive instantly from cache

🆓 Free: 30 downloads a day
⭐ Premium: no limits and no ads

Tap "Start" and send your first link.""",
    "uk": """Надішли посилання на TikTok — отримаєш відео без водяного знака прямо в Telegram.

🎬 Найкраща якість, яку дає TikTok
🖼 Фото-пости — альбомом
⚡ Популярні відео віддаю миттєво з кешу

🆓 Безкоштовно: 30 завантажень на добу
⭐ Premium: без лімітів і реклами

Натисни «Старт» і скинь перше посилання.""",
    "ru": """Отправь ссылку на TikTok — получишь видео без водяного знака прямо в Telegram.

🎬 Лучшее качество, которое даёт TikTok
🖼 Фото-посты — альбомом
⚡ Популярные видео отдаю мгновенно из кэша

🆓 Бесплатно: 30 загрузок в сутки
⭐ Premium: без лимитов и рекламы

Нажми «Старт» и скинь первую ссылку.""",
}

USER_COMMANDS = {
    "": [("start", "Start / change language"), ("help", "How to use the bot"),
         ("premium", "⭐ Premium: no limits or ads"), ("paysupport", "Payment and refund support")],
    "uk": [("start", "Почати / змінити мову"), ("help", "Як користуватися ботом"),
           ("premium", "⭐ Premium: без лімітів і реклами"), ("paysupport", "Питання щодо оплати й повернень")],
    "ru": [("start", "Начать / сменить язык"), ("help", "Как пользоваться ботом"),
           ("premium", "⭐ Premium: без лимитов и рекламы"), ("paysupport", "Вопросы по оплате и возвратам")],
}

# Shown only in admin chats (chat scope), on top of the Ukrainian user commands.
ADMIN_COMMANDS = [
    ("admin", "Адмін: список команд"), ("base", "Адмін: всього користувачів"),
    ("premiumstats", "Адмін: статистика Premium"), ("givepremium", "Адмін: USER_ID DAYS, видати Premium"),
    ("refund", "Адмін: CHARGE_ID, повернути зірки"), ("getbase", "Адмін: експорт user ID"),
    ("cleanbase", "Адмін: видалити неактивних"), ("draw", "Адмін: розсилка кампаній"),
    ("ads", "Адмін: кампанії BotAds"), ("broadcast", "Адмін: тест BotAds broadcast"),
    ("test1", "Адмін: тестове завантаження NATS (video|audio)"),
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
