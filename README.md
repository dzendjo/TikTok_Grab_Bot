# YtbVideoBot (aiogram 3)

Бот для завантаження відео та аудіо з YouTube. Основа — AudioBot
(`~/Desktop/Audiobot`): та сама адмінка, розсилки, BotAds, кеш і NATS-флоу.

## Як працює

1. Користувач надсилає посилання на YouTube.
2. Бот відповідає клавіатурою якості: **360p · 480p · 720p · 🎧 Аудіо**.
3. Після вибору:
   - якщо файл цієї якості вже є в кеші (`tracks`, окремий запис на кожен
     format_id: 134/135/136/140) — копіюється одразу;
   - інакше створюється `DownloadRequest` і запит іде в YT-API
     (`manager_download.<BOT_NAME>`, `quality` = `360p`/`480p`/`720p`/`audio`).
     Повідомлення з клавіатурою стає статусом прогресу.
4. Uploader YT-API кладе файл у source-канал з підписом `YTB|v1|rid=…|q=…`,
   бот ловить `channel_post`, кешує й копіює файл користувачу.

## Що збережено з AudioBot

- вибір мови (`/start`), `/help`, uk/en/ru локалізації
- антиспам: 20 запитів/год → блок на 6 год
- адмін-команди: `/admin`, `/base`, `/cleanbase`, `/getbase`, `/draw`, `/ads`, `/broadcast`
- `/test` — метадані через NATS, `/test1 [360p|480p|720p|audio]` — тестове завантаження
- whitelist каналів (бот виходить з чужих каналів)

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заповнити BOT_TOKEN, ADMIN_IDS, NATS_*, ALLOWED_SOURCE_CHANNEL_IDS,
# AD_API_BASE_URL/AD_BOT_HASH/AD_CACHE_CHANNEL_ID (BotAdsLib)
python -m app
```

## Перед запуском

- Бот має бути адміном у source-каналах (`ALLOWED_SOURCE_CHANNEL_IDS`), куди
  uploader YT-API кладе файли, — інакше він не отримає `channel_post` і не
  зможе скопіювати файл.
- NATS на сервері YT-API закритий фаєрволом: підключатися можна лише з
  дозволених IP серверів ботів.
