"""خطأ غير متوقع في نشاط واحد (زي KeyError اللي ظهر في الإنتاج) ما يوقفش الحل كله."""
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_engine as eng
import matching
from fake_platform import FakePlatform

ROOT = Path(__file__).resolve().parent.parent
_REAL_CLIENT = httpx.Client
ENTRIES = matching.parse_answer_file(ROOT / "answers.txt")
ITEMS = [("chapter", 70), ("chapter", 71), ("review", 5), ("test", 9)]


def _truth(text, option_texts):
    return matching.resolve_answer(text, ENTRIES, option_texts)[0]


@pytest.fixture
def run(monkeypatch, tmp_path):
    def _run(platform):
        monkeypatch.setattr(eng, "_learned_singleton", eng.LearnedAnswers(tmp_path / "l.json"))
        monkeypatch.setattr(matching, "record_unmatched", lambda *a, **k: None)
        monkeypatch.setattr(eng, "record_unmatched", lambda *a, **k: None)

        def factory(*a, **kw):
            kw.pop("http2", None)
            kw.pop("proxy", None)
            kw["transport"] = httpx.MockTransport(platform.handler)
            return _REAL_CLIENT(*a, **kw)

        monkeypatch.setattr(eng.httpx, "Client", factory)
        monkeypatch.setattr(eng, "_sleep", lambda c, s: eng._check(c))
        return eng.run_job("user", "x", ENTRIES, None, threading.Event())
    return _run


def test_one_off_unexpected_error_is_retried_and_job_still_finishes(run, monkeypatch):
    calls = {"n": 0}
    real = eng._solve_chapter

    def flaky(ctx, item):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError(45)
        return real(ctx, item)

    monkeypatch.setattr(eng, "_solve_chapter", flaky)
    result = run(FakePlatform(_truth, items=ITEMS))
    assert result["status"] == "finished", result
    assert calls["n"] >= 2


def test_persistent_unexpected_error_is_bounded_and_reported_with_location(run, monkeypatch):
    calls = {"n": 0}

    def broken(ctx, item):
        calls["n"] += 1
        raise KeyError(51)

    monkeypatch.setattr(eng, "_solve_chapter", broken)
    result = run(FakePlatform(_truth, items=ITEMS))
    assert result["status"] == "stuck", result
    assert calls["n"] == eng.MAX_ITEM_ATTEMPTS                 # مفيش لف لا نهائي
    assert "KeyError: 51" in result["error"] and "@ test_engine_resilience.py" in result["error"]


def test_stop_and_login_failure_still_propagate(run, monkeypatch):
    def stopper(ctx, item):
        raise eng.Stopped()

    monkeypatch.setattr(eng, "_solve_chapter", stopper)
    assert run(FakePlatform(_truth, items=ITEMS))["status"] == "stopped"


def test_partial_test_mapping_raises_structure_error_not_keyerror(monkeypatch):
    """ربط ناقص بين test_question_id والسؤال → UnknownStructure (بيتخطّى النشاط) بدل KeyError."""
    class Client:
        def test_start(self, test_id):
            return {
                "question_responses": [{"test_question_id": 45}, {"test_question_id": 46}],
                "test_questions": [{"id": 46, "question_id": 900}],      # ناقص: 45 مش موجود
                "questions": {"900": {"id": 900, "question_text": "q"}},
                "options": {},
            }

    ctx = eng._Ctx(Client(), ENTRIES, None, None, threading.Event())
    with pytest.raises(eng.UnknownStructure, match="tq=45"):
        eng._attempt_test(ctx, 9, "t")


def test_where_points_to_the_failing_line():
    try:
        {}[7]
    except KeyError as exc:
        assert eng._where(exc).startswith("test_engine_resilience.py:")
