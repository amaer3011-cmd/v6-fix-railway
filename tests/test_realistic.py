"""اختبارات المحرّك الحقيقي (PlatformClient + run_job) ضد منصة وهمية واقعية (fake_platform)."""
import os, sys, tempfile, threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import api_engine as eng
from matching import parse_answer_file, resolve_answer, normalize_text
from fake_platform import FakePlatform, FIXTURE

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = parse_answer_file(ROOT / "answers.txt")
QTEXTS = [eng._clean(b["question"]["question_text"]) for b in FIXTURE["blocks"] if b.get("type") == "question"]


def bank_truth(text, option_texts):
    return resolve_answer(text, ENTRIES, option_texts)[0]


@pytest.fixture
def run(monkeypatch, tmp_path):
    def _run(platform, entries=None, cancel=None, hook_cb=None, password="x"):
        monkeypatch.setattr(eng, "_learned_singleton", eng.LearnedAnswers(tmp_path / "learned.json"))
        real = httpx.Client

        def factory(*a, **kw):
            kw.pop("http2", None)
            kw.pop("proxy", None)
            kw["transport"] = httpx.MockTransport(platform.handler)
            return real(*a, **kw)

        monkeypatch.setattr(eng.httpx, "Client", factory)
        monkeypatch.setattr(eng, "_sleep", lambda c, s: eng._check(c))     # بدون انتظار حقيقي
        events = []

        def hook(kind, text):
            events.append((kind, text))
            if hook_cb:
                hook_cb(kind, text)

        result = eng.run_job("user", password, entries or ENTRIES, hook, cancel)
        return result, events

    return _run


def platform(**kw):
    return FakePlatform(bank_truth, **kw)


# ───────────── المسار الطبيعي ─────────────
def test_full_flow_finishes(run):
    p = platform()
    r, ev = run(p)
    assert r["status"] == "finished", r
    assert p.logins == 1
    kinds = [k for k, _ in ev]
    assert "quiz_done" in kinds and kinds.count("done") == 3          # chapter, review, chapter


def test_plan_progress_is_monotonic(run):
    r, ev = run(platform())
    done = [int(t.split("|")[0]) for k, t in ev if k == "plan_progress"]
    assert done == sorted(done) and done[-1] == 4


# ───────────── إجابات غلط في الدرس ─────────────
def test_chapter_bank_wrong_platform_hides_correct_answer(run):
    """البنك غلط في سؤال، والمنصة (زي العينة الموثّقة) بترجّع correct_option_id=null: لازم نكمّل بالاستبعاد."""
    p = platform(wrong_in_bank={QTEXTS[3]}, items=[("chapter", 70), ("review", 5), ("chapter", 71)])
    r, _ = run(p)
    assert r["status"] == "finished", r


def test_chapter_bank_wrong_platform_reveals_correct_answer(run):
    p = platform(wrong_in_bank={QTEXTS[3]}, reveal_correct=True, items=[("chapter", 70)])
    r, _ = run(p)
    assert r["status"] == "finished", r
    assert r["learned"] >= 1


def test_chapter_wrong_answers_do_not_inflate_question_count(run):
    p = platform(wrong_in_bank={QTEXTS[3]}, items=[("chapter", 70)])
    r, ev = run(p)
    assert r["status"] == "finished"
    # الدرس فيه 5 أسئلة؛ السؤال الغلط اتعاد بالاستبعاد لكن بيتحسب مرة واحدة بس
    assert [k for k, _ in ev].count("q_ok") == 5
    assert r["retried"] >= 1 and r["requests"] > 0


def test_platform_locks_after_wrong_answer_does_not_loop(run):
    p = platform(wrong_in_bank={QTEXTS[3]}, lock_after_wrong=True, items=[("chapter", 70), ("review", 5)])
    r, _ = run(p)
    assert r["status"] == "stuck", r
    assert p.request_count < 80            # ما لفّش للأبد


# ───────────── الاختبارات ─────────────
def test_test_fails_then_learns_and_passes(run):
    p = platform(wrong_in_bank={QTEXTS[0], QTEXTS[1]}, items=[("test", 9)])
    r, _ = run(p)
    assert r["status"] == "finished", r
    assert len(p.tests[9]["attempts"]) == 2 and r["learned"] >= 2


def test_test_never_passes_ends_tests_pending(run):
    p = platform(items=[("test", 9)], pass_score=101)               # مستحيل النجاح
    r, _ = run(p)
    assert r["status"] == "tests_pending" and r["reason"] == "failed", r
    assert len(p.tests[9]["attempts"]) == eng.MAX_TEST_ATTEMPTS


def test_unmatched_policy_stop(run, monkeypatch):
    monkeypatch.setattr(eng, "UNMATCHED_POLICY", "stop")
    p = platform(items=[("test", 9)])
    r, _ = run(p, entries=parse_answer_file(ROOT / "tests" / "fixtures" / "empty_bank.txt"))
    assert r["status"] == "tests_pending" and r["reason"] == "unmatched", r


# ───────────── الشبكة والجلسة ─────────────
def test_session_expiry_relogin(run):
    p = platform(expire_after=6)
    r, _ = run(p)
    assert r["status"] == "finished", r
    assert p.logins >= 2


def test_flaky_503_is_retried(run):
    r, _ = run(platform(flaky_every=5))
    assert r["status"] == "finished", r


def test_cloudflare_block_stops_quickly_with_diagnostic(run):
    p = platform(cloudflare=True)
    r, _ = run(p)
    assert r["status"] == "error" and "حماية" in r["error"], r
    assert p.request_count <= eng.HTTP_RETRIES + 3


def test_bad_password(run):
    r, _ = run(platform(), password="bad")
    assert r["status"] == "login_failed"


def test_cancel_mid_run(run):
    cancel = threading.Event()
    seen = []

    def cb(kind, _t):
        seen.append(kind)
        if kind == "done" and seen.count("done") == 1:
            cancel.set()

    r, _ = run(platform(), cancel=cancel, hook_cb=cb)
    assert r["status"] == "stopped"


def test_unlock_lag_does_not_end_the_run_early(run):
    """بعد إكمال نشاط، المنهج ممكن يرجّع النشاط اللي بعده لسه مقفول لحظة (eventual consistency)."""
    p = platform(lag_unlock=True)
    r, _ = run(p)
    assert r["status"] == "finished", r


# ───────────── قياس الأداء ─────────────
def test_result_contains_request_timings(run):
    r, ev = run(platform())
    assert r["requests"] > 0 and "timings" in r
    assert any(k == "debug" and t.startswith("⏱️") for k, t in ev)


# ───────────── التسريع: إجابات الدرس بالتوازي ─────────────
@pytest.mark.parametrize("lesson_parallel", [1, 3])
def test_lesson_answers_same_outcome_sequential_or_parallel(run, monkeypatch, lesson_parallel):
    monkeypatch.setattr(eng, "LESSON_PARALLEL", lesson_parallel)
    r, events = run(platform())
    assert r["status"] == "finished", r
    assert sum(1 for kind, _t in events if kind == "q_ok") >= len(QTEXTS)


def test_parallel_lessons_stop_cleanly_on_cancel(run, monkeypatch):
    monkeypatch.setattr(eng, "LESSON_PARALLEL", 3)
    cancel = threading.Event()
    r, _ = run(platform(), cancel=cancel, hook_cb=lambda k, t: cancel.set() if k == "q_ok" else None)
    assert r["status"] == "stopped"
