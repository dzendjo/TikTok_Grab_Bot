"""Smoke test: quality callback flow (cache hit/miss, double tap, bad data) with mocked DB/NATS.

Run from the project root: .venv/bin/python tests/smoke_quality_callback.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:dummy")
sys.path.insert(0, os.getcwd())

from aiogram.types import Chat, Message  # noqa: E402

from app.handlers import user as h  # noqa: E402
from app.locales import localization  # noqa: E402
from app.services import media_cache, nats_meta  # noqa: E402

_ = lambda path, **kw: localization.get_text("uk", path, **kw)  # noqa: E731
FAKE_BOT = SimpleNamespace(name="bot")


class FakeMessage(Message):
    @property
    def bot(self):
        return FAKE_BOT

    async def reply(self, text, **kw):
        self.__dict__.setdefault("calls", []).append(("reply", text, kw))

    async def answer(self, text, **kw):
        self.__dict__.setdefault("calls", []).append(("answer", text, kw))

    async def edit_text(self, text, **kw):
        self.__dict__.setdefault("calls", []).append(("edit_text", text, kw))
        return True

    async def delete(self, **kw):
        self.__dict__.setdefault("calls", []).append(("delete",))
        return True


def make_msg(message_id=10, text=None):
    m = FakeMessage.model_construct(
        message_id=message_id, date=0, text=text,
        chat=Chat.model_construct(id=555, type="private"),
        from_user=SimpleNamespace(id=555),
    )
    m.__dict__["calls"] = []
    return m


class FakeRequest:
    inserted = []

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id = f"rid{len(FakeRequest.inserted)}"
        self.nats_ack = None
        self.terminal = False

    @property
    def request_id(self):
        return self.id

    async def insert(self):
        FakeRequest.inserted.append(self)

    async def save(self):
        pass

    # class-level query helpers used by handler
    user_id = "user_id"
    message_id = "message_id"
    find_one = AsyncMock(return_value=None)

    @classmethod
    def get_pymongo_collection(cls):
        class _Collection:
            async def update_one(self, flt, update):
                for doc in FakeRequest.inserted:
                    if doc.id == flt["_id"] and all(getattr(doc, k, None) == v for k, v in flt.items() if k != "_id"):
                        doc.__dict__.update(update["$set"])
        return _Collection()


class Cb:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=555)
        self.answers = []

    async def answer(self, text=None, **kw):
        await asyncio.sleep(0)
        self.answers.append((text, kw))


async def main():
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        ok = ok and cond

    common = [
        patch.object(h, "_get_or_create_db_user", AsyncMock(return_value=SimpleNamespace())),
        patch.object(h, "_get_rate_limit_text", AsyncMock(return_value=None)),
        patch.object(h, "DownloadRequest", FakeRequest),
        patch.object(h, "send_post_delivery_advert", AsyncMock()),
        patch.object(h.premium, "get_access", lambda user: h.FULL_ACCESS),
    ]
    for p in common:
        p.start()

    # 3. cache miss -> download requested with quality
    dl = AsyncMock(return_value={"status": "queued"})
    with patch.object(h, "try_send_cached_media_for_url", AsyncMock(return_value=False)), \
         patch.object(h, "request_media_download", dl):
        picker = make_msg(message_id=77)
        cb = Cb("dl:720p:dQw4w9WgXcQ", picker)
        await h.quality_selected_handler(cb, nats_client=None, language="uk", _=_, user=None)
        req = FakeRequest.inserted[-1]
        check(req.quality == "720p" and req.message_id == 77, "DownloadRequest has quality=720p, message_id=picker")
        check(req.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "canonical URL")
        check(dl.await_args.kwargs["quality"] == "720p" and dl.await_args.kwargs["message_id"] == 77, "NATS called with 720p + picker id")
        check(picker.calls[0][0] == "edit_text" and "reply_markup" not in picker.calls[0][2], "picker edited to status (keyboard removed)")
        check(req.status == "queued", "status from ACK saved")

    # 4. cache hit -> picker deleted, no NATS
    dl = AsyncMock()
    with patch.object(h, "try_send_cached_media_for_url", AsyncMock(return_value=True)) as cache, \
         patch.object(h, "request_media_download", dl):
        picker = make_msg(message_id=78)
        await h.quality_selected_handler(Cb("dl:audio:dQw4w9WgXcQ", picker), nats_client=None, language="uk", _=_, user=None)
        check(cache.await_args.kwargs["quality"] == "audio", "cache lookup uses selected quality")
        check(("delete",) in picker.calls and not dl.await_count, "cache hit deletes picker, no NATS")
        check(FakeRequest.inserted[-1].status == "cache_hit" and FakeRequest.inserted[-1].quality == "audio", "cache_hit saved with quality")

    # 5. concurrent double tap -> one download
    dl = AsyncMock(return_value={"status": "queued"})
    with patch.object(h, "try_send_cached_media_for_url", AsyncMock(return_value=False)), \
         patch.object(h, "request_media_download", dl):
        picker = make_msg(message_id=79)
        cb1, cb2 = Cb("dl:480p:dQw4w9WgXcQ", picker), Cb("dl:480p:dQw4w9WgXcQ", picker)
        await asyncio.gather(
            h.quality_selected_handler(cb1, nats_client=None, language="uk", _=_, user=None),
            h.quality_selected_handler(cb2, nats_client=None, language="uk", _=_, user=None),
        )
        check(dl.await_count == 1, f"double tap -> 1 download (got {dl.await_count})")
        check(not h._pickers_in_progress, "in-progress set cleaned up")

    # 6. tap after request already exists in DB
    FakeRequest.find_one = AsyncMock(return_value=object())
    dl = AsyncMock()
    with patch.object(h, "request_media_download", dl):
        cb = Cb("dl:360p:dQw4w9WgXcQ", make_msg(message_id=80))
        await h.quality_selected_handler(cb, nats_client=None, language="uk", _=_, user=None)
        check(not dl.await_count and cb.answers[0][0] == "⏳ Запит уже обробляється", "existing request -> no new download")
    FakeRequest.find_one = AsyncMock(return_value=None)

    # 7. bad callback / inaccessible message
    cb = Cb("dl:2160p:dQw4w9WgXcQ", make_msg())
    await h.quality_selected_handler(cb, nats_client=None, language="uk", _=_, user=None)
    check(cb.answers[0][1].get("show_alert"), "unknown quality -> alert")
    cb = Cb("dl:360p:dQw4w9WgXcQ", None)
    await h.quality_selected_handler(cb, nats_client=None, language="uk", _=_, user=None)
    check(cb.answers[0][1].get("show_alert"), "inaccessible message -> alert")

    # 8. cache item picking: video strict, audio lenient
    items = [{"format_id": "134", "label": "video"}, {"format_id": "140", "label": "audio"}]
    pick = media_cache._pick_download_item_for_quality
    check(pick(items, "136", "720p") is None, "720p does not fall back to 360p")
    check(pick(items, "134", "360p")["format_id"] == "134", "360p exact hit")
    check(pick([{"format_id": "x", "label": "audio"}], "140", "audio")["label"] == "audio", "audio keeps legacy fallback")
    check(media_cache.quality_to_format_id("video_high") == media_cache.quality_to_format_id("720p") == "136", "uploader caption q=video_high maps to 720p cache")

    # 9. status text
    text = nats_meta._build_download_status_text({"status": "downloading", "progress": 30, "user_language": "uk"}, quality="480p")
    check("Завантажую відео 480p (30%)" in text, "status shows video quality")
    check("Завантажую в Telegram (" not in text, "no upload % while downloading")
    text = nats_meta._build_download_status_text({"status": "uploading_to_telegram", "progress": 40, "user_language": "uk"}, quality="720p")
    check("⏳ Завантажую в Telegram (40%)" in text and "✅ Завантажую відео 720p\n" in text, "upload % on the upload step only")
    text = nats_meta._build_download_status_text({"status": "uploading_to_telegram", "user_language": "en"}, quality="720p")
    check("⏳ Uploading to Telegram\n" in text + "\n", "no % before the first upload tick")

    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
