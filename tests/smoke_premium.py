"""Smoke test: premium access, quota, credits, payments, paywall, transcript.

Uses the local MongoDB with a throwaway database (dropped at the end); Telegram
and NATS are mocked. Run from the project root:
    .venv/bin/python tests/smoke_premium.py
"""
import asyncio
import datetime
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:dummy")
# The checks below assume the paid-tier defaults, whatever .env says.
os.environ["FREE_DAILY_DOWNLOADS"] = "3"
os.environ["FREE_MAX_FILE_MB"] = "500"
sys.path.insert(0, os.getcwd())

from aiogram.types import Chat, Message, SuccessfulPayment  # noqa: E402
from pymongo import AsyncMongoClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import DownloadRequest, Payment, User, init_db  # noqa: E402
from app.handlers import payments as pay  # noqa: E402
from app.handlers import user as h  # noqa: E402
from app.locales import localization  # noqa: E402
from app.services import nats_meta, premium  # noqa: E402
from app.services.broadcast import BroadcastService  # noqa: E402

DB_NAME = "YtbVideoBot_smoke_premium"
UTC = datetime.UTC
VID = "dQw4w9WgXcQ"
_ = lambda path, **kw: localization.get_text("uk", path, **kw)  # noqa: E731
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label)
    ok = ok and cond


def near(a, b, seconds=5):
    return a is not None and abs((premium.utc(a) - b).total_seconds()) < seconds


class FakeBot:
    def __init__(self):
        self.sent = []
        self.invoices = []
        self.id = 1

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return make_msg(message_id=1000 + len(self.sent), bot=self)

    async def create_invoice_link(self, **kw):
        self.invoices.append(kw)
        return f"https://t.me/$inv{len(self.invoices)}"

    async def me(self):
        return SimpleNamespace(username="Dwloadbot")


class FakeMessage(Message):
    @property
    def bot(self):
        return self.__dict__["_bot"]

    def _log(self, *item):
        self.__dict__["calls"].append(item)

    async def reply(self, text, **kw):
        self._log("reply", text, kw)
        return make_msg(message_id=self.message_id + 500, bot=self.bot)

    async def answer(self, text, **kw):
        self._log("answer", text, kw)
        return make_msg(message_id=self.message_id + 600, bot=self.bot)

    async def edit_text(self, text, **kw):
        self._log("edit_text", text, kw)
        return True

    async def delete(self, **kw):
        self._log("delete")
        return True

    async def answer_document(self, document, **kw):
        self._log("document", document, kw)
        return True


def make_msg(message_id=10, text=None, bot=None, user_id=555, successful_payment=None):
    m = FakeMessage.model_construct(
        message_id=message_id, date=0, text=text,
        chat=Chat.model_construct(id=user_id, type="private"),
        from_user=SimpleNamespace(id=user_id),
        successful_payment=successful_payment,
    )
    m.__dict__["calls"] = []
    m.__dict__["_bot"] = bot or FakeBot()
    return m


class Cb:
    def __init__(self, data, message, user_id=555):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = message.bot
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append((text, kw))


def texts(msg):
    return [c[1] for c in msg.calls if c[0] in ("reply", "answer", "edit_text")]


async def fresh_user(user_id, **fields):
    await User.find_one(User.id == user_id).delete()
    user = User(_id=user_id, language="uk", **fields)
    await user.insert()
    return user


async def main():
    client = AsyncMongoClient(settings.db_url)
    await client.drop_database(DB_NAME)
    await init_db(settings.db_url, DB_NAME)
    now = datetime.datetime.now(UTC)

    # --- atomic premium updates -------------------------------------------------
    u = await fresh_user(1)
    until = await premium.grant_premium_days(1, 30)
    check(near(until, now + datetime.timedelta(days=30)), "grant 30 days from now")
    until = await premium.grant_premium_days(1, 90)
    check(near(until, now + datetime.timedelta(days=120)), "grant stacks: 30 + 90 days")
    check((await User.get(1)).is_premium, "is_premium after grant")
    until = await premium.shift_premium_days(1, -90)
    check(near(until, now + datetime.timedelta(days=30)), "shift -90 days (refund)")
    # a stale snapshot written with set() must not roll premium back
    stale = await User.get(1)
    await premium.grant_premium_days(1, 10)
    await stale.set({User.visited: datetime.datetime.now(UTC)})
    check(near((await User.get(1)).premium_until, now + datetime.timedelta(days=40)), "stale snapshot set() keeps premium_until")
    await premium.revoke_premium(1)
    check(not (await User.get(1)).is_premium, "revoke")
    await fresh_user(2, premium_until=now - datetime.timedelta(days=3))
    until = await premium.grant_premium_days(2, 30)
    check(near(until, now + datetime.timedelta(days=30)), "expired premium: grant counts from now")

    # --- credits -------------------------------------------------------------------
    await fresh_user(3)
    check(await premium.consume_credit(3) is None, "no credit to consume")
    check(await premium.add_credits(3, 2) == 2, "add 2 credits")
    check(await premium.consume_credit(3) == 1, "consume -> 1 left")
    check(await premium.add_credits(3, -5) == 0, "remove floors at 0")

    # --- quota -----------------------------------------------------------------------
    await fresh_user(4)
    old = now - datetime.timedelta(hours=20)
    for status, counts, created in [
        ("completed", True, old),
        ("failed", True, now),
        ("cache_hit", False, now),
        ("completed", True, now - datetime.timedelta(hours=25)),
        ("queued", True, now),
    ]:
        await DownloadRequest(url="u", user_id=4, status=status, counts_to_quota=counts, created_at=created).insert()
    quota = await premium.get_quota_status(4)
    check(quota.used == 2 and quota.left == 1 and not quota.exhausted, f"quota counts only free non-failed in 24 h (used={quota.used})")
    check(near(quota.resets_at, old + datetime.timedelta(hours=24)), "quota resets when oldest leaves the window")

    # --- credit return is idempotent ------------------------------------------------
    await fresh_user(5)
    req = DownloadRequest(url="u", user_id=5, status="failed", terminal=True, paid_credit=True)
    await req.insert()
    bot = FakeBot()
    await nats_meta._return_credit_and_notify(bot, req, "uk")
    await nats_meta._return_credit_and_notify(bot, await DownloadRequest.get(req.id), "uk")
    check((await User.get(5)).single_credits == 1 and len(bot.sent) == 1, "failed paid request returns the credit once")

    # --- meta cache ---------------------------------------------------------------------
    from app.database import VideoMeta
    from app.services import meta_cache
    import orjson
    old_meta = {"result": "ok", "title": "Old", "duration": 213, "upload_date": "25.10.2009",
                "download_urls": [{"format_id": "137", "file_size": 80, "download_link": "https://expiring"}, {"format_id": "140", "file_size_approx": 3}]}
    await meta_cache.save_meta("oldVideo01", old_meta)
    doc = await VideoMeta.get("oldVideo01")
    check(doc.expires_at is None and doc.formats == [{"format_id": "137", "file_size": 80, "file_size_approx": 0}, {"format_id": "140", "file_size": 0, "file_size_approx": 3}], "old video kept forever, links dropped")
    got = await meta_cache.get_cached_meta("oldVideo01")
    check(got["result"] == "ok" and got["title"] == "Old" and got["duration"] == 213 and len(got["download_urls"]) == 2, "cached meta in YT-API shape")
    today = now.strftime("%d.%m.%Y")
    await meta_cache.save_meta("newVideo01", {**old_meta, "upload_date": today})
    check(near((await VideoMeta.get("newVideo01")).expires_at, now + datetime.timedelta(hours=4)), "video < 2 days old expires in 4 h")
    await meta_cache.save_meta("noDate0001", {**old_meta, "upload_date": ""})
    check((await VideoMeta.get("noDate0001")).expires_at is not None, "unknown upload date treated as fresh")
    await meta_cache.save_meta("liveVideo1", {"result": 0, "error_code": 408})
    await meta_cache.save_meta("emptyVid01", {"result": "ok", "download_urls": []})
    check(await VideoMeta.get("liveVideo1") is None and await VideoMeta.get("emptyVid01") is None, "errors / empty meta not cached")
    await VideoMeta.get_pymongo_collection().update_one({"_id": "newVideo01"}, {"$set": {"expires_at": now - datetime.timedelta(minutes=1)}})
    check(await meta_cache.get_cached_meta("newVideo01") is None, "expired entry ignored before TTL sweep")
    indexes = await VideoMeta.get_pymongo_collection().index_information()
    check(indexes.get("expires_at_ttl", {}).get("expireAfterSeconds") == 0, "TTL index on expires_at")

    # failed download status drops the cached meta (real status handler, fake NATS/bot)
    class FakeNats:
        def __init__(self): self.cbs = {}
        async def subscribe(self, subject, cb): self.cbs[subject] = cb; return subject
        async def flush(self): pass
    class StatusBot(FakeBot):
        async def edit_message_text(self, **kw): self.sent.append(("edit", kw.get("text"), kw))
    fake_nats, status_bot = FakeNats(), StatusBot()
    await nats_meta.subscribe_download_status(fake_nats, status_bot)
    status_cb = fake_nats.cbs[f"download_status.{settings.bot_name}"]
    req = DownloadRequest(url="https://www.youtube.com/watch?v=oldVideo01", user_id=5, message_id=7, status="queued", quality="1080p")
    await req.insert()
    await status_cb(SimpleNamespace(data=orjson.dumps({"request_id": req.request_id, "status": "downloading", "progress": 10, "user_id": 5, "message_id": 7})))
    check(await VideoMeta.get("oldVideo01") is not None, "progress status keeps meta")
    await status_cb(SimpleNamespace(data=orjson.dumps({"request_id": req.request_id, "status": "failed", "terminal": True, "error_code": 302, "user_id": 5, "message_id": 7})))
    check(await VideoMeta.get("oldVideo01") is None, "failed download drops cached meta")

    # --- busy servers: priority retry -------------------------------------------------
    nats_meta.PRIORITY_RETRY_DELAY_SECONDS = 0.05
    await fresh_user(6)
    await meta_cache.save_meta("busyVideo1", old_meta)
    retry_dl = AsyncMock(return_value={"result": "ok", "status": "start"})
    retry_query = {"user_id": 6, "is_retry": True}

    def fail(req, code, message_id=70):
        return SimpleNamespace(data=orjson.dumps({"request_id": req.request_id, "status": "failed", "terminal": True,
                                                  "error_code": code, "user_id": 6, "message_id": message_id}))

    with patch.object(nats_meta, "request_media_download", retry_dl):
        first = DownloadRequest(url="https://www.youtube.com/watch?v=busyVideo1", user_id=6, message_id=70, status="queued",
                                quality="1080p", priority=True, premium=True, paid_credit=True, language="uk", user_language="uk")
        await first.insert()
        status_bot.sent.clear()
        await status_cb(fail(first, "301"))
        retry1 = await DownloadRequest.find_one(retry_query)
        check(retry1 and retry1.status == "retry_wait" and retry1.retry_attempt == 1 and retry1.paid_credit and retry1.busy_since, "busy 301 -> retry queued, credit moved")
        check(not (await DownloadRequest.get(first.id)).paid_credit and (await User.get(6)).single_credits == 0, "failed attempt doesn't return the credit")
        check(status_bot.sent and "сервери зараз зайняті" in status_bot.sent[-1][1] and "спроба 1" in status_bot.sent[-1][1], f"busy text shown {status_bot.sent[-1][1] if status_bot.sent else None!r}")
        check(await VideoMeta.get("busyVideo1") is not None, "busy retry keeps cached meta")
        await asyncio.sleep(0.2)
        check(retry_dl.await_count == 1 and retry_dl.await_args.kwargs["request_id"] == retry1.request_id and retry_dl.await_args.kwargs["message_id"] == 70, "retry dispatched with a fresh request id, same status message")
        check((await DownloadRequest.get(retry1.id)).status == "queued", "retry status queued after dispatch")

        # dispatch itself fails -> treated as busy -> retries keep coming until it succeeds
        retry_dl.side_effect = RuntimeError("nats down")
        await status_cb(fail(retry1, "404"))
        await asyncio.sleep(0.2)
        retry_dl.side_effect = None
        await asyncio.sleep(0.2)
        retries = await DownloadRequest.find(retry_query).sort("retry_attempt").to_list()
        attempts = [r.retry_attempt for r in retries]
        check(len(attempts) >= 3 and attempts == list(range(1, len(attempts) + 1)), f"dispatch failure re-schedules {attempts}")
        check(retries[-1].status == "queued" and retries[-1].paid_credit and sum(r.paid_credit for r in retries) == 1, "last retry dispatched, credit on it only")

        # window over -> usual failure text, credit back, meta dropped, no new retry
        last = retries[-1]
        await DownloadRequest.get_pymongo_collection().update_one({"_id": last.id}, {"$set": {"busy_since": now - datetime.timedelta(minutes=3)}})
        status_bot.sent.clear()
        await status_cb(fail(last, "301"))
        edits = [x for x in status_bot.sent if x[0] == "edit"]
        check(await DownloadRequest.find(retry_query).count() == len(retries), "after 2 min -> no new retry")
        check(bool(edits) and "перевантажені" in edits[-1][1], f"after 2 min -> usual failure text {edits[-1][1] if edits else None!r}")
        check((await User.get(6)).single_credits == 1 and await VideoMeta.get("busyVideo1") is None, "final failure returns credit, drops meta")

        # non-priority request and non-busy errors fail right away
        before = await DownloadRequest.find(retry_query).count()
        free_req = DownloadRequest(url="https://www.youtube.com/watch?v=busyVideo2", user_id=6, message_id=71, status="queued", quality="360p", counts_to_quota=True)
        await free_req.insert()
        await status_cb(fail(free_req, "301", 71))
        prio = DownloadRequest(url="https://www.youtube.com/watch?v=busyVideo3", user_id=6, message_id=72, status="queued", quality="720p", priority=True)
        await prio.insert()
        await status_cb(fail(prio, "302", 72))
        check(await DownloadRequest.find(retry_query).count() == before, "free request / code 302: no retry")

    # antispam ignores automatic retries and ⚡ cache hits
    await fresh_user(7)
    for i in range(25):
        await DownloadRequest(url="u", user_id=7, status="failed", is_retry=True).insert()
        await DownloadRequest(url="u", user_id=7, status="cache_hit").insert()
    check(await h._get_rate_limit_text(await User.get(7), 7, _) is None, "antispam ignores retries and cache hits")

    # free: 20 an hour -> 6 h block
    spam_now = datetime.datetime.now(UTC)
    await fresh_user(8)
    for i in range(20):
        await DownloadRequest(url="u", user_id=8, status="completed").insert()
    text = await h._get_rate_limit_text(await User.get(8), 8, _)
    check(bool(text) and "6 год" in text and near((await User.get(8)).rate_limited_until, spam_now + datetime.timedelta(hours=6)), f"free antispam blocks for 6 h {text!r}")

    # premium: 20 an hour and an old block are fine; at 100 wait until the oldest leaves the hour
    await fresh_user(9, premium_until=spam_now + datetime.timedelta(days=30), rate_limited_until=spam_now + datetime.timedelta(hours=5))
    for i in range(20):
        await DownloadRequest(url="u", user_id=9, status="completed").insert()
    check(await h._get_rate_limit_text(await User.get(9), 9, _) is None, "premium: 20 an hour and an old free block don't stop it")
    await DownloadRequest(url="u", user_id=9, status="completed", created_at=spam_now - datetime.timedelta(minutes=50)).insert()
    for i in range(78):
        await DownloadRequest(url="u", user_id=9, status="completed").insert()
    check(await h._get_rate_limit_text(await User.get(9), 9, _) is None, "premium: 99 an hour is fine")
    await DownloadRequest(url="u", user_id=9, status="completed").insert()
    text = await h._get_rate_limit_text(await User.get(9), 9, _)
    check(text == _("download/rate_limited_minutes_mt", retry_minutes=10), f"premium: 100 an hour -> wait 10 min, no block {text!r}")

    # ACK must not overwrite a fast failure that already landed
    fast = DownloadRequest(url="u", user_id=7, status="failed", terminal=True)
    await fast.insert()
    await h._apply_download_ack(fast, {"status": "start"})
    got = await DownloadRequest.get(fast.id)
    check(got.status == "failed" and got.terminal and got.nats_ack == {"status": "start"}, "late ACK keeps failed status")
    slow = DownloadRequest(url="u", user_id=7, status="queued")
    await slow.insert()
    await h._apply_download_ack(slow, {"status": "start"})
    check((await DownloadRequest.get(slow.id)).status == "start", "ACK applied while queued")

    # --- payloads ---------------------------------------------------------------------
    check(premium.parse_payload("month").product == "month", "payload month")
    p = premium.parse_payload(f"one:1080p:{VID}")
    check((p.product, p.action, p.video_id) == ("single", "1080p", VID), "payload single")
    check(premium.parse_payload(f"one:transcript:{VID}").action == "transcript", "payload single transcript")
    check(premium.parse_payload("one:2160p:x") is None and premium.parse_payload("free") is None, "bad payloads rejected")

    # --- picker keyboard ------------------------------------------------------------------
    from app.services.quality_options import QualityOption
    MB = 1024 * 1024
    opts = [
        QualityOption("360p", "360p", 100 * MB, False),
        QualityOption("720p", "720p", 700 * MB, False),
        QualityOption("1080p", "1080p", 900 * MB, True),
        QualityOption("audio", "audio", 5 * MB, True),
    ]
    free = premium.Access(premium=False, unlimited=False)
    rows = h._build_quality_keyboard(VID, opts, _, free, premium.QuotaStatus(1, 3, None)).inline_keyboard
    labels = [r[0].text for r in rows]
    datas = [r[0].callback_data for r in rows]
    check(labels == ["📥 360p · 100 MB", "🔒 720p · 700 MB", "🔒 1080p · 900 MB", "⚡ Аудіо · 5.0 MB", "🔒 Текст відео"], f"free picker locks {labels}")
    check(datas[1] == f"dl:720p:{VID}:b" and datas[0] == f"dl:360p:{VID}" and datas[4] == f"tx:{VID}", "over-500MB flag + transcript callback")
    rows = h._build_quality_keyboard(VID, opts, _, free, premium.QuotaStatus(3, 3, None)).inline_keyboard
    check([r[0].text[0] for r in rows] == ["🔒", "🔒", "🔒", "⚡", "🔒"], "quota exhausted: cached audio still free")
    rows = h._build_quality_keyboard(VID, opts, _, premium.Access(True, True), None).inline_keyboard
    check([r[0].text[0] for r in rows] == ["📥", "📥", "⚡", "⚡", "📝"], "premium: nothing locked")
    # meta missing: sizes unknown, so no size lock and no :b flag; only 1080p/transcript stay Premium
    from app.services.quality_options import build_quality_options
    rows = h._build_quality_keyboard(VID, build_quality_options(None, {}), _, free, premium.QuotaStatus(0, 3, None)).inline_keyboard
    check([r[0].text for r in rows] == ["📥 360p", "📥 480p", "📥 720p", "📥 Аудіо", "🔒 Текст відео"], f"no meta: free picker unlocked {[r[0].text for r in rows]}")
    check(all(not r[0].callback_data.endswith(":b") for r in rows), "no meta: no over-size flag")
    check(h._parse_quality_callback(f"dl:720p:{VID}:b") == ("720p", VID, True), "parse flag")
    check(h._parse_quality_callback(f"dl:720p:{VID}:x") is None, "unknown flag rejected")

    # --- callback flow ---------------------------------------------------------------------
    dl = AsyncMock(return_value={"status": "queued"})
    advert = AsyncMock()
    common = [
        patch.object(h, "request_media_download", dl),
        patch.object(h, "try_send_cached_media_for_url", AsyncMock(return_value=False)),
        patch.object(h, "get_cached_sizes", AsyncMock(return_value={})),
        patch.object(h, "send_post_delivery_advert", advert),
    ]
    for item in common:
        item.start()

    # free user, 1080p, no credit -> paywall with single option, nothing queued
    await fresh_user(10)
    picker = make_msg(message_id=20, text=f"🎬 Rick Astley · 3:33\n\nОбери якість:", user_id=10)
    await h.quality_selected_handler(Cb(f"dl:1080p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    paywall = [c for c in picker.calls if c[0] == "reply"]
    check(len(paywall) == 1 and "1080p доступно тільки в Premium" in paywall[0][1], "1080p -> paywall")
    buttons = [b.text for row in paywall[0][2]["reply_markup"].inline_keyboard for b in row]
    check(buttons == ["⭐ Підписка · 99 ⭐/міс", "3 місяці · 249 ⭐", "Рік · 799 ⭐", "🎟 Тільки цей раз (1080p) · 10 ⭐"], f"paywall buttons {buttons}")
    invoices = picker.bot.invoices
    check(invoices[0]["subscription_period"] == 2592000 and invoices[0]["prices"][0].amount == 99, "month invoice is a 30-day subscription")
    check(invoices[-1]["payload"] == f"one:1080p:{VID}" and invoices[-1]["prices"][0].amount == 10 and invoices[-1]["subscription_period"] is None, "single invoice payload/price")
    check(not dl.await_count, "nothing queued behind the paywall")
    check(premium._pending_actions[10].title == "Rick Astley", "pending action remembered with title")

    # free user, 360p within quota -> queued, counts to quota, ads on
    picker = make_msg(message_id=21, user_id=10)
    await h.quality_selected_handler(Cb(f"dl:360p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    req = await DownloadRequest.find_one(DownloadRequest.user_id == 10, DownloadRequest.message_id == 21)
    check(dl.await_count == 1 and req.counts_to_quota and not req.premium and not req.paid_credit, "free 360p queued, counts to quota")

    # meta missing -> picker button has no size flag -> free 720p is queued, whatever the real size
    picker = make_msg(message_id=28, user_id=10)
    dl.reset_mock()
    await h.quality_selected_handler(Cb(f"dl:720p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    req = await DownloadRequest.find_one(DownloadRequest.user_id == 10, DownloadRequest.message_id == 28)
    check(dl.await_count == 1 and req.quality == "720p" and req.counts_to_quota, "no meta: free 720p downloads (no size check)")
    await req.delete()

    # exhaust the quota -> paywall reason quota
    for i in range(2):
        await DownloadRequest(url="u", user_id=10, status="completed", counts_to_quota=True).insert()
    picker = make_msg(message_id=22, user_id=10)
    await h.quality_selected_handler(Cb(f"dl:480p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    check("Безкоштовний ліміт вичерпано" in texts(picker)[0] and ("через 24 год." in texts(picker)[0] or "через 23 год 59 хв." in texts(picker)[0]), f"quota paywall {texts(picker)[0]!r}")
    # cached quality still free when the quota is used up
    with patch.object(h, "get_cached_sizes", AsyncMock(return_value={"480p": 10})), \
         patch.object(h, "try_send_cached_media_for_url", AsyncMock(return_value=True)):
        picker = make_msg(message_id=23, user_id=10)
        await h.quality_selected_handler(Cb(f"dl:480p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
        check(("delete",) in picker.calls and advert.await_count == 1, "cache hit allowed past the quota, with advert")

    # with a credit: 720p over 500 MB -> credit spent, no ads, paid flag
    await premium.add_credits(10, 1)
    picker = make_msg(message_id=24, user_id=10)
    await h.quality_selected_handler(Cb(f"dl:720p:{VID}:b", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    req = await DownloadRequest.find_one(DownloadRequest.user_id == 10, DownloadRequest.message_id == 24)
    check(req and req.paid_credit and req.premium and not req.counts_to_quota, "credit spent on >500MB 720p")
    check(any("Використано разове завантаження. Залишилось: 0" in t for t in texts(picker)), "credit-used notice")
    check((await User.get(10)).single_credits == 0, "credit balance 0")

    # credit returned when NATS is down
    await premium.add_credits(10, 1)
    from nats.errors import NoRespondersError
    dl.side_effect = NoRespondersError()
    picker = make_msg(message_id=25, user_id=10)
    await h.quality_selected_handler(Cb(f"dl:1080p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    dl.side_effect = None
    check((await User.get(10)).single_credits == 1 and any("повернуто на баланс" in t for t in texts(picker)), "NATS failure returns the credit")
    await premium.add_credits(10, -1)

    # premium user: 1080p queued, no ads, no quota
    await fresh_user(11, premium_until=now + datetime.timedelta(days=5))
    picker = make_msg(message_id=26, user_id=11)
    await h.quality_selected_handler(Cb(f"dl:1080p:{VID}", picker, 11), nats_client=None, language="uk", _=_, user=await User.get(11))
    req = await DownloadRequest.find_one(DownloadRequest.user_id == 11, DownloadRequest.message_id == 26)
    check(req and req.premium and not req.counts_to_quota and req.quality == "1080p", "premium 1080p: no ads, no quota")

    # admin (ADMIN_IDS): 1080p without paywall or quota, ads stay
    admin_id = settings.admin_ids[0]
    await fresh_user(admin_id)
    for i in range(3):
        await DownloadRequest(url="u", user_id=admin_id, status="completed", counts_to_quota=True).insert()
    check(premium.get_access(await User.get(admin_id)) == premium.Access(premium=False, unlimited=True, priority=True), "admin access unlimited + priority, not premium")
    picker = make_msg(message_id=29, user_id=admin_id)
    await h.quality_selected_handler(Cb(f"dl:1080p:{VID}:b", picker, admin_id), nats_client=None, language="uk", _=_, user=await User.get(admin_id))
    req = await DownloadRequest.find_one(DownloadRequest.user_id == admin_id, DownloadRequest.message_id == 29)
    check(req and req.quality == "1080p" and not req.counts_to_quota and not req.premium, "admin 1080p >500MB past the quota, ads stay")
    check(premium.get_access(await User.get(10)).unlimited is False, "non-admin still limited")

    # paywall switched off: free user gets 1080p, ads stay
    with patch.object(settings, "premium_enabled", False):
        picker = make_msg(message_id=27, user_id=10)
        await h.quality_selected_handler(Cb(f"dl:1080p:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
        req = await DownloadRequest.find_one(DownloadRequest.user_id == 10, DownloadRequest.message_id == 27)
        check(req and not req.premium and not req.counts_to_quota, "PREMIUM_ENABLED=false: no paywall, ads stay")

    # --- transcript ---------------------------------------------------------------------------
    tx_ok = AsyncMock(return_value={"result": "ok", "transcript_text": "[0:00] hello\n[0:30] world"})
    with patch.object(h, "request_transcript", tx_ok):
        picker = make_msg(message_id=30, text="🎬 Rick: <Astley>/x · 3:33\n\nОбери", user_id=11)
        await h.transcript_selected_handler(Cb(f"tx:{VID}", picker, 11), nats_client=None, language="uk", _=_, user=await User.get(11))
        check(tx_ok.await_args.args[0] == VID and tx_ok.await_args.kwargs["language"] == "uk", "transcript requested")
        status_calls = [c for c in picker.calls if c[0] == "reply"]
        check(len(status_calls) == 1 and "Отримую текст" in status_calls[0][1], "status reply")
    # run the transcript into a status message we hold, to inspect the file
    status = make_msg(message_id=31, user_id=11)
    with patch.object(h, "request_transcript", tx_ok):
        await h._run_transcript(status=status, nats_client=None, user_id=11, video_id=VID, title="Rick: <Astley>/x",
                                language="uk", _=_, grant=premium.Grant(True, False, False))
    doc = [c for c in status.calls if c[0] == "document"]
    body = doc[0][1].data.decode() if doc else ""
    check(bool(doc) and doc[0][1].filename == "Rick Astley x.txt", f"filename sanitized: {doc[0][1].filename if doc else None}")
    check(body.startswith("Rick: <Astley>/x\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ\n\n[0:00] hello"), "file has title, url, text")
    check("&lt;Astley&gt;" in doc[0][2]["caption"] and "@Dwloadbot" in doc[0][2]["caption"], "caption escaped + bot")
    check(("delete",) in status.calls, "status deleted after the file")

    # transcript not available + paid credit -> error text, credit back
    await premium.add_credits(10, 1)
    with patch.object(h, "request_transcript", AsyncMock(return_value={"result": 0, "error_code": 308, "error": "TranscriptsDisabled"})):
        picker = make_msg(message_id=32, user_id=10)
        await h.transcript_selected_handler(Cb(f"tx:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    check((await User.get(10)).single_credits == 1, "308 returns the credit")
    await premium.add_credits(10, -1)
    status = make_msg(message_id=33, user_id=10)
    with patch.object(h, "request_transcript", AsyncMock(return_value={"result": 0, "error_code": 308})):
        await h._run_transcript(status=status, nats_client=None, user_id=10, video_id=VID, title="", language="uk", _=_,
                                grant=premium.Grant(no_ads=False, counts_to_quota=False, paid_credit=False))
    check(any("немає субтитрів" in t for t in texts(status)), "308 -> no captions text")
    picker = make_msg(message_id=34, user_id=10)
    await h.transcript_selected_handler(Cb(f"tx:{VID}", picker, 10), nats_client=None, language="uk", _=_, user=await User.get(10))
    check("Текст відео доступний тільки в Premium" in texts(picker)[0], "free transcript -> paywall")

    # --- payments ---------------------------------------------------------------------------
    def sp(payload, charge, amount, recurring=False, first=False):
        # Telegram sends subscription_expiration_date as a Unix timestamp (int) on subscription payments.
        expires = int((now + datetime.timedelta(days=30)).timestamp()) if recurring else None
        return SuccessfulPayment.model_validate(dict(
            currency="XTR", total_amount=amount, invoice_payload=payload, telegram_payment_charge_id=charge,
            provider_payment_charge_id="", is_recurring=recurring or None, is_first_recurring=first or None,
            subscription_expiration_date=expires,
        ))

    await fresh_user(20)
    premium.remember_pending_action(20, "1080p", VID, "Rick")
    dl.reset_mock()
    msg = make_msg(message_id=40, user_id=20, successful_payment=sp("month", "stxFIRST", 99, True, True))
    await pay.successful_payment_handler(msg, nats_client=None, language="uk", _=_)
    u = await User.get(20)
    check(near(u.premium_until, now + datetime.timedelta(days=30)) and u.premium_auto_renew and u.premium_subscription_charge_id == "stxFIRST", "subscription activates 30 days + auto-renew")
    check(any("Premium активовано" in t for t in texts(msg)), "activation message")
    rec = await Payment.find_one(Payment.charge_id == "stxFIRST")
    check(rec is not None and near(rec.subscription_expiration_date, now + datetime.timedelta(days=30)), "int subscription_expiration_date stored as a date")
    check(dl.await_count == 1 and dl.await_args.kwargs["quality"] == "1080p", "pending 1080p started after purchase")
    await pay.successful_payment_handler(make_msg(message_id=41, user_id=20, successful_payment=sp("month", "stxFIRST", 99, True, True)), nats_client=None, language="uk", _=_)
    check(near((await User.get(20)).premium_until, now + datetime.timedelta(days=30)), "duplicate charge ignored")
    msg = make_msg(message_id=42, user_id=20, successful_payment=sp("month", "stxRENEW", 99, True, False))
    await pay.successful_payment_handler(msg, nats_client=None, language="uk", _=_)
    u = await User.get(20)
    check(near(u.premium_until, now + datetime.timedelta(days=60)) and u.premium_subscription_charge_id == "stxFIRST", "renewal stacks, keeps first charge id")
    check(any("продовжено" in t for t in texts(msg)), "renewal message")

    await fresh_user(21)
    msg = make_msg(message_id=43, user_id=21, successful_payment=sp(f"one:720p:{VID}", "stxONE", 10))
    dl.reset_mock()
    await pay.successful_payment_handler(msg, nats_client=None, language="uk", _=_)
    req = await DownloadRequest.find_one(DownloadRequest.user_id == 21, DownloadRequest.quality == "720p")
    check(dl.await_count == 1 and req and req.paid_credit and (await User.get(21)).single_credits == 0, "single purchase spends its credit on the action")
    pays = await Payment.find(Payment.user_id == 21).to_list()
    check(len(pays) == 1 and pays[0].granted_credits == 1 and pays[0].amount == 10, "payment stored")

    # pre-checkout
    answers = []
    query = SimpleNamespace(currency="XTR", invoice_payload="year", answer=AsyncMock(side_effect=lambda **kw: answers.append(kw)))
    await pay.pre_checkout_handler(query, _=_)
    query = SimpleNamespace(currency="XTR", invoice_payload="bogus", answer=AsyncMock(side_effect=lambda **kw: answers.append(kw)))
    await pay.pre_checkout_handler(query, _=_)
    check(answers[0] == {"ok": True} and answers[1]["ok"] is False, "pre-checkout accepts ours, rejects unknown")

    # /premium for a renewing premium user hides the subscription button, shows the toggle
    msg = make_msg(message_id=44, user_id=20)
    await pay.premium_handler(msg, language="uk", _=_, user=await User.get(20))
    kb = [b.text for row in msg.calls[0][2]["reply_markup"].inline_keyboard for b in row]
    check(kb == ["3 місяці · 249 ⭐", "Рік · 799 ⭐", "Вимкнути автопродовження"], f"/premium buttons {kb}")
    check("Premium активний" in msg.calls[0][1], "/premium status text")
    msg = make_msg(message_id=45, user_id=10)
    await pay.premium_handler(msg, language="uk", _=_, user=await User.get(10))
    check("Ви використали сьогодні: 3 з 3" in msg.calls[0][1], "/premium for free user shows quota")

    # --- status updates: atomic, late upload ticks dropped --------------------------------------
    req = DownloadRequest(url="u", user_id=30, message_id=50, status="uploading_to_telegram")
    await req.insert()
    rid = str(req.id)
    tick = {"request_id": rid, "status": "uploading_to_telegram", "progress": 40, "terminal": False}
    got, _prev, _lang = await nats_meta._update_download_request_from_status(tick)
    check(got is not None and (await DownloadRequest.get(req.id)).progress == 40, "upload tick stored")
    # the channel post delivered meanwhile (atomic lock), then a late tick and the final status arrive
    await DownloadRequest.get_pymongo_collection().update_one({"_id": req.id}, {"$set": {"media_sent": True}})
    stale = False
    try:
        await nats_meta._update_download_request_from_status({**tick, "progress": 95})
    except nats_meta._StaleStatusUpdate:
        stale = True
    fresh = await DownloadRequest.get(req.id)
    check(stale and fresh.progress == 40 and fresh.media_sent, "late tick after delivery dropped")
    await nats_meta._update_download_request_from_status({"request_id": rid, "status": "completed", "terminal": True})
    fresh = await DownloadRequest.get(req.id)
    check(fresh.status == "completed" and fresh.terminal and fresh.media_sent, "completed keeps media_sent (no full-save rollback)")
    stale = False
    try:
        await nats_meta._update_download_request_from_status({**tick, "progress": 95})
    except nats_meta._StaleStatusUpdate:
        stale = True
    check(stale and (await DownloadRequest.get(req.id)).status == "completed", "tick after completed dropped")

    # --- ads & broadcasts ----------------------------------------------------------------------
    for item in common:
        item.stop()
    with patch("app.services.broadcast.BroadcastService") as bs:
        await nats_meta.send_post_delivery_advert(FakeBot(), 20)
        check(not bs.called, "no post-delivery advert for premium user")
    text = nats_meta._build_download_status_text({"status": "downloading", "progress": 5, "user_language": "uk"}, quality="720p", show_bottom_ad=False)
    check("shortsum_bot" not in text, "no bottom ad in premium status")
    recipients = await BroadcastService._fetch_active_recipients_with_language(BroadcastService.__new__(BroadcastService))
    ids = {r[0] for r in recipients}
    check(20 not in ids and 11 not in ids and 10 in ids, "broadcast skips premium users")

    # --- launch mode: FREE_MAX_FILE_MB=0 lifts the free size cap --------------------------------
    settings.free_max_file_mb = 0
    big = QualityOption("720p", "720p", 900 * 1024 * 1024, cached=False)
    check(premium.free_max_bytes() == 0 and not h._is_over_free_size(big), "no free size cap: 900 MB 720p unlocked")
    check(premium.static_lock_reason("720p", True) is None, "stale :b picker flag ignored without a cap")
    check(premium.static_lock_reason("transcript", False) == premium.REASON_TRANSCRIPT, "transcript stays Premium without a cap")
    check("2 ГБ" not in premium.build_benefits_text(_), "benefits drop the size line without a cap")
    settings.free_max_file_mb = 500

    await client.drop_database(DB_NAME)
    await client.close()
    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
