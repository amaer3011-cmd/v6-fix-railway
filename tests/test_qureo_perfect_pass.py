"""مرحلة «100% في كل مراجعة/اختبار»: حساب مكتمل بدرجة ناقصة يتعاد لحد ما يبقى كامل."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import matching
import qureo_engine as qureo

ENTRIES = matching.AnswerIndex([("Choose Alpha", "Alpha"), ("Print hello", 'print("Hello")')])


def _make_client(initial_correct, can_retake=True):
    class Fake:
        instances = []

        def __init__(self, _cancel=None, *a, **k):
            self.initial = initial_correct
            self.started = False
            self.answered = []
            self.starts = 0
            Fake.instances.append(self)

        def login(self, *_):
            return {"name": "s"}

        def course(self):
            return {"basic_section": {"id": 10}}

        def section(self, _):
            return {"chapters": [{"id": 1, "name": "Introduction", "seq": 1, "test_type": "review"}]}

        def section_progress(self, _):
            return [{"chapter_id": 1, "completed_at": "done"}]          # الفصل مكتمل بالفعل

        def chapter_access(self, _):
            return {}

        def review_result(self, _):
            correct = 2 if len(self.answered) >= 2 else self.initial
            return {"choice_results": [{"question_id": 11}], "description_results": [{"question_id": 12}],
                    "correct_count": correct}

        def test_questions(self, _):
            return [
                {"question": {"id": 11, "content": "Choose Alpha", "type": "choice"},
                 "choices": [{"id": 111, "content": "Alpha"}, {"id": 112, "content": "Beta"}]},
                {"question": {"id": 12, "content": "Print hello", "type": "description"},
                 "descriptions": [{"code": "", "editable": True}]},
            ]

        def test_state(self, _):
            return {"visible_result": not self.started, "work_in_process": self.started}

        def start_test(self, _):
            if not can_retake:
                raise qureo.PlatformError(409, "test already completed", "/test")
            self.started = True
            self.starts += 1

        def answer_choice(self, qid, cid):
            self.answered.append((qid, cid))
            return {"result": {"correct": True}}

        def answer_description(self, qid, code):
            self.answered.append((qid, code))
            return {"result": {"correct": True}}

        def timing_report(self):
            return []

        def request_count(self):
            return 0

        def close(self):
            pass

    return Fake


def _run(monkeypatch, fake):
    monkeypatch.setattr(qureo, "QureoClient", fake)
    return qureo.run_job("s", "p", ENTRIES, lambda k, t: None)


def test_completed_but_imperfect_chapter_is_redone_to_100(monkeypatch):
    fake = _make_client(initial_correct=1)
    result = _run(monkeypatch, fake)
    assert result["status"] == "finished"
    assert result["perfect_fixed"] == ["Introduction"] and result["not_perfect"] == []
    assert result["precompleted"] is False                  # اتعمل شغل فعلي: مش «مكتمل قبل كده»
    assert fake.instances[0].starts == 1 and len(fake.instances[0].answered) == 2


def test_already_perfect_account_is_left_alone(monkeypatch):
    fake = _make_client(initial_correct=2)
    result = _run(monkeypatch, fake)
    assert result["status"] == "finished" and result["precompleted"] is True
    assert result["perfect_fixed"] == [] and fake.instances[0].starts == 0   # مفيش إعادة ولا لمس


def test_failed_retake_is_reported_not_fatal(monkeypatch):
    fake = _make_client(initial_correct=1, can_retake=False)
    result = _run(monkeypatch, fake)
    assert result["status"] == "finished"                   # الحل ما بيتوقفش
    assert result["not_perfect"] == ["Introduction"] and result["perfect_fixed"] == []


def test_perfect_pass_can_be_disabled(monkeypatch):
    monkeypatch.setattr(qureo, "PERFECT_PASS", False)
    fake = _make_client(initial_correct=1)
    result = _run(monkeypatch, fake)
    assert result["perfect_fixed"] == [] and fake.instances[0].starts == 0


def test_run_job_counts_work_done_in_perfect_pass(monkeypatch):
    fake = _make_client(initial_correct=1)
    result = _run(monkeypatch, fake)
    assert result["questions"] == 2 and result["activities"] == 1


def test_perfect_note_for_user_message():
    import bot
    assert bot.perfect_note({}) == ""
    text = bot.perfect_note({"perfect_fixed": ["A", "B"], "not_perfect": ["<C>"]})
    assert "٢" in text and "100%" in text and "&lt;C&gt;" in text


# ─── تقرير الفصول في النتيجة والرسالة النهائية ───

def test_result_lists_chapters_with_scores_and_redo(monkeypatch):
    fake = _make_client(initial_correct=1)
    result = _run(monkeypatch, fake)
    rows = result["chapters"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Introduction" and row["was_done"] is True
    assert row["state"] == "fixed" and row["before"] == 1
    assert row["correct"] == 2 and row["total"] == 2
    assert row["closed"] is True                       # الفصل مسجّل مكتمل بعد الإعادة


def test_result_marks_perfect_and_not_perfect(monkeypatch):
    perfect = _run(monkeypatch, _make_client(initial_correct=2))
    assert perfect["chapters"][0]["state"] == "perfect"
    assert (perfect["chapters"][0]["correct"], perfect["chapters"][0]["total"]) == (2, 2)
    stuck = _run(monkeypatch, _make_client(initial_correct=1, can_retake=False))
    assert stuck["chapters"][0]["state"] == "not_perfect"
    assert (stuck["chapters"][0]["correct"], stuck["chapters"][0]["total"]) == (1, 2)


def test_chapters_report_text():
    import bot
    assert bot.chapters_report({}) == ""
    text = bot.chapters_report({"chapters": [
        {"name": "Intro", "lectures": 6, "was_done": False, "state": "perfect", "correct": 8, "total": 8, "closed": True},
        {"name": "Loops <b>", "lectures": 0, "was_done": True, "state": "fixed", "before": 3, "correct": 8, "total": 8, "closed": True},
        {"name": "Final", "lectures": 0, "was_done": True, "state": "not_perfect", "correct": 5, "total": 8, "closed": False},
    ]})
    assert "Intro" in text and "٦ درس" in text and "٨/٨" in text
    assert "🎯" in text and "٣/٨ ← ٨/٨" in text
    assert "Loops &lt;b&gt;" in text                      # escape
    assert "⚠️" in text and "لسه مش مقفول" in text


def test_chapters_report_is_capped_for_telegram():
    import bot
    rows = [{"name": f"Chapter {i} " + "x" * 40, "lectures": 3, "state": "perfect", "correct": 9, "total": 10}
            for i in range(60)]
    text = bot.chapters_report({"chapters": rows})
    assert len(text) < 2500 and "فصل آخر" in text


# ─── الألماس (diamond) وأحسن نتيجة (best_correct_count) — مبني على capture حقيقي ───

def _diamond_client(best, latest, diamond_start=15, can_retake=True):
    """منصة وهمية بنفس شكل Qureo الحقيقي: section_progress فيه best_correct_count/diamond،
    ورد /diamond رقم، وrequest الـresult فيه diamond."""
    base = _make_client(initial_correct=latest, can_retake=can_retake)

    class Fake(base):
        def section(self, _):
            return {"chapters": [{"id": 1, "name": "Intro", "seq": 1, "test_type": "review", "question_count": 2}]}

        def _diamonds(self):
            return 5 if self.started and len(self.answered) >= 2 else 2

        def section_progress(self, _):
            best_now = 2 if self.started and len(self.answered) >= 2 else best
            return [{"chapter_id": 1, "completed_at": "done", "best_correct_count": best_now,
                     "diamond": 5 if best_now >= 2 else 2}]

        def review_result(self, _):
            data = super().review_result(_)
            data["diamond"] = self._diamonds()
            return data

        def course_diamond(self):
            return diamond_start + (3 if self.started and len(self.answered) >= 2 else 0)

    return Fake


def test_best_perfect_chapter_is_not_redone_even_if_last_attempt_was_worse(monkeypatch):
    fake = _diamond_client(best=2, latest=1)                  # أحسن نتيجة 2/2 حتى لو آخر محاولة 1
    result = _run(monkeypatch, fake)
    assert result["perfect_fixed"] == [] and fake.instances[0].starts == 0
    assert result["chapters"][0]["state"] == "perfect"
    assert result["chapters"][0]["diamond"] == 5


def test_weak_best_is_redone_and_diamonds_are_reported(monkeypatch):
    fake = _diamond_client(best=1, latest=1)
    result = _run(monkeypatch, fake)
    row = result["chapters"][0]
    assert result["perfect_fixed"] == ["Intro"] and fake.instances[0].starts == 1
    assert (row["before"], row["correct"], row["total"]) == (1, 2, 2)
    assert row["diamond"] == 5
    assert (result["diamond_before"], result["diamond_after"]) == (15, 18)


def test_missing_diamond_endpoint_is_not_fatal(monkeypatch):
    class NoDiamond(_diamond_client(best=1, latest=1)):
        def course_diamond(self):
            raise qureo.PlatformError(404, "/diamond")
    result = _run(monkeypatch, NoDiamond)
    assert result["status"] == "finished" and result["diamond_after"] is None


def test_diamonds_line_text():
    import bot
    assert bot.diamonds_line({}) == ""
    assert "١٥" in bot.diamonds_line({"diamond_before": 15, "diamond_after": 17}) and "+٢" in bot.diamonds_line(
        {"diamond_before": 15, "diamond_after": 17})
    assert "إجمالي الألماس" in bot.diamonds_line({"diamond_before": 17, "diamond_after": 17})
    text = bot.chapters_report({"chapters": [{"name": "A", "state": "perfect", "correct": 6, "total": 6, "diamond": 5}],
                                "diamond_before": 10, "diamond_after": 15})
    assert "💎٥" in text and "+٥" in text


def test_real_client_reads_bare_number_diamond_endpoint():
    """Qureo بترجّع رقم لوحده (مش dict) لـ /courses/<id>/diamond — مش لازم يتحسب «شكل غير متوقع»."""
    import httpx
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=17)

    client = qureo.QureoClient(None)
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.course_diamond() == 17
    assert seen == [f"/api/study/students/courses/{client.course_id}/diamond"]
    client.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=17)))
    with __import__("pytest").raises(qureo.UnknownStructure):         # باقي الـendpoints لسه بتتحقق من الشكل
        client.course()
