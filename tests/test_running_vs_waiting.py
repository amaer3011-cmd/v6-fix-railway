"""«حلول جارية» لازم تفرّق بين الحلول اللي شغّالة فعلًا والمنتظرة في الطابور."""
import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import bot
import db

ADMIN = 999


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.init(str(tmp_path / "t.db"))
    monkeypatch.setattr(bot, "OWNER_CHAT_ID", ADMIN)
    for uid in range(1, 6):
        db.touch_user(uid, f"user{uid}", f"u{uid}")
    return db


def _bot_data(running=(), waiting=()):
    cancels = {uid: threading.Event() for uid in (*running, *waiting)}
    states = {**{u: "running" for u in running}, **{u: "waiting" for u in waiting}}
    return {"cancels": cancels, "job_states": states, "waiting": len(waiting)}


def test_job_groups_splits_running_and_waiting():
    data = _bot_data(running=(1, 2), waiting=(3, 4, 5))
    assert bot.job_groups(data) == ([1, 2], [3, 4, 5])
    assert bot.job_groups({}) == ([], [])
    data["cancels"][9] = threading.Event()                 # مسجّل بدون حالة = لسه منتظر
    assert 9 in bot.job_groups(data)[1]


def _admin_update():
    msg = SimpleNamespace(reply_text=AsyncMock(), reply_html=AsyncMock())
    q = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN), answer=AsyncMock(), message=msg)
    return SimpleNamespace(callback_query=q), msg


def test_admin_list_shows_two_sections(env):
    update, msg = _admin_update()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=_bot_data(running=(1, 2), waiting=(3, 4, 5))))
    asyncio.run(bot.on_admin_active(update, ctx))
    text = msg.reply_html.await_args.args[0]
    assert "شغّالة الآن" in text and "في الطابور" in text
    assert "شغّالة ٢" in text and "منتظرة ٣" in text
    assert text.index("شغّالة الآن") < text.index("user1") < text.index("في الطابور") < text.index("user3")
    kb = msg.reply_html.await_args.kwargs["reply_markup"]
    assert len([b for row in kb.inline_keyboard for b in row if b.callback_data.startswith("admin_stop_")]) == 5


def test_dashboard_and_health_counts_are_not_double_counted(env):
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={**_bot_data(running=(1, 2), waiting=(3, 4, 5)),
                                                                 "maintenance": False, "queue_closed": False}))
    msg = SimpleNamespace(reply_html=AsyncMock())
    asyncio.run(bot._send_admin_dashboard(msg, ctx))
    assert "شغّالة الآن: <b>٢</b>  •  انتظار: <b>٣</b>" in msg.reply_html.await_args.args[0]
    asyncio.run(bot._send_admin_health(msg, ctx))
    health = msg.reply_html.await_args.args[0]
    assert "وظائف شغّالة: <b>٢</b>" in health and "طلبات منتظرة: <b>٣</b>" in health


def test_empty_list_message(env):
    update, msg = _admin_update()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"cancels": {}}))
    asyncio.run(bot.on_admin_active(update, ctx))
    assert "مفيش حلول جارية" in msg.reply_text.await_args.args[0]


# ── دورة الحياة الحقيقية: منتظر → شغّال → اتشال ────────────────
def test_real_job_lifecycle_waiting_then_running_then_removed(env, monkeypatch):
    seen = {}
    release = threading.Event()

    def fake_run(*a, **k):
        release.wait(5)
        return {"status": "login_failed"}

    monkeypatch.setattr(bot.core, "run_job", fake_run)

    async def scenario():
        tg = MagicMock()
        tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
        tg.edit_message_text = AsyncMock()
        app = SimpleNamespace(bot=tg, create_task=lambda c: None)
        app.bot_data = {
            "sem": asyncio.Semaphore(1), "psem": asyncio.Semaphore(bot.PRIORITY_JOBS),
            "cancels": {}, "waiting": 0, "normal_active": 0, "queue_closed": False,
            "entries": [], "qureo_entries": [], "javascript_entries": [],
        }
        t1 = asyncio.create_task(bot.run_student_job(app, 1, 1, "111", "pw", "financial"))
        await asyncio.sleep(0.5)
        t2 = asyncio.create_task(bot.run_student_job(app, 2, 2, "222", "pw", "financial"))
        await asyncio.sleep(0.5)
        seen["during"] = bot.job_groups(app.bot_data)
        release.set()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=20)
        seen["after"] = (bot.job_groups(app.bot_data), dict(app.bot_data["job_states"]))

    monkeypatch.setattr(bot, "MAX_JOBS", 1)
    asyncio.run(scenario())
    assert seen["during"] == ([1], [2])            # الأول شغّال، التاني منتظر
    assert seen["after"] == (([], []), {})          # كله اتنضّف
