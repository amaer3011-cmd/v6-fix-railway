"""إشعار الأدمن: بيتبعت بس لما المنصة تتنجز فعلًا. + تصنيف حسابات "مالهاش صلاحية" برسالة واضحة."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import bot
import db
import qureo_engine

OWNER = 999
USER = 5


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.init(str(tmp_path / "t.db"))
    db.touch_user(USER, "mohamed", "mohamed")
    monkeypatch.setattr(bot, "OWNER_CHAT_ID", OWNER)
    return db


def _run(monkeypatch, platform, job_result):
    """يشغّل run_student_job بمحرك مزيّف ويرجّع رسائل الأدمن ورسائل المستخدم."""
    fake = lambda *a, **k: dict(job_result)
    monkeypatch.setattr(bot.core, "run_job", fake)
    monkeypatch.setattr(bot.qureo_engine, "run_job", fake)

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
        await asyncio.wait_for(
            bot.run_student_job(app, USER, USER, "476329837", "pw", platform), timeout=15
        )
        return tg

    tg = asyncio.run(scenario())
    sent = [(c.args[0], c.args[1]) for c in tg.send_message.await_args_list]
    to_owner = [text for chat, text in sent if chat == OWNER]
    to_user = [text for chat, text in sent if chat == USER]
    return to_owner, to_user


# ── القاعدة نفسها ─────────────────────────────────────────────
@pytest.mark.parametrize("status,expected", [
    ("finished", True),
    ("login_failed", False), ("stuck", False), ("stopped", False), ("timeout", False),
    ("error", False), ("tests_pending", False), ("already_completed", False), (None, False),
])
def test_only_finished_triggers_admin_notice(status, expected):
    assert bot.should_notify_admin_completion(status) is expected


# ── تكاملي: أي حالة غير الإنجاز مفيهاش إشعار للأدمن ───────────
@pytest.mark.parametrize("platform", ["financial", "qureo", "javascript"])
@pytest.mark.parametrize("job_result", [
    {"status": "login_failed", "last": ""},
    {"status": "stuck", "error": "Qureo HTTP 403: course access", "last": ""},
    {"status": "stuck", "error": "Qureo HTTP 409: select Python course", "last": ""},
    {"status": "stuck", "error": "باقي أنشطة لم تُفتح بعد (3/9)", "last": "x"},
    {"status": "stopped", "last": "x"},
    {"status": "error", "error": "HTTP 403: User has no access to this subject (/subjects/7/curriculum)"},
    {"status": "error", "error": "boom"},
    {"status": "tests_pending", "reason": "unmatched", "test": "t", "question": "q"},
    {"status": "finished", "precompleted": True, "last": "x"},      # منجز قبل كده: مفيش إنجاز جديد
])
def test_no_admin_notice_unless_really_completed(env, monkeypatch, platform, job_result):
    to_owner, to_user = _run(monkeypatch, platform, job_result)
    assert [t for t in to_owner if "إشعار إتمام" in t] == []
    assert to_user, "المستخدم لازم يستلم رسالة نتيجة دايمًا"


@pytest.mark.parametrize("platform", ["financial", "qureo", "javascript"])
def test_admin_notice_sent_when_platform_completed(env, monkeypatch, platform):
    to_owner, _ = _run(monkeypatch, platform, {
        "status": "finished", "last": "آخر درس", "questions": 12, "activities": 30,
    })
    notices = [t for t in to_owner if "إشعار إتمام" in t]
    assert len(notices) == 1
    assert "اكتمل بنجاح" in notices[0] and "476329837" in notices[0]


# ── حسابات مالهاش صلاحية: رسالة واضحة + مفيش خصم محاولة ─────────
@pytest.mark.parametrize("platform,job_result", [
    ("qureo", {"status": "stuck", "error": "Qureo HTTP 403: course access", "last": ""}),
    ("javascript", {"status": "stuck", "error": "Qureo HTTP 403: course access", "last": ""}),
    ("financial", {"status": "error", "error": "HTTP 403: User has no access to this subject (/subjects/7/curriculum)"}),
])
def test_no_access_account_gets_clear_message_and_no_charge(env, monkeypatch, platform, job_result):
    before = db.get_entitlement(USER)
    to_owner, to_user = _run(monkeypatch, platform, job_result)
    text = "\n".join(to_user)
    assert "مالوش صلاحية" in text and "لم يتم خصم" in text
    assert "توقف في منتصف الطريق" not in text and "مشكلة مؤقتة" not in text
    assert to_owner == []
    assert db.get_entitlement(USER) == before


def test_select_course_message_unchanged(env, monkeypatch):
    _, to_user = _run(monkeypatch, "qureo", {"status": "stuck", "error": "Qureo HTTP 409: select Python course", "last": ""})
    assert "اختار دورة Python" in "\n".join(to_user)


# ── تصنيف الأخطاء ─────────────────────────────────────────────
@pytest.mark.parametrize("error,expected", [
    ("Qureo HTTP 403: course access", True),
    ("HTTP 403: User has no access to this subject (/subjects/7/curriculum)", True),
    ("Qureo HTTP 409: select Python course", False),
    ("Qureo HTTP 400: /api/study/students/lectures/1/complete", False),
    ("HTTP 500: boom", False),
    ("", False), (None, False),
])
def test_is_access_error(error, expected):
    assert bot.is_access_error(error) is expected


# ── المحرك الحقيقي: كورس غير متاح للحساب → stuck بنص 403 (مش login_failed) ──
def test_qureo_engine_course_access_is_classified(monkeypatch):
    class Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            raise qureo_engine.PlatformError(403, "course access")

        def timing_report(self):
            return []

        def request_count(self):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(qureo_engine, "QureoClient", Client)
    result = qureo_engine.run_job("476329837", "pw", [], None)
    assert result["status"] == "stuck"
    assert bot.is_access_error(result["error"])
