"""Smoke test: TikTok links, cache, album/video delivery, quota and paywall.

Uses the local MongoDB with a throwaway database (dropped at the end); Telegram
and NATS are mocked. Run from the project root:
    .venv/bin/python tests/smoke_tiktok.py
"""
import asyncio
import datetime
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson

os.environ.setdefault("BOT_TOKEN", "123:dummy")
os.environ["FREE_DAILY_DOWNLOADS"] = "2"
os.environ["BOT_NAME"] = "tiktoksmoke"
sys.path.insert(0, os.getcwd())

from pymongo import AsyncMongoClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import DownloadRequest, Track, User, init_db  # noqa: E402
from app.handlers import user as h  # noqa: E402
from app.locales import localization  # noqa: E402
from app.services import media_cache, nats_meta  # noqa: E402
from app.utils import tiktok  # noqa: E402

DB_NAME = "TikTokBot_smoke"
VIDEO_POST = "7694335322922044703"
PHOTO_POST = "7606831942381718797"
CHANNEL = -1001234567890
_ = lambda path, **kw: localization.get_text("uk", path, **kw)  # noqa: E731
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label)
    ok = ok and cond


class FakeBot:
    def __init__(self):
        self.calls = []
        self.fail_copy = False

    async def me(self):
        return SimpleNamespace(username="tiktoksmoke_bot")

    async def copy_message(self, **kw):
        if self.fail_copy:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=None, message="message to copy not found")
        self.calls.append(("copy_message", kw))

    async def copy_messages(self, **kw):
        self.calls.append(("copy_messages", kw))

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", {"chat_id": chat_id, "text": text, **kw}))
        return FakeMessage(self, message_id=500 + len(self.calls))

    async def edit_message_text(self, **kw):
        self.calls.append(("edit_message_text", kw))


class FakeMessage:
    def __init__(self, bot, message_id=100):
        self.bot = bot
        self.message_id = message_id
        self.edits = []
        self.answers = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def delete(self):
        self.deleted = True


def names(bot):
    return [name for name, _kw in bot.calls]


async def test_links():
    check(tiktok.find_tiktok_url("дивись https://www.tiktok.com/@khaby.lame/video/7694335322922044703?is_from_webapp=1 круто")
          == "https://www.tiktok.com/@khaby.lame/video/7694335322922044703", "full link found, query dropped")
    check(tiktok.extract_post_id("https://www.tiktok.com/@strawbvrriezz/photo/7606831942381718797") == PHOTO_POST, "photo link id")
    check(tiktok.find_tiktok_url("https://vm.tiktok.com/ZMabc123/") == "https://vm.tiktok.com/ZMabc123/", "vm short link found")
    check(tiktok.find_tiktok_url("https://www.tiktok.com/t/ZTabc/") == "https://www.tiktok.com/t/ZTabc/", "/t/ short link found")
    check(tiktok.find_tiktok_url("https://www.tiktok.com/@khaby.lame") is None, "profile link rejected")
    check(tiktok.find_tiktok_url("https://youtu.be/dQw4w9WgXcQ") is None, "youtube link rejected")
    check(await tiktok.resolve_post_id("https://www.tiktok.com/@x/video/7694335322922044703") == VIDEO_POST, "full link resolves without HTTP")
    check(tiktok.canonical_url(VIDEO_POST) == "https://www.tiktok.com/@/video/7694335322922044703", "canonical URL")


async def test_cache():
    bot = FakeBot()
    await media_cache.save_cached(post_id=PHOTO_POST, quality="photos", source_chat_id=CHANNEL, message_ids=[11, 12, 13])
    cached = await media_cache.find_cached(PHOTO_POST, ("video", "photos"))
    check(cached is not None and cached.quality == "photos" and cached.message_ids == (11, 12, 13), "photo post cached under photos")
    sent = await media_cache.try_send_cached(bot, user_id=7, post_id=PHOTO_POST, qualities=("video", "photos"))
    check(sent == "photos" and names(bot) == ["copy_messages"], "album from cache: one copy_messages, no follow-up message")
    check(bot.calls[0][1]["message_ids"] == [11, 12, 13] and bot.calls[0][1]["remove_caption"], "album ids in order, tracking captions removed")

    bot = FakeBot()
    bot.fail_copy = True
    await media_cache.save_cached(post_id=VIDEO_POST, quality="video", source_chat_id=CHANNEL, message_ids=[21])
    sent = await media_cache.try_send_cached(bot, user_id=7, post_id=VIDEO_POST, qualities=("video", "photos"))
    check(sent is None and await media_cache.find_cached(VIDEO_POST, ("video",)) is None, "dead pointer dropped, falls through to download")


async def _status_handler(bot):
    callbacks = {}

    class FakeClient:
        async def subscribe(self, subject, cb):
            callbacks[subject] = cb
            return SimpleNamespace(unsubscribe=AsyncMock())

        async def flush(self):
            pass

    await nats_meta.subscribe_download_status(FakeClient(), bot)
    handler = callbacks[f"download_status.{settings.bot_name}"]

    async def send(payload):
        await handler(SimpleNamespace(data=orjson.dumps({"bot_name": settings.bot_name, **payload})))

    return send


async def test_album_delivery():
    bot = FakeBot()
    send = await _status_handler(bot)
    request = DownloadRequest(url="https://vm.tiktok.com/ZMabc123/", quality="video", user_id=8, message_id=300, premium=True)
    await request.insert()
    base = {"request_id": request.request_id, "user_id": 8, "message_id": 300}
    await send({**base, "status": "uploading_to_telegram", "terminal": False})
    await send({
        **base, "status": "completed", "terminal": True, "site_id": PHOTO_POST, "media_type": "photos",
        "source_chat_id": CHANNEL, "source_message_ids": [31, 32, 33, 34],
    })
    stored = await DownloadRequest.get(request.id)
    check(stored.media_sent and stored.site_id == PHOTO_POST and stored.source_message_ids == [31, 32, 33, 34], "album request delivered, site_id from status")
    copies = [kw for name, kw in bot.calls if name == "copy_messages"]
    check(len(copies) == 1 and copies[0]["message_ids"] == [31, 32, 33, 34], "album copied once with copy_messages")
    cached = await media_cache.find_cached(PHOTO_POST, ("photos",))
    check(cached is not None and cached.message_ids == (31, 32, 33, 34), "album cached under the resolved post id")


async def test_video_delivery():
    bot = FakeBot()
    send = await _status_handler(bot)
    request = DownloadRequest(url=tiktok.canonical_url(VIDEO_POST), quality="video", site_id=VIDEO_POST, user_id=9, message_id=301, premium=True)
    await request.insert()
    await send({
        "request_id": request.request_id, "user_id": 9, "message_id": 301, "status": "completed", "terminal": True,
        "site_id": VIDEO_POST, "media_type": "video", "source_chat_id": CHANNEL, "source_message_ids": [41], "source_message_id": 41,
    })
    copies = [kw for name, kw in bot.calls if name == "copy_message"]
    check(len(copies) == 1 and copies[0]["message_id"] == 41 and copies[0]["caption"] == "@tiktoksmoke_bot", "video copied with bot caption")
    check("reply_markup" not in copies[0], "video has no sound button")

    audio = DownloadRequest(url=tiktok.canonical_url(VIDEO_POST), quality="audio", site_id=VIDEO_POST, user_id=9, message_id=302, premium=True)
    await audio.insert()
    await send({
        "request_id": audio.request_id, "user_id": 9, "message_id": 302, "status": "completed", "terminal": True,
        "site_id": VIDEO_POST, "media_type": "audio", "source_chat_id": CHANNEL, "source_message_ids": [42], "source_message_id": 42,
    })
    copies = [kw for name, kw in bot.calls if name == "copy_message"]
    check(len(copies) == 2 and copies[1]["message_id"] == 42, "audio delivered (old sound button still works)")
    check((await media_cache.find_cached(VIDEO_POST, ("audio",))) is not None, "audio cached separately")

    failed = DownloadRequest(url=tiktok.canonical_url(VIDEO_POST), quality="audio", user_id=9, message_id=303, user_language="uk")
    await failed.insert()
    await send({"request_id": failed.request_id, "user_id": 9, "message_id": 303, "status": "failed", "terminal": True,
                "error_code": "409", "error_message": "This TikTok post has no sound"})
    edits = [kw for name, kw in bot.calls if name == "edit_message_text"]
    check(edits and _("download_status/error_no_sound_mt").strip() in edits[-1]["text"], "no-sound failure gets its own text")


async def test_request_flow():
    user = User(_id=55, language="uk")
    await user.save()
    bot = FakeBot()

    # Cache hit: no quota spent, status deleted, request logged as cache_hit.
    await media_cache.save_cached(post_id=VIDEO_POST, quality="video", source_chat_id=CHANNEL, message_ids=[51])
    status = FakeMessage(bot, 600)
    download = AsyncMock(return_value={"result": "ok", "status": "start"})
    with patch.object(h, "request_media_download", download), patch.object(h, "send_post_delivery_advert", AsyncMock()):
        await h._process_request(status=status, nats_client=None, db_user=user, user_id=55, url=tiktok.canonical_url(VIDEO_POST),
                                 post_id=VIDEO_POST, quality="video", language="uk", _=_)
    hit = await DownloadRequest.find_one(DownloadRequest.user_id == 55, DownloadRequest.status == "cache_hit")
    check(status.deleted and hit is not None and not download.called, "cache hit served without a download")

    # Two free downloads, then the paywall.
    for index in range(2):
        status = FakeMessage(bot, 610 + index)
        with patch.object(h, "request_media_download", download):
            await h._process_request(status=status, nats_client=None, db_user=user, user_id=55,
                                     url=f"https://www.tiktok.com/@a/video/70000000000000000{index}", post_id=f"70000000000000000{index}",
                                     quality="video", language="uk", _=_)
    check(download.call_count == 2, "two free downloads requested")
    counted = await DownloadRequest.find(DownloadRequest.user_id == 55, DownloadRequest.counts_to_quota == True).count()  # noqa: E712
    check(counted == 2, "free downloads count toward the quota")
    status = FakeMessage(bot, 620)
    with patch.object(h, "request_media_download", download), \
         patch.object(h.premium, "build_buy_keyboard", AsyncMock(return_value=None)) as buy:
        await h._process_request(status=status, nats_client=None, db_user=user, user_id=55,
                                 url="https://www.tiktok.com/@a/video/7000000000000000099", post_id="7000000000000000099",
                                 quality="video", language="uk", _=_)
    check(download.call_count == 2 and status.edits and "ліміт" in status.edits[-1][0], "third download hits the paywall")
    check(buy.call_args.kwargs["single"] == ("video", "7000000000000000099"), "paywall offers the single purchase for this post")

    # A cache hit still works with the quota used up.
    status = FakeMessage(bot, 630)
    with patch.object(h, "send_post_delivery_advert", AsyncMock()):
        await h._process_request(status=status, nats_client=None, db_user=user, user_id=55, url=tiktok.canonical_url(VIDEO_POST),
                                 post_id=VIDEO_POST, quality="video", language="uk", _=_)
    check(status.deleted, "cache hit allowed after the quota is used up")

    # Double tap guard.
    h._requests_in_progress.add((55, "7000000000000000100", "audio"))
    status = FakeMessage(bot, 640)
    await h._process_request(status=status, nats_client=None, db_user=user, user_id=55, url="x", post_id="7000000000000000100",
                             quality="audio", language="uk", _=_)
    check(status.edits and status.edits[-1][0] == _("download/already_requested_mt"), "second identical request is refused")
    h._requests_in_progress.clear()


async def main():
    await init_db(settings.db_url, DB_NAME)
    try:
        await test_links()
        await test_cache()
        await test_album_delivery()
        await test_video_delivery()
        await test_request_flow()
    finally:
        await AsyncMongoClient(settings.db_url).drop_database(DB_NAME)
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
