"""الطابور (المشتركين بيعدّوه) + سقف مكافأة الدعوات + التوازي التكيّفي + رسالة الاشتراكات."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import api_engine as eng
import db


@pytest.fixture()
def conn(tmp_path):
    db.init(str(tmp_path / "t.db"))
    for uid in range(1, 30):
        db.touch_user(uid, f"u{uid}", f"u{uid}")
    return db


# ── من يعدّي الطابور ──────────────────────────────────────────
def test_free_user_has_no_priority(conn):
    assert db.has_priority(1) is False


def test_single_code_gives_priority_until_used(conn):
    db.consume_attempt(1, "financial", "A")              # يخلّص المجانية
    assert db.has_priority(1) is False
    db.redeem_code(db.generate_code("single"), 1)
    assert db.has_priority(1) is True                     # اشترى كود: بيعدّي الطابور
    db.consume_attempt(1, "financial", "B")
    assert db.has_priority(1) is False                    # الكود خلص


def test_pack_and_weekly_have_priority(conn):
    db.redeem_code(db.generate_code("vip"), 1)
    db.redeem_code(db.generate_code("vip_unlimited"), 2)
    assert db.has_priority(1) is True and db.has_priority(2) is True


def test_invite_reward_does_not_skip_queue(conn):
    for r in range(10, 15):
        db.register_referral(3, r)
    assert db.get_entitlement(3)["ref_attempts"] == 1
    assert db.has_priority(3) is False                    # مكافأة دعوات مش اشتراك


# ── سقف الدعوات ───────────────────────────────────────────────
def test_referral_rewards_capped_at_two(conn):
    results = [db.register_referral(1, 10 + i) for i in range(14)]
    assert sum(r["new_rewards"] for r in results) == 2
    assert results[4]["new_rewards"] == 1 and results[9]["new_rewards"] == 1
    assert results[9]["capped"] is True and results[9]["remaining"] == 0
    assert db.get_entitlement(1)["ref_attempts"] == 2
    assert db.get_referral_progress(1)["capped"] is True


def test_referral_progress_before_cap(conn):
    for i in range(3):
        db.register_referral(1, 10 + i)
    p = db.get_referral_progress(1)
    assert (p["count"], p["granted"], p["remaining"], p["capped"]) == (3, 0, 2, False)


def test_referral_attempt_works_on_any_account(conn):
    db.consume_attempt(1, "financial", "A")
    for i in range(5):
        db.register_referral(1, 10 + i)
    assert db.check_account_access(1, "qureo", "B") == {"ok": True, "kind": "ref"}
    assert db.consume_attempt(1, "javascript", "B")["kind"] == "single_ref"
    assert db.has_attempt(1) is False


# ── رسالة الاشتراكات ──────────────────────────────────────────
def test_subscription_text_has_prices():
    import bot
    text = bot.subscription_text()
    assert "٥٠" in text and "١٢٠" in text and "٢٥٠" in text
    assert "فورًا" in text and "الطابور" in text


# ── التوازي التكيّفي ──────────────────────────────────────────
class _Client:
    def __init__(self):
        self.throttled = 0


class _Ctx:
    def __init__(self):
        self.client = _Client()
        self.cancel = threading.Event()
        self.par = {}


def test_adaptive_map_keeps_order_and_grows():
    ctx = _Ctx()
    out = list(eng._adaptive_map(ctx, "t", lambda x: x * 2, range(40), 6))
    assert out == [x * 2 for x in range(40)]
    assert ctx.par["t"] == 6                              # كبر لحد الحد الأقصى


def test_adaptive_map_backs_off_when_throttled():
    ctx = _Ctx()
    widths = []

    def work(x):
        if x == 12:                                       # المنصة اتضغطت في نص الشغل
            ctx.client.throttled += 1
        return x

    for x in eng._adaptive_map(ctx, "t", work, range(40), 8):
        widths.append(ctx.par["t"])
    assert max(widths) >= 5 and min(widths[12:]) < max(widths)   # نزل بعد الضغط
    assert sorted(set(range(40))) == list(range(40))


def test_adaptive_map_propagates_errors():
    ctx = _Ctx()

    def boom(x):
        if x == 3:
            raise RuntimeError("boom")
        return x

    with pytest.raises(RuntimeError):
        list(eng._adaptive_map(ctx, "t", boom, range(10), 4))


# ── منطق الطابور في البوت ─────────────────────────────────────
class _App:
    def __init__(self, **data):
        self.bot_data = data


def test_queue_full_counts_only_normal_users():
    import bot
    app = _App(normal_active=bot.MAX_JOBS + bot.MAX_QUEUE - 1, cancels={i: 1 for i in range(50)})
    assert bot.queue_full(app) is False                    # المشتركين الشغّالين مش بيملّوا الطابور
    app.bot_data["normal_active"] = bot.MAX_JOBS + bot.MAX_QUEUE
    assert bot.queue_full(app) is True


def test_queue_closed_flag_and_persisted_setting(conn):
    import bot
    app = _App()
    assert bot.queue_closed(app) is False
    bot._set_queue_closed(app, True)
    assert bot.queue_closed(app) is True
    assert db.get_setting("queue_closed", "0") == "1"      # بيتحفظ ويفضل بعد إعادة التشغيل
    bot._set_queue_closed(app, False)
    assert bot.queue_closed(app) is False and db.get_setting("queue_closed", "0") == "0"


def test_is_vip_user_matches_purchases(conn):
    import bot
    assert bot.is_vip_user(5) is False
    db.consume_attempt(5, "financial", "A")
    db.redeem_code(db.generate_code("single"), 5)
    assert bot.is_vip_user(5) is True


# ── اختبار تكاملي: المشترك يتحل له فورًا والعادي يستنى ─────────
def test_buyer_skips_queue_while_normal_user_waits(conn, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import bot

    monkeypatch.setattr(bot, "MAX_JOBS", 1)                    # خانة واحدة عشان الطابور يبان
    monkeypatch.setattr(bot, "MAX_QUEUE", 10)
    db.consume_attempt(7, "financial", "BUYER-OLD")            # يخلّص مجانيته
    db.redeem_code(db.generate_code("single"), 7)              # ويشتري محاولة
    assert bot.is_vip_user(7) and not bot.is_vip_user(8) and not bot.is_vip_user(9)

    started, gate = [], threading.Event()

    def fake_run_job(username, password, entries, hook=None, cancel=None, *a, **k):
        started.append(username)
        gate.wait(timeout=5)                                    # الحل "شغّال" لحد ما نفتح البوابة
        return {"status": "login_failed", "last": ""}

    monkeypatch.setattr(bot.core, "run_job", fake_run_job)

    async def scenario():
        tg = MagicMock()
        tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
        tg.edit_message_text = AsyncMock()
        app = SimpleNamespace(bot=tg, create_task=lambda c: None)
        app.bot_data = {
            "sem": asyncio.Semaphore(bot.MAX_JOBS), "psem": asyncio.Semaphore(bot.PRIORITY_JOBS),
            "cancels": {}, "waiting": 0, "normal_active": 0, "queue_closed": False,
            "entries": [], "qureo_entries": [], "javascript_entries": [],
        }
        job = lambda uid, name: asyncio.create_task(
            bot.run_student_job(app, uid, uid, name, "pw", "financial"))
        # عاديين بعدد الخانات + واحد زيادة في الطابور
        normals = [job(8, "normal-A"), job(9, "normal-B")]
        await asyncio.sleep(0.4)
        buyer = job(7, "BUYER")
        await asyncio.sleep(0.4)
        snapshot = list(started)
        waiting = app.bot_data["waiting"]
        gate.set()
        await asyncio.wait_for(asyncio.gather(buyer, *normals), timeout=10)
        return snapshot, waiting, app.bot_data["normal_active"]

    snapshot, waiting, normal_active = asyncio.run(scenario())
    assert "normal-A" in snapshot and "BUYER" in snapshot        # المشترك بدأ فورًا
    assert "normal-B" not in snapshot and waiting == 1           # العادي التاني لسه في الطابور
    assert normal_active == 0                                    # العدّاد اتنضف بعد الانتهاء


# ── عدّاد الوقت: من أول الحل مش من الانتظار ─────────────────────
def test_progress_clock_starts_only_when_solving_starts():
    import bot
    from unittest.mock import MagicMock

    progress = bot.Progress(MagicMock(), 1, 1, "code")
    time.sleep(0.3)                                       # "واقف في الطابور"
    assert progress.elapsed() == 0.0                      # العدّاد لسه ما بدأش
    assert "هيبدأ أول ما الحل يبدأ" in progress.render()
    progress.start_clock()
    assert progress.queue_wait >= 0.3                     # الانتظار اتسجّل لوحده
    time.sleep(0.2)
    assert 0.15 <= progress.elapsed() < 0.3               # وقت الحل من اللحظة دي بس
    assert "الوقت المنقضي" in progress.render()
