"""اختبارات قسم Qureo JavaScript: بنك الـ Markdown، اختيار الدورة، وحل دورة كاملة وهمية."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matching  # noqa: E402
import qureo_engine as qureo  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BANK = ROOT / "javascript_answers.md"


@pytest.fixture(scope="module")
def js_bank():
    return qureo.parse_js_answer_file(BANK)


# ───────────── البنك ─────────────

def test_js_bank_loads_and_drops_unanswerable_entries(js_bank):
    assert len(js_bank) >= 500
    answers = [answer for _q, answer in js_bank]
    assert not any(a.startswith(qureo._JS_NO_ANSWER) for a in answers)
    assert all(a.strip() for a in answers)


def test_js_bank_strips_lesson_prefixes_and_keeps_code(js_bank):
    questions = [q for q, _a in js_bank]
    assert not any(q.startswith(("(الدرس", "[اختبار", "مراجعة المستوى")) for q in questions)
    answer, _ = matching.match_answer('أنشئ برنامجًا يعرض "Good night" في وحدة التحكم.', js_bank)
    assert answer == 'console.log("Good night");'


def test_clean_js_answer_extracts_code_from_backticks_and_notes():
    assert qureo._clean_js_answer("`let a = 1;` والناتج `1`") == "let a = 1;"
    assert qureo._clean_js_answer("المحرر.") == "المحرر"
    assert qureo._clean_js_answer("Math.round(1.8) (وتكون قيمة التعبير 2).") == "Math.round(1.8)"
    assert qureo._clean_js_answer("let n = 5;\nconsole.log(n);") == "let n = 5;\nconsole.log(n);"


def test_looks_like_code_ignores_arabic_inside_strings():
    assert qureo._looks_like_code('console.log("مرحبا");')
    assert qureo._looks_like_code("<h1>عنوان</h1>")
    assert not qureo._looks_like_code("لا تغيّر الكود؛ الإجراء المطلوب هو تشغيله فقط")


# ───────────── توليد الكود ─────────────

def test_js_answer_code_as_is_and_copy_text(js_bank):
    assert qureo._answer_code(
        'أدخل البرنامج نفسه كما هو: `console.log("Hello");`', js_bank, "javascript"
    ) == 'console.log("Hello");'
    assert qureo._answer_code(
        'اطبع "hi" في وحدة التحكم. نسخ النص: hi', js_bank, "javascript"
    ) == 'console.log("hi");'


def test_js_answer_code_never_uses_python_print(js_bank):
    code = qureo._answer_code(
        "اجعل الكمبيوتر يحسب 15 ÷ 5 ويعرض نتيجة الحساب في وحدة التحكم.", js_bank, "javascript"
    )
    assert "print(" not in code and "console.log" in code


def test_js_answer_code_rejects_prose_answers():
    entries = matching.AnswerIndex([("Zzzz qqqq question here please", "لا شيء مناسب هنا إطلاقا")])
    with pytest.raises(qureo.UnmatchedAnswer):
        qureo._answer_code("Zzzz qqqq question here please", entries, "javascript")


# ───────────── الدخول واكتشاف الدورة ─────────────

def test_login_autodetects_javascript_course_id():
    class FakeHttp:
        def __init__(self):
            self.cookies = SimpleNamespace(set=lambda *a, **k: cookies.append(a[1]))

    cookies = []

    class FakeClient:
        course_key = "javascript"
        course_id = "javascript_short_ar"
        course_label = "JavaScript"
        http = FakeHttp()

        def _request(self, method, base_url, path, **kwargs):
            return {}

        def _get(self, path, **kwargs):
            if path == "/oauth2/refresh":
                return {}
            if path == "/oauth2/userinfo":
                return {
                    "available_courses": [{"content_id": "python_short_ar"}, {"content_id": "js_basic_ar_v2"}],
                    "selected_course": "js_basic_ar_v2",
                }
            if path == "/api/study/students/me":
                return {"course_code": "js_basic_ar_v2", "name": "s"}
            raise AssertionError(path)

    client = FakeClient()
    qureo.QureoClient.login(client, "id", "pw")
    assert client.course_id == "js_basic_ar_v2"
    assert cookies[-1] == "js_basic_ar_v2"


def test_login_rejects_account_without_javascript_course():
    class FakeClient:
        course_key = "javascript"
        course_id = "javascript_short_ar"
        course_label = "JavaScript"

        def _request(self, *a, **k):
            return {}

        def _get(self, path, **kwargs):
            if path == "/oauth2/userinfo":
                return {"available_courses": [{"content_id": "python_short_ar"}]}
            return {}

    with pytest.raises(qureo.PlatformError) as exc:
        qureo.QureoClient.login(FakeClient(), "id", "pw")
    assert exc.value.status == 403


def test_client_uses_course_profile():
    py = qureo.QureoClient()
    js = qureo.QureoClient(course="javascript")
    try:
        assert (py.course_id, py.lang) == ("python_short_ar", "python")
        assert js.lang == "javascript" and js.course_label == "JavaScript"
    finally:
        py.close()
        js.close()


# ───────────── حل دورة JS كاملة (منصة وهمية) ─────────────

def test_run_job_javascript_saves_js_code_and_labels_plan(monkeypatch, js_bank):
    answered = []

    class FakeJsClient:
        lang = "javascript"
        course_label = "JavaScript"

        def __init__(self, _cancel=None, course="python", course_id=None):
            assert course == "javascript"
            self.started = False

        def login(self, *_):
            return {"name": "student"}

        def course(self):
            return {"basic_section": {"id": 1}}

        def section(self, _):
            return {"chapters": [{"id": 1, "name": "Level 1", "seq": 1, "test_type": "review"}]}

        def section_progress(self, _):
            return []

        def chapter_access(self, _):
            return {}

        def chapter(self, _):
            return {"lectures": []}

        def lecture_progress(self, _):
            return []

        def review_result(self, _):
            return {"choice_results": [{"question_id": 1}], "description_results": [{"question_id": 2}],
                    "correct_count": 2} if len(answered) >= 2 else None

        def test_questions(self, _):
            return [
                {"question": {"id": 1, "type": "choice", "content": "كم عدد لغات البرمجة الموجودة في العالم؟"},
                 "choices": [{"id": 10, "content": "10 أنواع"}, {"id": 11, "content": "أكثر من 100 نوع"}]},
                {"question": {"id": 2, "type": "description",
                              "content": 'أنشئ برنامجًا يعرض "Good night" في وحدة التحكم.'},
                 "descriptions": [{"code": "", "editable": True}]},
            ]

        def test_state(self, _):
            return {"work_in_process": self.started}

        def start_test(self, _):
            self.started = True

        def answer_choice(self, qid, cid):
            answered.append((qid, cid))
            return {"result": {"correct": True}}

        def answer_description(self, qid, code):
            answered.append((qid, code))
            return {"result": {"correct": True}}

        def timing_report(self):
            return []

        def request_count(self):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(qureo, "QureoClient", FakeJsClient)
    events = []
    result = qureo.run_job("s", "p", js_bank, lambda k, t: events.append((k, t)), course="javascript")

    assert result["status"] == "finished", result
    assert answered == [(1, 11), (2, 'console.log("Good night");')]
    assert ("plan_todo", "JavaScript Qureo|0|1") in events



def test_run_job_infers_javascript_from_course_id_positional(monkeypatch):
    seen = {}

    class Fake:
        lang = "javascript"
        course_label = "JavaScript"

        def __init__(self, cancel=None, course="python", course_id=None):
            seen.update(course=course, course_id=course_id)

        def login(self, *_):
            raise qureo.LoginFailed()

        def timing_report(self):
            return []

        def request_count(self):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(qureo, "QureoClient", Fake)
    # نفس النداء اللي بيعمله bot.py: (…, headless, report_consent, course_id)
    result = qureo.run_job("s", "p", None, None, None, True, False, "javascript_short_ar")
    assert result["status"] == "login_failed"
    assert seen == {"course": "javascript", "course_id": "javascript_short_ar"}


def test_md_bank_loads_through_parse_answer_file():
    assert len(qureo.parse_answer_file(BANK)) >= 500


# ───────────── إعادة تشغيل على بيانات حقيقية من الـ capture ─────────────
import json  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "qureo_js_capture.json"


@pytest.fixture()
def js_mode():
    token = qureo._JS_MODE.set(True)
    yield
    qureo._JS_MODE.reset(token)


def test_js_clean_keeps_html_tags_and_strips_platform_markers(js_mode):
    assert qureo._clean("[[bdi]]<h1>Hello</h1>[[/bdi]]") == "<h1>Hello</h1>"
    assert qureo._clean("اختر [[bold]][[bdi]]num1[[/bdi]][[/bold]]") == "اختر num1"
    assert qureo._clean("برنامج:\n[[code]]let a = 1;\nconsole.log(a);[[/code]]") == "برنامج: let a = 1; console.log(a);"
    assert qureo._clean("مخطط [[img]]test32-3.png[[/img]]") == "مخطط"


def test_python_clean_is_unchanged_outside_js_mode():
    assert qureo._clean("[[bdi]]<b>x</b>[[/bdi]]") == "x"


def test_real_userinfo_picks_javascript_ar_not_trial():
    cookies = []

    class FakeClient:
        course_key = "javascript"
        course_id = "javascript_short_ar"      # قيمة قديمة غلط: لازم يتصحح
        course_label = "JavaScript"
        http = SimpleNamespace(cookies=SimpleNamespace(set=lambda *a, **k: cookies.append(a[1])))

        def _request(self, *a, **k):
            return {}

        def _get(self, path, **kw):
            if path == "/oauth2/userinfo":
                return {"available_courses": [
                    {"content_id": "javascript_trial_ar"}, {"content_id": "javascript_ar"},
                    {"content_id": "python_short_ar"}], "selected_course": None}
            if path == "/api/study/students/me":
                return {"course_code": "javascript_ar"}
            return {}

    client = FakeClient()
    qureo.QureoClient.login(client, "id", "pw")
    assert client.course_id == "javascript_ar"
    assert cookies[-1] == "javascript_ar"


def test_default_js_course_id_is_the_real_one():
    assert qureo.COURSES["javascript"]["id"] == "javascript_ar"


class _ReplayClient:
    """منصة وهمية بأسئلة حقيقية (فصل 90031): بتصحّح وتكشف الإجابة الصح زي المنصة الحقيقية."""

    lang = "javascript"
    course_label = "JavaScript"

    def __init__(self, chapter_id="90031"):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.items = data["questions"][chapter_id]
        self.reveal = data["reveal"]
        self.started = False
        self.last = {}     # question_id -> صح/غلط آخر إجابة
        self.log = []

    def test_questions(self, _):
        return self.items

    def test_state(self, _):
        return {"work_in_process": self.started}

    def start_test(self, _):
        self.started = True

    def answer_description(self, qid, code):
        ok = code.strip() == self.reveal["desc"][str(qid)].strip()
        self.last[qid] = ok
        self.log.append(("desc", qid, ok))
        res = {"correct": ok, "answer_code": code}
        if not ok:
            res["model_answer"] = self.reveal["desc"][str(qid)]
        return {"result": res}

    def answer_choice(self, qid, choice_id):
        right = self.reveal["choice"][str(qid)]
        ok = choice_id == right
        self.last[qid] = ok
        self.log.append(("choice", qid, ok))
        item = next(i for i in self.items if i["question"]["id"] == qid)
        return {"result": {"choice_id": choice_id, "correct": ok, "choices": [
            {"choice": c, "correct": c["id"] == right, "explanation": ""} for c in item["choices"]]}}

    def review_result(self, _):
        if len(self.last) < len(self.items):
            return None
        return {"correct_count": sum(self.last.values()),
                "choice_results": [{"question_id": q} for q, _ in self.last.items()]}


def test_review_replay_on_real_questions_completes_with_empty_bank_and_learns(js_mode, tmp_path):
    """بنك فاضي تمامًا: لازم الفصل يخلص 5/5 بالتعلّم من ردّ المنصة، والمتعلَّم يتحفظ في مخزن JS."""
    import api_engine
    learned = api_engine.LearnedAnswers(tmp_path / "js_learned.json")
    ctx = qureo._Ctx(learned)
    client = _ReplayClient()
    empty = matching.AnswerIndex([("Zzzz unrelated question text here", "x")])
    result = qureo._solve_review(
        client, {"id": 90031, "name": "الدوال (4)", "test_type": "review"}, empty, None, None, ctx
    )
    assert result["correct_count"] == 5
    assert ctx.learned_new >= 3
    assert len(learned) >= 3


def test_second_student_gets_everything_right_first_time_from_learned(js_mode, tmp_path):
    import api_engine
    learned = api_engine.LearnedAnswers(tmp_path / "js_learned.json")
    empty = matching.AnswerIndex([("Zzzz unrelated question text here", "x")])
    meta = {"id": 90031, "name": "الدوال (4)", "test_type": "review"}
    qureo._solve_review(_ReplayClient(), meta, empty, None, None, qureo._Ctx(learned))
    second = _ReplayClient()
    qureo._solve_review(second, meta, empty, None, None, qureo._Ctx(learned))
    assert [ok for _kind, _q, ok in second.log].count(False) <= 2   # الاختيارات المتعلَّمة صح من أول مرة


def test_js_learned_store_is_separate_from_python(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(qureo, "_js_learned", None)
    js_store = qureo.get_js_learned()
    assert js_store._path.name == "learned_js_answers.json"
    assert js_store._path != api_path(tmp_path)


def api_path(tmp_path):
    return tmp_path / "learned_answers.json"


# ───────────── الاختبار النهائي (test): المنصة ما بتكشفش الصح وقت الإجابة ─────────────
FINAL = ROOT / "tests" / "fixtures" / "qureo_js_final_test.json"


class _FinalTestClient:
    """زي المنصة الحقيقية (من الـ capture): ردّ الإجابة {} والنتيجة بعد آخر سؤال بس."""

    lang = "javascript"
    course_label = "JavaScript"

    def __init__(self):
        data = json.loads(FINAL.read_text(encoding="utf-8"))
        self.items, self.reveal = data["questions"], data["reveal"]
        self.started = False
        self.visible = False
        self.current = {}
        self.last_attempt = {}
        self.attempts = 0

    def test_questions(self, _):
        return self.items

    def test_state(self, _):
        return {"visible_result": self.visible, "work_in_process": self.started}

    def start_test(self, _):
        self.started, self.current = True, {}
        self.attempts += 1

    def _done(self, qid, ok):
        self.current[qid] = ok
        if len(self.current) == len(self.items):
            self.started, self.visible, self.last_attempt = False, True, dict(self.current)
        return {}

    def answer_test_choice(self, qid, choice_id):
        return self._done(qid, choice_id == self.reveal["choice"][str(qid)])

    def answer_test_description(self, qid, code):
        return self._done(qid, code.strip() == self.reveal["desc"][str(qid)].strip())

    def test_result(self, _):
        if not self.visible:
            return None
        results = {"choice_results": [], "description_results": [],
                   "correct_count": sum(self.last_attempt.values())}
        for item in self.items:
            qid = item["question"]["id"]
            if item["question"]["type"] == "choice":
                right = self.reveal["choice"][str(qid)]
                results["choice_results"].append({"question_id": qid, "choices": [
                    {"choice": c, "correct": c["id"] == right} for c in item["choices"]]})
            else:
                results["description_results"].append(
                    {"question_id": qid, "model_answer": self.reveal["desc"][str(qid)]})
        return results


def test_final_test_passes_on_second_attempt_with_empty_bank(js_mode, tmp_path):
    import api_engine
    ctx = qureo._Ctx(api_engine.LearnedAnswers(tmp_path / "l.json"))
    client = _FinalTestClient()
    empty = matching.AnswerIndex([("Zzzz unrelated question text here", "x")])
    result = qureo._solve_review(
        client, {"id": 90033, "name": "مراجعة الفهم", "test_type": "test"}, empty, None, None, ctx
    )
    assert result["correct_count"] == len(client.items) == 7
    assert client.attempts == 2          # محاولة أولى بتخمين + تانية بالإجابات المصححة


def test_js_final_test_reads_test_result_endpoint_first():
    calls = []

    class C:
        lang = "javascript"

        def _get(self, path, **kw):
            calls.append(path)
            if path.endswith("/test/result"):
                return {"histories": [], "result": {"correct_count": 3, "choice_results": []}}
            return None

    out = qureo.QureoClient.test_result(C(), 90033)
    assert calls == ["/api/study/students/chapters/90033/test/result"]
    assert out["correct_count"] == 3


def test_python_final_test_result_order_unchanged():
    calls = []

    class C:
        lang = "python"

        def _get(self, path, **kw):
            calls.append(path)
            return {"correct_count": 1}

    qureo.QureoClient.test_result(C(), 5)
    assert calls[0].endswith("/review/result")


# ───────────── دروس HTML/CSS الحقيقية (استبيان + أكواد متعددة) ─────────────
LECTURES = ROOT / "tests" / "fixtures" / "qureo_js_lectures_html_css.json"


def test_real_html_css_lectures_save_every_exercise_and_survey(js_mode):
    lectures = json.loads(LECTURES.read_text(encoding="utf-8"))
    saved = []

    class C:
        lang = "javascript"

        def __init__(self, content):
            self.content = content

        def lecture(self, _):
            return self.content

        def student_lecture(self, _):
            return {"talks": []}

        def save_lecture_talk(self, talk_id, value):
            saved.append((talk_id, value))

        def complete_lecture(self, _):
            saved.append(("complete", None))

        def start_lecture(self, _):
            pass

    empty = matching.AnswerIndex([("Zzzz unrelated question text here", "x")])
    total_try = total_survey = 0
    for lid, content in lectures.items():
        saved.clear()
        qureo._solve_lecture(C(content), int(lid), "t", empty, None, None, qureo._Ctx(), None, confirm=False)
        tries = [t for t in content["talks"] if t["type"] == "try"
                 and any(c.get("editable") for c in t["body"].get("codes", []))]
        surveys = [t for t in content["talks"] if t["type"] == "choice_survey"]
        total_try += len(tries)
        total_survey += len(surveys)
        assert len(saved) == len(tries) + len(surveys) + 1      # + complete
        assert saved[-1][0] == "complete"
        for _id, value in saved[:-1]:
            assert "try" in value or "choice_survey" in value
    assert total_try >= 10 and total_survey == 1


def test_try_talk_uses_editable_codes_language_and_model_answer(js_mode):
    lectures = json.loads(LECTURES.read_text(encoding="utf-8"))
    saved = {}

    class C:
        lang = "javascript"

        def lecture(self, _):
            return content

        def student_lecture(self, _):
            return {"talks": []}

        def save_lecture_talk(self, talk_id, value):
            saved[talk_id] = value

        def complete_lecture(self, _):
            pass

    for content in lectures.values():
        qureo._solve_lecture(C(), 1, "t", matching.AnswerIndex([("a b c", "x")]), None, None,
                             qureo._Ctx(), None, confirm=False)
    langs = {v["try"]["lang"] for v in saved.values() if "try" in v}
    assert "css" in langs and "html" in langs      # لغة الكود المعدَّل مش لغة الدورة
    assert all(v["try"]["answer"] for v in saved.values() if "try" in v)
