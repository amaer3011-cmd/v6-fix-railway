"""طلاب اللغات: لو الحساب مالوش الثقافة المالية، البوت بيسأل المنصة (GET /subjects) ويحل أول مادة مسموحة من غير أي إعداد."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import api_engine as eng
import bot
from fake_platform import FakePlatform
from test_realistic import ENTRIES, bank_truth

NO_ACCESS = "User has no access to this subject"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(eng, "SUBJECT_IDS", [7])
    c = eng.PlatformClient()
    yield c
    c.close()


def _script(client, monkeypatch, allowed, listing):
    calls = []

    def fake_data(method, path, body=None, **kw):
        calls.append((method, path))
        if path == "/subjects":
            if isinstance(listing, Exception):
                raise listing
            return listing
        for sid in range(1, 40):
            if path.startswith(f"/subjects/{sid}/") or path == f"/subjects/{sid}":
                if sid in allowed:
                    return [{"id": sid}] if path.endswith("/curriculum") else {}
                raise eng.PlatformError(403, NO_ACCESS, path)
        return {}

    monkeypatch.setattr(client, "_data", fake_data)
    return calls


def test_language_account_is_discovered_and_solved(client, monkeypatch):
    calls = _script(client, monkeypatch, allowed={3}, listing=[{"id": 3, "name_ar": "اللغة الإنجليزية"}])
    assert client.curriculum() == [{"id": 3}]
    assert client.subject_id == 3 and client.subject_label == "اللغة الإنجليزية"
    assert client._http.headers["Referer"].endswith("/subjects/3/curriculum")
    assert [p for _m, p in calls] == ["/subjects/7/curriculum", "/subjects", "/subjects/3/curriculum"]
    client.subject()
    client._unlock_achievements_now()
    assert ("GET", "/subjects/3") in calls and ("POST", "/subjects/3/achievements/unlock") in calls
    client.curriculum()                                      # بعد التحديد: مفيش اكتشاف تاني
    assert [p for _m, p in calls].count("/subjects") == 1


def test_financial_account_never_asks_for_the_list(client, monkeypatch):
    calls = _script(client, monkeypatch, allowed={7}, listing=[{"id": 3}])
    client.curriculum()
    assert client.subject_id == 7 and client.subject_label == ""
    assert [p for _m, p in calls] == ["/subjects/7/curriculum"]


def test_discovery_skips_already_tried_and_inaccessible_subjects(client, monkeypatch):
    calls = _script(client, monkeypatch, allowed={5}, listing=[{"id": 7}, {"id": 3}, {"id": "5", "name": "Français"}])
    client.curriculum()
    assert client.subject_id == 5 and client.subject_label == "Français"
    assert [p for _m, p in calls] == ["/subjects/7/curriculum", "/subjects", "/subjects/3/curriculum", "/subjects/5/curriculum"]


@pytest.mark.parametrize("listing", [[], None, {"unrelated": 1}, eng.PlatformError(500, "boom", "/subjects")])
def test_nothing_to_discover_raises_the_original_no_access_error(client, monkeypatch, listing):
    _script(client, monkeypatch, allowed=set(), listing=listing)
    with pytest.raises(eng.PlatformError) as err:
        client.curriculum()
    assert err.value.status == 403 and "no access" in err.value.message


def test_platform_block_is_not_mistaken_for_missing_access(client, monkeypatch):
    calls = []

    def fake_data(method, path, body=None, **kw):
        calls.append(path)
        raise eng.PlatformError(403, "حماية المنصة منعت الطلب", path)

    monkeypatch.setattr(client, "_data", fake_data)
    with pytest.raises(eng.PlatformError):
        client.curriculum()
    assert calls == ["/subjects/7/curriculum"]               # ما سألش عن المواد: ده حظر مش صلاحيات


@pytest.mark.parametrize("payload, expected", [
    ([{"id": 7, "name_ar": "الثقافة المالية"}, {"id": "12", "name": "English"}],
     [{"id": 7, "name": "الثقافة المالية"}, {"id": 12, "name": "English"}]),
    ({"subjects": [{"subject_id": 3, "title": "Français"}]}, [{"id": 3, "name": "Français"}]),
    ({"data": [{"id": 5}]}, [{"id": 5, "name": "مادة 5"}]),
    ([{"name": "no id"}, "junk", None], []),
    ({"unrelated": 1}, []),
    (None, []),
])
def test_subject_rows_accepts_reasonable_shapes(payload, expected):
    assert eng._subject_rows(payload) == expected


# ── المحرك كله ضد منصة وهمية بحساب لغات (المادة الأساسية 403) ──
def test_full_job_for_a_language_account(monkeypatch, tmp_path):
    platform = FakePlatform(bank_truth)
    inner = platform.handler
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == "/api/v1/subjects/7/curriculum":
            return httpx.Response(403, json={"success": False, "error": {"message": NO_ACCESS}})
        if path == "/api/v1/subjects" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": [{"id": 3, "name_ar": "لغة"}]})
        return inner(request)

    monkeypatch.setattr(eng, "SUBJECT_IDS", [7])
    monkeypatch.setattr(eng, "_learned_singleton", eng.LearnedAnswers(tmp_path / "learned.json"))
    real = httpx.Client

    def factory(*a, **k):
        k.pop("http2", None)
        k.pop("proxy", None)
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(eng.httpx, "Client", factory)
    monkeypatch.setattr(eng, "_sleep", lambda c, s: eng._check(c))
    result = eng.run_job("user", "x", ENTRIES, None, None)

    assert result["status"] == "finished", result
    assert result["subject_id"] == 3 and result["subject_label"] == "لغة"
    assert "/api/v1/subjects/3/curriculum" in seen
    assert "/api/v1/subjects/3/achievements/unlock" in seen


# ── البوت ──
def test_subject_note_only_for_non_primary_financial_subjects(monkeypatch):
    monkeypatch.setattr(bot.core, "SUBJECT_IDS", [7])
    assert bot.subject_note("financial", {"subject_id": 7}) == ""
    assert bot.subject_note("financial", {}) == ""
    assert bot.subject_note("qureo", {"subject_id": 3}) == ""
    line = bot.subject_note("financial", {"subject_id": 3, "subject_label": "<b>English</b>"})
    assert "المادة" in line and "(<code>3</code>)" in line and "<b>English</b>" not in line.replace("<b>المادة:</b>", "")
    assert bot.subject_note("financial", {"subject_id": 3}).count("(<code>3</code>)") == 1


def test_admin_completion_notice_mentions_the_language_subject(monkeypatch):
    import db
    db.init(str(Path(tempfile.mkdtemp()) / "t.db"))
    db.touch_user(1, "student", "s")
    monkeypatch.setattr(bot, "OWNER_CHAT_ID", 999)
    tg = SimpleNamespace(send_message=AsyncMock())
    app = SimpleNamespace(bot=tg)
    line = bot.subject_note("financial", {"subject_id": 3, "subject_label": "English"})
    asyncio.run(bot.notify_admin_completion(app, 1, "ACC", "financial", "finished", 10, 2, 60, "U", subject_line=line))
    text = tg.send_message.await_args.args[1]
    assert "English" in text and "الثقافة المالية" in text
