"""اختبارات تسريعات v6.8: طلب الإنجازات في الخلفية، إعادة استخدام لقطة المنهج، وحفظ إجابات Qureo بالتوازي."""
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_engine as eng
import matching
import qureo_engine as qureo
from fake_platform import FakePlatform

ROOT = Path(__file__).resolve().parent.parent
_REAL_CLIENT = httpx.Client        # الأصلي، قبل أي monkeypatch (الـ fixture بتتنادى أكتر من مرة في نفس الاختبار)
ENTRIES = matching.parse_answer_file(ROOT / "answers.txt")


def _bank_truth(text, option_texts):
    return matching.resolve_answer(text, ENTRIES, option_texts)[0]


@pytest.fixture
def run_financial(monkeypatch, tmp_path):
    def _run(platform, background=True):
        monkeypatch.setattr(eng, "_learned_singleton", eng.LearnedAnswers(tmp_path / "learned.json"))
        monkeypatch.setattr(eng, "BG_ACHIEVEMENTS", background)
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


def _count(platform, method, suffix):
    return sum(1 for m, p in platform.log if m == method and p.endswith(suffix))


ITEMS = [("chapter", 70), ("chapter", 71), ("review", 5), ("chapter", 72), ("test", 9)]


@pytest.mark.parametrize("background", [True, False])
def test_financial_job_finishes_with_and_without_background_achievements(run_financial, background):
    p = FakePlatform(_bank_truth, items=ITEMS)
    result = run_financial(p, background=background)
    assert result["status"] == "finished", result
    # كل درس (٣ دروس) لازم يوصله طلب الإنجازات، حتى لو اتبعت في الخلفية (الـ close بيستناه)
    assert _count(p, "POST", "/achievements/unlock") == 3


def test_curriculum_is_not_fetched_twice_at_start_or_end(run_financial):
    p = FakePlatform(_bank_truth, items=ITEMS)
    result = run_financial(p)
    assert result["status"] == "finished", result
    # لقطة البداية بتتستخدم للفة الأولى، وآخر لفة هي نفسها النتيجة: لفة لكل نشاط + لفة أخيرة فاضية = 5 + 1... بس الأولى مشتركة
    assert _count(p, "GET", "/curriculum") == len(ITEMS) + 1


def _slow(platform, normal=0.04, achievements=0.12):
    original = platform.handler

    def handler(request):
        time.sleep(achievements if request.url.path.endswith("/achievements/unlock") else normal)
        return original(request)

    platform.handler = handler
    return platform


def test_background_achievements_hide_their_latency(run_financial):
    items = [("chapter", 70 + i) for i in range(4)]
    t0 = time.perf_counter()
    assert run_financial(_slow(FakePlatform(_bank_truth, items=items)), background=True)["status"] == "finished"
    background_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    assert run_financial(_slow(FakePlatform(_bank_truth, items=items)), background=False)["status"] == "finished"
    sequential_elapsed = time.perf_counter() - t0
    # ٤ دروس × ٠٫١٢ث: الخلفية بتخفي ٣ منهم على الأقل (الأخير بيستناه الإغلاق)
    assert background_elapsed < sequential_elapsed - 0.2, (background_elapsed, sequential_elapsed)


def test_achievements_failure_is_ignored(run_financial):
    p = FakePlatform(_bank_truth, items=[("chapter", 70)])
    original = p.handler

    def broken(request):
        if request.url.path.endswith("/achievements/unlock"):
            return httpx.Response(500, json={"success": False, "error": {"message": "boom"}})
        return original(request)

    p.handler = broken
    assert run_financial(p, background=True)["status"] == "finished"


# ───────────────────────── Qureo: حفظ إجابات الدرس بالتوازي ─────────────────────────
class _LectureClient:
    """عميل Qureo بسيط لاختبار _solve_lecture: بيسجّل الحفظ، وبيحاكي زمن استجابة، وممكن يفشل في حفظ معيّن."""

    def __init__(self, latency=0.0, fail_first=(), fail_always=()):
        self.latency = latency
        self.saved = []                         # (talk_id, value) بترتيب الوصول الفعلي
        self.completed = []
        self.fail_first = set(fail_first)       # talk_ids أول محاولة ليهم بتفشل (محاكاة رفض التوازي)
        self.fail_always = set(fail_always)
        self._seen = set()
        self._lock = threading.Lock()
        self.lang = "python"

    def save_lecture_talk(self, talk_id, value):
        time.sleep(self.latency)
        with self._lock:
            first = talk_id not in self._seen
            self._seen.add(talk_id)
        if talk_id in self.fail_always or (first and talk_id in self.fail_first):
            raise qureo.PlatformError(409, f"/lecture-talks/{talk_id}")
        with self._lock:
            self.saved.append((talk_id, value))

    def complete_lecture(self, lecture_id):
        # الحفظ كله لازم يكون خلص قبل الإكمال
        self.completed.append((lecture_id, len(self.saved)))


def _content(n):
    return {
        "talks": [
            {
                "id": 1000 + i,
                "type": "choice_question",
                "body": {
                    "question": f"Zzzz qqqq unknown question number {i}",
                    "choices": [
                        {"label": "short", "value": "a"},
                        {"label": "a much longer option", "value": "b"},
                    ],
                },
            }
            for i in range(n)
        ]
    }


@pytest.fixture(autouse=True)
def _no_unmatched_log(monkeypatch):
    monkeypatch.setattr(matching, "record_unmatched", lambda *a, **k: None)
    monkeypatch.setattr(qureo.matching, "record_unmatched", lambda *a, **k: None)


def _solve(client, n=8, hook=None):
    events = []
    qureo._solve_lecture(
        client, 7, "Lecture", [], lambda k, t="": events.append(k), threading.Event(),
        qureo._Ctx(), prefetched=(_content(n), {}), confirm=False,
    )
    return events


def test_talk_saving_is_sequential_by_default():
    assert qureo.TALK_PARALLEL == 1
    client = _LectureClient()
    events = _solve(client, 5)
    assert [t for t, _ in client.saved] == [1000, 1001, 1002, 1003, 1004]
    assert events.count("q_ok") == 5
    assert client.completed == [(7, 5)]


def test_parallel_talk_saving_saves_everything_once_and_before_completion(monkeypatch):
    monkeypatch.setattr(qureo, "TALK_PARALLEL", 4)
    client = _LectureClient(latency=0.02)
    events = _solve(client, 9)
    assert sorted(t for t, _ in client.saved) == [1000 + i for i in range(9)]
    assert events.count("q_ok") == 9
    assert client.completed == [(7, 9)]


def test_parallel_talk_saving_is_faster(monkeypatch):
    sequential = _LectureClient(latency=0.05)
    t0 = time.perf_counter()
    _solve(sequential, 8)
    sequential_elapsed = time.perf_counter() - t0

    monkeypatch.setattr(qureo, "TALK_PARALLEL", 4)
    parallel = _LectureClient(latency=0.05)
    t0 = time.perf_counter()
    _solve(parallel, 8)
    parallel_elapsed = time.perf_counter() - t0
    assert parallel_elapsed < sequential_elapsed * 0.6


def test_parallel_failure_falls_back_to_sequential_retry(monkeypatch):
    monkeypatch.setattr(qureo, "TALK_PARALLEL", 4)
    client = _LectureClient(fail_first={1002, 1005})
    events = _solve(client, 8)
    assert sorted(t for t, _ in client.saved) == [1000 + i for i in range(8)]    # كلهم اتحفظوا مرة واحدة
    assert events.count("q_ok") == 8
    assert client.completed == [(7, 8)]


def test_parallel_failure_that_persists_is_raised(monkeypatch):
    monkeypatch.setattr(qureo, "TALK_PARALLEL", 4)
    client = _LectureClient(fail_always={1003})
    with pytest.raises(qureo.PlatformError):
        _solve(client, 6)
    assert client.completed == []                      # ما نكمّلش الدرس لو حفظ فشل فعلًا


def test_stop_during_parallel_saving_propagates(monkeypatch):
    monkeypatch.setattr(qureo, "TALK_PARALLEL", 3)
    client = _LectureClient()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(qureo.Stopped):
        qureo._solve_lecture(
            client, 7, "Lecture", [], None, cancel, qureo._Ctx(),
            prefetched=(_content(4), {}), confirm=False,
        )
