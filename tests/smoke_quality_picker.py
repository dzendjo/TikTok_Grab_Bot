"""Smoke test: quality options (meta/cache/2 GB/60 fps) + link handler picker with mocked meta/cache.

Run from the project root: .venv/bin/python tests/smoke_quality_picker.py
"""
import asyncio, os, sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:dummy")
sys.path.insert(0, os.getcwd())

from aiogram.types import Chat, Message  # noqa: E402
from app.handlers import user as h  # noqa: E402
from app.locales import localization  # noqa: E402
from app.services.quality_options import build_quality_options, format_size  # noqa: E402

_ = lambda path, **kw: localization.get_text("uk", path, **kw)  # noqa: E731
MB = 1024 * 1024
ok = True
def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label); ok = ok and cond

def v(fid, size, approx=False):
    return {"format_id": fid, "file_size": 0 if approx else size, "file_size_approx": size if approx else 0}

RICK = {"result": "ok", "title": "Rick <Astley>", "duration": 213, "download_urls": [
    v("140", 3.4 * MB), v("134", 8.4 * MB), v("135", 14.1 * MB), v("136", 26.5 * MB), v("137", 80.9 * MB)]}
ZOO = {"result": "ok", "title": "zoo", "duration": 19, "download_urls": [v("140-drc", 0.3 * MB), v("140", 0.3 * MB), v("134", 0.3 * MB)]}
LONG = {"result": "ok", "title": "long", "duration": 36000, "download_urls": [
    v("140", 300 * MB), v("134", 600 * MB), v("135", 1100 * MB), v("136", 1900 * MB), v("137", 4000 * MB, approx=True)]}
SHORT60 = {"result": "ok", "title": "short", "duration": 32, "download_urls": [
    v("140", 0.5 * MB), v("134", 2.1 * MB), v("135", 3.3 * MB), v("298", 13.8 * MB), v("299", 22.5 * MB)]}
BOTH = {"result": "ok", "title": "both", "download_urls": [v("140", MB), v("136", 5 * MB), v("298", 7 * MB), v("137", 9 * MB)]}
NO_VIDEO_ITAGS = {"result": "ok", "title": "x", "download_urls": [v("140", MB)]}

q = lambda opts: [(o.quality, round(o.size_bytes / MB, 1) if o.size_bytes else None, o.cached) for o in opts]
check(q(build_quality_options(RICK, {})) == [("360p", 11.8, False), ("480p", 17.5, False), ("720p", 29.9, False), ("1080p", 84.3, False), ("audio", 3.4, False)], f"rick: all 5, video+audio sizes {q(build_quality_options(RICK, {}))}")
check([o.quality for o in build_quality_options(ZOO, {})] == ["360p", "audio"], "zoo: only formats YouTube lists")
check([o.quality for o in build_quality_options(LONG, {})] == ["360p", "480p", "audio"], f"long: >2GB hidden (720p=2.2GB, 1080p) {q(build_quality_options(LONG, {}))}")
check([o.quality for o in build_quality_options(None, {})] == ["360p", "480p", "720p", "audio"], "no meta: classic set, no 1080p")
check([o.quality for o in build_quality_options({"result": 0, "error": "x"}, {"1080p": 5 * MB})] == ["360p", "480p", "720p", "1080p", "audio"], "meta failed + cached 1080p shown")
check([o.quality for o in build_quality_options(NO_VIDEO_ITAGS, {})] == ["360p", "480p", "720p", "audio"], "meta without video itags: classic set")
opts = build_quality_options(SHORT60, {})
check([(o.quality, o.label) for o in opts] == [("360p", "360p"), ("480p", "480p"), ("720p", "720p60"), ("1080p", "1080p60"), ("audio", "audio")], f"short 60fps: 720p60/1080p60 {[(o.quality, o.label) for o in opts]}")
check(round(opts[3].size_bytes / MB, 1) == 23.0, "1080p60 size = 299 + 140")
opts = build_quality_options(BOTH, {})
check([(o.quality, o.label, round(o.size_bytes / MB)) for o in opts] == [("720p", "720p60", 8), ("1080p", "1080p", 10), ("audio", "audio", 1)], f"60fps preferred like worker {[(o.quality, o.label) for o in opts]}")
cached = build_quality_options(RICK, {"720p": 30 * MB})
check([(o.quality, o.cached) for o in cached][2] == ("720p", True) and cached[2].size_bytes == 30 * MB, "cached 720p: icon + cached size")
check([format_size(x) for x in (500 * 1024, 26.5 * MB, 1300 * MB)] == ["500 KB", "26.5 MB", "1.27 GB"], "format_size")


class FakeMessage(Message):
    @property
    def bot(self): return SimpleNamespace()
    async def reply(self, text, **kw):
        self.__dict__["calls"].append(("reply", text, kw))
        picker = make_msg(message_id=99); self.__dict__["picker"] = picker; return picker
    async def answer(self, text, **kw): self.__dict__["calls"].append(("answer", text, kw))
    async def edit_text(self, text, **kw): self.__dict__["calls"].append(("edit_text", text, kw)); return True

def make_msg(message_id=10, text=None):
    m = FakeMessage.model_construct(message_id=message_id, date=0, text=text,
        chat=Chat.model_construct(id=555, type="private"), from_user=SimpleNamespace(id=555))
    m.__dict__["calls"] = []
    return m

async def run_link(meta, cached, cached_meta=None):
    msg = make_msg(text="https://youtu.be/dQw4w9WgXcQ?si=x")
    with patch.object(h.meta_cache, "get_cached_meta", AsyncMock(return_value=cached_meta)), \
         patch.object(h.meta_cache, "save_meta", AsyncMock()), \
         patch.object(h, "_get_or_create_db_user", AsyncMock(return_value=SimpleNamespace())), \
         patch.object(h, "_get_rate_limit_text", AsyncMock(return_value=None)), \
         patch.object(h.premium, "get_access", lambda user: h.FULL_ACCESS), \
         patch.object(h, "request_social_meta", AsyncMock(return_value=meta) if not isinstance(meta, Exception) else AsyncMock(side_effect=meta)) as meta_mock, \
         patch.object(h, "get_cached_sizes", AsyncMock(return_value=cached)):
        await h.youtube_link_handler(msg, nats_client=None, language="uk", _=_, user=None)
    return msg, meta_mock

async def main():
    msg, meta_mock = await run_link(RICK, {"720p": 30 * MB})
    picker = msg.__dict__["picker"]
    check(msg.calls[0][0] == "reply" and "метадані" in msg.calls[0][1], "placeholder reply first")
    check(meta_mock.await_args.args[0] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "meta uses canonical URL")
    kind, text, kw = picker.calls[0]
    rows = kw["reply_markup"].inline_keyboard
    labels = [r[0].text for r in rows]
    print("   picker text:", text.replace("\n", " | "))
    print("   buttons:", labels)
    check(all(len(r) == 1 for r in rows) and len(rows) == 6, "6 rows (5 qualities + transcript), one button each")
    check(labels[5] == "📝 Текст відео" and rows[5][0].callback_data == "tx:dQw4w9WgXcQ", "transcript button last")
    check(labels[2] == "⚡ 720p · 30.0 MB" and labels[0] == "📥 360p · 11.8 MB" and labels[4] == "📥 Аудіо · 3.4 MB", "icons + sizes")
    check("Rick &lt;Astley&gt;" in text and "3:33" in text, "title escaped + duration")
    check([r[0].callback_data for r in rows][3] == "dl:1080p:dQw4w9WgXcQ", "1080p callback data")

    msg, _m = await run_link(TimeoutError(), {})
    labels = [r[0].text for r in msg.__dict__["picker"].calls[0][2]["reply_markup"].inline_keyboard]
    check(labels == ["📥 360p", "📥 480p", "📥 720p", "📥 Аудіо", "📝 Текст відео"], f"meta timeout -> fallback picker {labels}")

    msg, _m = await run_link({"result": 0, "error_code": 408, "error": "live"}, {})
    kind, text, kw = msg.__dict__["picker"].calls[0]
    check("reply_markup" not in kw and "❌" in text, f"live stream -> error instead of picker: {text!r}")

    # cached meta: picker sent directly as the reply, no worker request
    msg, meta_mock = await run_link(None, {}, cached_meta=RICK)
    kind, text, kw = msg.calls[0]
    labels = [r[0].text for r in kw["reply_markup"].inline_keyboard]
    check(kind == "reply" and "Rick" in text and len(msg.calls) == 1, "cached meta -> picker is the first reply")
    check(not meta_mock.await_count and labels[3] == "📥 1080p · 84.3 MB", f"cached meta -> no meta request, sizes kept {labels}")

    # quality callback accepts 1080p
    check(h._parse_quality_callback("dl:1080p:dQw4w9WgXcQ") == ("1080p", "dQw4w9WgXcQ", False), "1080p callback parses")

    print("\nALL PASS" if ok else "\nSOME FAILED"); sys.exit(0 if ok else 1)

asyncio.run(main())
