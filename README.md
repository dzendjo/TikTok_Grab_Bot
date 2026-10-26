# TikTok Grab Bot (aiogram 3)

@tiktok_grab_bot завантажує з TikTok відео без водяного знака, фото-пости
(альбомом) і звук. Форк YtbVideoBot: та сама адмінка, розсилки, BotAds,
Premium за Telegram Stars, кеш через source-канали і NATS-флоу YT-API.

## Як працює

1. Користувач надсилає посилання на TikTok: `tiktok.com/@user/video|photo/<id>`
   або коротке `vm.tiktok.com/…`, `vt.tiktok.com/…`, `tiktok.com/t/…`
   (коротке бот розгортає сам, щоб знайти пост у кеші).
2. Якщо пост уже є в кеші (`tracks`, ключ `<post id>_<video|audio|photos>`) —
   копіюється одразу і не витрачає денний ліміт.
3. Інакше: денний ліміт безкоштовних завантажень → разове завантаження →
   paywall. Потім `DownloadRequest` і запит у YT-API
   (`manager_download.<BOT_NAME>`, `quality=video`). Воркер сам визначає,
   відео це чи фото-пост, і віддає найкращий H.264 або фото.
4. Uploader YT-API кладе файл (або альбом) у source-канал. Статус `completed`
   несе `site_id`, `media_type` і `source_message_ids`; бот копіює пост
   користувачу (альбом — одним `copyMessages`) і кешує його.
5. Кнопка «🎵 Звук» під результатом — запит `quality=audio`.

Групи не підтримуються: обробники працюють лише в приватних чатах.

## Бекенд

Окремий YT-API стек для TikTok на 65.21.205.145 (`/root/YT-API-tiktok`,
гілка `tiktok` репозиторію YT-API): власні NATS, менеджер, uploader і
воркери з TikTok-пробою. NATS слухає лише `127.0.0.1:4224`; для локального
запуску потрібен SSH-тунель:

```bash
ssh -N -L 14224:127.0.0.1:4224 root@65.21.205.145
```

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# заповнити BOT_TOKEN, BOT_NAME, ADMIN_IDS, NATS_TOKEN, ALLOWED_SOURCE_CHANNEL_IDS,
# AD_API_BASE_URL/AD_BOT_HASH/AD_CACHE_CHANNEL_ID (BotAdsLib)
.venv/bin/python -m app
```

Smoke-тест (локальний MongoDB, тимчасова база):

```bash
.venv/bin/python tests/smoke_tiktok.py
```

## Перед запуском

- Бот має бути адміном у source-каналах (`ALLOWED_SOURCE_CHANNEL_IDS`) і в
  каналі BotAds (`AD_CACHE_CHANNEL_ID`). З будь-якого іншого каналу він
  виходить одразу, тож спершу пропиши канал у `.env`, а потім додавай бота.
- Polling видаляє вебхук: не запускай локально з токеном бота, який працює
  на проді через webhook.
