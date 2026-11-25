"""دعم طلاب اللغات: لو الحساب مالوش صلاحية على المادة الأولى (403) البوت يجرّب المواد التانية."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_engine as eng


def test_parse_subject_ids_keeps_primary_first_and_dedupes():
    assert eng._parse_subject_ids("", 7) == [7]
    assert eng._parse_subject_ids("9, 7 12;9", 7) == [7, 9, 12]
    assert eng._parse_subject_ids("abc,,x1", 7) == [7]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(eng, "SUBJECT_IDS", [7, 9, 12])
    c = eng.PlatformClient()
    yield c
    c.close()


def _script(client, monkeypatch, allowed):
    calls = []

    def fake_data(method, path, body=None, **kw):
        calls.append((method, path))
        for sid in (7, 9, 12):
            if path.startswith(f"/subjects/{sid}"):
                if sid in allowed:
                    return [{"id": sid}] if path.endswith("/curriculum") else {}
                raise eng.PlatformError(403, "User has no access to this subject", path)
        return {}

    monkeypatch.setattr(client, "_data", fake_data)
    return calls


def test_language_student_falls_through_to_their_subject(client, monkeypatch):
    calls = _script(client, monkeypatch, allowed={9})
    assert client.curriculum() == [{"id": 9}]
    assert client.subject_id == 9
    assert [p for _m, p in calls] == ["/subjects/7/curriculum", "/subjects/9/curriculum"]
    client.subject()                                    # باقي النداءات بتستخدم المادة اللي اتحددت
    client._unlock_achievements_now()
    assert ("GET", "/subjects/9") in calls and ("POST", "/subjects/9/achievements/unlock") in calls
    client.curriculum()                                 # مش بيجرّب تاني بعد ما اتحدد
    assert calls.count(("GET", "/subjects/7/curriculum")) == 1


def test_regular_student_stays_on_first_subject(client, monkeypatch):
    calls = _script(client, monkeypatch, allowed={7, 9})
    client.curriculum()
    assert client.subject_id == 7 and len(calls) == 1


def test_no_access_to_any_subject_raises_the_403(client, monkeypatch):
    _script(client, monkeypatch, allowed=set())
    with pytest.raises(eng.PlatformError) as err:
        client.curriculum()
    assert err.value.status == 403 and "no access" in str(err.value)


def test_cloudflare_block_is_not_treated_as_no_access(client, monkeypatch):
    def fake_data(method, path, body=None, **kw):
        raise eng.PlatformError(403, "حماية المنصة منعت الطلب", path)

    monkeypatch.setattr(client, "_data", fake_data)
    with pytest.raises(eng.PlatformError) as err:
        client.curriculum()
    assert "حماية" in err.value.message and client.subject_id == 7   # مبيلفش على المواد: ده حظر IP مش صلاحيات


def test_single_subject_default_behaviour_unchanged(monkeypatch):
    monkeypatch.setattr(eng, "SUBJECT_IDS", [7])
    c = eng.PlatformClient()
    seen = []
    monkeypatch.setattr(c, "_data", lambda m, p, b=None, **k: seen.append(p) or [])
    c.curriculum()
    assert seen == ["/subjects/7/curriculum"] and c._subject_locked
    c.close()
