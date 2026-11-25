import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matching
import qureo_engine as qureo


ROOT = Path(__file__).resolve().parent.parent


def test_qureo_answer_bank_preserves_and_extracts_code():
    entries = qureo.parse_answer_file(ROOT / "qureo_answers.txt")
    assert len(entries) >= 300
    answer, _source = matching.match_answer(
        'أنشئ برنامجا كما يلي. (1) اجعل الكمبيوتر يحسب 850-3000 ويطبع نتيجة الحساب في وحدة التحكم',
        entries,
    )
    assert answer == "print(3000-850)"


def test_extract_code_keeps_multiline_indentation():
    answer = (
        "الكود النهائي:\n"
        "score = 80\n"
        "if score >= 60:\n"
        '    print("Pass")\n'
        "else:\n"
        '    print("Fail")\n'
        "ملاحظة إضافية"
    )
    assert qureo.extract_code_answer(answer) == (
        'score = 80\nif score >= 60:\n    print("Pass")\nelse:\n    print("Fail")'
    )


def test_resolve_choice_uses_raw_text_before_normalized_variants():
    entries = qureo.parse_answer_file(ROOT / "qureo_answers.txt")
    options = ['print("Hello")', 'print("Hello"', 'print(Hello)']
    index = qureo.resolve_choice(
        'اختر البرنامج الصحيح لطباعة "Hello" في وحدة التحكم.',
        options,
        entries,
    )
    assert index == 0


def test_answer_code_patterns_handle_variable_assignment_and_print():
    assert qureo._answer_code(
        "في السطر الثاني أسند 100 إلى المتغير number",
        qureo.parse_answer_file(ROOT / "qureo_answers.txt"),
    ) == "number = 100"
    assert qureo._answer_code(
        "في السطر الثالث اطبع قيمة المتغير number",
        qureo.parse_answer_file(ROOT / "qureo_answers.txt"),
    ) == "print(number)"


def test_answer_code_patterns_handle_test_comprehension():
    entries = qureo.parse_answer_file(ROOT / "qureo_answers.txt")
    assert qureo._answer_code(
        'اطبع "hello" في وحدة التحكم. نسخ النص: hello',
        entries,
    ) == 'print("hello")'
    assert qureo._answer_code(
        "أسند 5 إلى num ثم اطبع قيمة num",
        entries,
    ) == "num = 5\nprint(num)"


def test_resolve_choice_evaluates_true_false_comparison():
    entries = qureo.parse_answer_file(ROOT / "qureo_answers.txt")
    index = qureo.resolve_choice(
        'math = 80\nprint(math == 80)',
        ["True", "False"],
        entries,
    )
    assert index == 0


def test_solve_lecture_handles_choice_question_talk(monkeypatch):
    class FakeChoiceClient:
        def __init__(self):
            self.saved = []
            self.completed = None

        def lecture(self, lecture_id):
            return {
                "lecture": {"chapter_id": 1},
                "talks": [
                    {
                        "id": 1008,
                        "type": "choice_question",
                        "body": {
                            "question": "لنجرِ محادثةً مع الجميع.",
                            "choices": [
                                {"label": "كودي هو أيضًا AI", "value": "1"},
                                {"label": "سيرد كودي أيضًا.", "value": "2"},
                            ],
                        },
                    }
                ],
            }

        def student_lecture(self, lecture_id):
            return None

        def start_lecture(self, lecture_id):
            self.started = True

        def save_lecture_talk(self, talk_id, value):
            self.saved.append((talk_id, value))

        def complete_lecture(self, lecture_id):
            self.completed = lecture_id

        def lecture_progress(self, chapter_id):
            return [{"lecture_id": 100, "completed_at": "done"}]

    entries = matching.AnswerIndex(
        [
            ("لنجرِ محادثةً مع الجميع.", "كودي هو أيضًا AI"),
        ]
    )
    client = FakeChoiceClient()
    qureo._solve_lecture(client, 100, "Introduction", entries, None, None)
    assert client.saved == [(1008, {"choice_survey": {"value": "1"}})]
    assert client.completed == 100


def test_login_refresh_accepts_redirected_non_json(monkeypatch):
    class RefreshResponse:
        status_code = 200
        content = b"<html>redirected</html>"
        headers = {}

        def json(self):
            raise ValueError

    class FakeHttp:
        def __init__(self, *args, **kwargs):
            self.cookies = {}

        def request(self, _method, url, **_kwargs):
            return RefreshResponse()

        def close(self):
            pass

    monkeypatch.setattr(qureo.httpx, "Client", FakeHttp)
    client = qureo.QureoClient()
    assert client._get("/oauth2/refresh", expect_json=False) == {}


def test_student_lecture_404_is_treated_as_not_started(monkeypatch):
    class NotFoundResponse:
        status_code = 404
        content = b'{"error":"not_found"}'
        headers = {}

        def json(self):
            return {"error": "not_found"}

    class FakeHttp:
        def __init__(self, *args, **kwargs):
            self.cookies = {}

        def request(self, _method, _url, **_kwargs):
            return NotFoundResponse()

        def close(self):
            pass

    monkeypatch.setattr(qureo.httpx, "Client", FakeHttp)
    client = qureo.QureoClient()
    assert client.student_lecture(511001010) is None


def test_qureo_login_falls_back_to_portal_userinfo(monkeypatch):
    class FakeClient:
        def _request(self, method, base_url, path, **kwargs):
            if base_url == qureo.PORTAL_URL and path == "/oauth2/password-login":
                return {}
            if base_url == qureo.PORTAL_URL and path == "/oauth2/userinfo":
                return {"available_courses": [{"content_id": "python_short_ar"}], "selected_course": None}
            raise AssertionError("unexpected portal request")

        def _get(self, path, **kwargs):
            if path == "/oauth2/refresh":
                return {} if kwargs.get("expect_json") is False else {"unexpected": True}
            if path == "/oauth2/userinfo":
                raise qureo.UnknownStructure("non-JSON")
            if path == "/api/study/students/me":
                return {"course_code": "python_short_ar", "name": "student"}
            raise AssertionError(path)

    client = FakeClient()
    try:
        qureo.QureoClient.login(client, "student-id", "password")
    except Exception as exc:
        raise AssertionError(f"portal userinfo fallback should succeed: {exc}") from exc


def test_run_job_solves_lesson_and_review(monkeypatch):
    class FakeQureoClient:
        def __init__(self, _cancel=None):
            self.started = False
            self.lecture_done = False
            self.answered = []

        def login(self, _username, _password):
            return {"name": "student"}

        def course(self):
            return {"basic_section": {"id": 10}}

        def section(self, _section_id):
            return {
                "chapters": [
                    {"id": 1, "name": "Introduction", "seq": 1, "test_type": "review"}
                ]
            }

        def section_progress(self, _section_id):
            return []

        def chapter_access(self, _chapter_id):
            return {}

        def chapter(self, _chapter_id):
            return {
                "lectures": [{"id": 100, "seq": 1, "title": "Print"}]
            }

        def lecture_progress(self, _chapter_id):
            return [{"lecture_id": 100, "completed_at": "done"}] if self.lecture_done else []

        def lecture(self, _lecture_id):
            return {"lecture": {"chapter_id": 1}, "talks": []}

        def student_lecture(self, _lecture_id):
            return {"talks": []}

        def complete_lecture(self, _lecture_id):
            self.lecture_done = True

        def review_result(self, _chapter_id):
            if len(self.answered) < 2:
                return None
            return {
                "choice_results": [{"question_id": 11}],
                "description_results": [{"question_id": 12}],
                "correct_count": 2,
            }

        def test_questions(self, _chapter_id):
            return [
                {
                    "question": {"id": 11, "content": "Choose Alpha", "type": "choice"},
                    "choices": [
                        {"id": 111, "content": "Alpha"},
                        {"id": 112, "content": "Beta"},
                    ],
                },
                {
                    "question": {"id": 12, "content": "Print hello", "type": "description"},
                    "descriptions": [{"code": "", "editable": True}],
                },
            ]

        def test_state(self, _chapter_id):
            return {"work_in_process": self.started}

        def start_test(self, _chapter_id):
            self.started = True

        def answer_choice(self, question_id, choice_id):
            self.answered.append((question_id, choice_id))
            return {"result": {"correct": True}}

        def answer_description(self, question_id, answer_code):
            self.answered.append((question_id, answer_code))
            return {"result": {"correct": True}}

        def timing_report(self):
            return []

        def request_count(self):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(qureo, "QureoClient", FakeQureoClient)
    entries = matching.AnswerIndex(
        [("Choose Alpha", "Alpha"), ("Print hello", 'print("Hello")')]
    )
    events = []
    result = qureo.run_job("student", "password", entries, lambda kind, text: events.append(kind))

    assert result["status"] == "finished"
    assert result["questions"] == 2
    assert result["activities"] == 2
    assert "done" in events
    rows = result["chapters"]                       # تقرير الفصول: اسم + دروس اتحلّت + حالة
    assert len(rows) == 1 and rows[0]["name"] and rows[0]["lectures"] == 1
    assert rows[0]["was_done"] is False


# ───────────────────────── v6.2: جلسة / تعلّم / تخمين ─────────────────────────
import json

import pytest

import api_engine


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def _scripted_http(monkeypatch, responses):
    calls = []

    class FakeHttp:
        def __init__(self, *args, **kwargs):
            self.cookies = {}

        def request(self, method, url, **_kwargs):
            calls.append((method, url))
            return responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(qureo.httpx, "Client", FakeHttp)
    return calls


def test_expired_session_relogs_in_once_and_retries(monkeypatch):
    calls = _scripted_http(monkeypatch, [_Resp(401, {}), _Resp(200, {"ok": 1})])
    client = qureo.QureoClient()
    client._creds = ("student", "secret")
    logins = []
    monkeypatch.setattr(client, "login", lambda *creds: logins.append(creds))
    assert client._get("/api/study/anything") == {"ok": 1}
    assert logins == [("student", "secret")]
    assert len(calls) == 2


def test_wrong_password_does_not_loop_on_401(monkeypatch):
    calls = _scripted_http(monkeypatch, [_Resp(401, {})] * 3)
    client = qureo.QureoClient()
    client._creds = ("student", "secret")   # حتى لو في جلسة قديمة، الدخول نفسه ما يعيدش
    with pytest.raises(qureo.LoginFailed):
        client.login("student", "wrong")
    assert len(calls) == 1


class _ReviewClient:
    """مراجعة وهمية بسؤال واحد؛ `wrong_first` بيخلّي المنصة ترفض أول إجابة وتكشف الصح."""

    def __init__(self, question, choices=None, wrong_first=False, model_answer=None):
        self.question = question
        self.choices = choices
        self.wrong_first = wrong_first
        self.model_answer = model_answer
        self.answered = []
        self.started = False

    def test_questions(self, _cid):
        item = {"question": self.question}
        if self.choices is not None:
            item["choices"] = self.choices
        return [item]

    def test_state(self, _cid):
        return {"work_in_process": self.started}

    def start_test(self, _cid):
        self.started = True

    def _reply(self, value, correct_choice_id=None):
        self.answered.append((self.question["id"], value))
        if self.wrong_first and len(self.answered) == 1:
            result = {"correct": False}
            if correct_choice_id is not None:
                result["choices"] = [{"correct": True, "choice": {"id": correct_choice_id}}]
            if self.model_answer:
                result["model_answer"] = self.model_answer
            return {"result": result}
        return {"result": {"correct": True}}

    def answer_choice(self, _qid, choice_id):
        return self._reply(choice_id, correct_choice_id=111)

    def answer_test_choice(self, _qid, choice_id):
        return self._reply(choice_id, correct_choice_id=111)

    def answer_description(self, _qid, code):
        return self._reply(code)

    def review_result(self, _cid):
        return {"choice_results": [], "description_results": [], "correct_count": 1} \
            if self.answered else None

    test_result = review_result


_OTHER = matching.AnswerIndex([("سؤال مختلف تماما عن أي شيء آخر هنا", "إجابة")])
_CHOICES = [{"id": 111, "content": "Alpha"}, {"id": 112, "content": "Beta is longer"}]


def test_unmatched_review_choice_guesses_then_learns_platform_answer(tmp_path):
    learned = api_engine.LearnedAnswers(tmp_path / "learned.json")
    ctx = qureo._Ctx(learned)
    client = _ReviewClient(
        {"id": 11, "content": "Zzzz qqqq unknown wording", "type": "choice"},
        _CHOICES, wrong_first=True,
    )
    qureo._solve_review(
        client, {"id": 1, "name": "Review", "test_type": "review"}, _OTHER, None, None, ctx
    )
    assert client.answered == [(11, 112), (11, 111)]     # خمّن الأطول، وبعدين الصح اللي كشفته المنصة
    assert (ctx.guessed, ctx.retried, ctx.learned_new) == (1, 1, 1)
    assert learned.lookup("Zzzz qqqq unknown wording", ["Alpha", "Beta is longer"]) == 0
    assert json.loads((tmp_path / "learned.json").read_text(encoding="utf-8"))


def test_learned_choice_beats_guessing_next_time(tmp_path):
    learned = api_engine.LearnedAnswers(tmp_path / "learned.json")
    learned.add("Zzzz qqqq unknown wording", "Alpha")
    ctx = qureo._Ctx(learned)
    client = _ReviewClient(
        {"id": 11, "content": "Zzzz qqqq unknown wording", "type": "choice"}, _CHOICES
    )
    qureo._solve_review(
        client, {"id": 1, "name": "Review", "test_type": "review"}, _OTHER, None, None, ctx
    )
    assert client.answered == [(11, 111)]
    assert ctx.guessed == 0


def test_final_test_guesses_unmatched_choice_by_default_and_recovers():
    """السلوك الحالي: الاختبار النهائي كمان بيخمّن سؤال الاختيار اللي مش في البنك والمنصة بتكشف الصح."""
    ctx = qureo._Ctx()
    client = _ReviewClient(
        {"id": 11, "content": "Zzzz qqqq unknown wording", "type": "choice"}, _CHOICES, wrong_first=True
    )
    qureo._solve_review(
        client, {"id": 1, "name": "Final", "test_type": "test"}, _OTHER, None, None, ctx
    )
    assert client.answered == [(11, 112), (11, 111)]
    assert ctx.guessed == 1


def test_unmatched_policy_stop_makes_final_test_stop_without_answering(monkeypatch):
    """UNMATCHED_POLICY=stop: الحل يقف عند السؤال اللي مش في البنك بدل التخمين، ومفيش إجابة تتبعت."""
    monkeypatch.setattr(qureo, "UNMATCHED_POLICY", "stop")
    ctx = qureo._Ctx()
    client = _ReviewClient(
        {"id": 11, "content": "Zzzz qqqq unknown wording", "type": "choice"}, _CHOICES
    )
    with pytest.raises(qureo.UnmatchedAnswer):
        qureo._solve_review(
            client, {"id": 1, "name": "Final", "test_type": "test"}, _OTHER, None, None, ctx
        )
    assert client.answered == []


def test_description_uses_learned_code_and_learns_model_answer(tmp_path):
    learned = api_engine.LearnedAnswers(tmp_path / "learned.json")
    learned.add("Zzzz qqqq write something", "print(1)")
    ctx = qureo._Ctx(learned)
    client = _ReviewClient({"id": 12, "content": "Zzzz qqqq write something", "type": "description"})
    qureo._solve_review(
        client, {"id": 1, "name": "Review", "test_type": "review"}, _OTHER, None, None, ctx
    )
    assert client.answered == [(12, "print(1)")]

    client = _ReviewClient(
        {"id": 13, "content": "Zzzz another one", "type": "description"},
        wrong_first=True, model_answer="x = 5\nprint(x)",
    )
    ctx = qureo._Ctx(learned)
    learned.add("Zzzz another one", "print(0)")   # متعلَّمة غلط، المنصة بتصحّحها
    qureo._solve_review(
        client, {"id": 1, "name": "Review", "test_type": "review"}, _OTHER, None, None, ctx
    )
    assert client.answered == [(13, "print(0)"), (13, "x = 5\nprint(x)")]
    assert learned.get("Zzzz another one") == "x = 5\nprint(x)"
    assert ctx.retried == 1


def test_answer_code_accepts_curly_and_guillemet_quotes():
    assert qureo._answer_code("اطبع “Hi there” في وحدة التحكم", _OTHER) == 'print("Hi there")'
    assert qureo._answer_code("اطبع «Hi» في وحدة التحكم", _OTHER) == 'print("Hi")'


def test_lecture_code_talk_without_bank_answer_saves_starter_code_instead_of_stopping():
    class C:
        saved = []
        def lecture(self, _id):
            return {"lecture": {"chapter_id": 1}, "talks": [{
                "id": 5, "type": "try", "question": "Zzzz لا يوجد في البنك",
                "codes": [{"editable": True, "code": "print('x')", "model_answer": ""}]}]}
        def student_lecture(self, _id): return {"talks": []}
        def save_lecture_talk(self, tid, v): self.saved.append((tid, v))
        def complete_lecture(self, _id): pass
        def lecture_progress(self, _c): return [{"lecture_id": 100, "completed_at": "x"}]
    ctx = qureo._Ctx()
    c = C()
    qureo._solve_lecture(c, 100, "t", _OTHER, None, None, ctx)
    assert c.saved == [(5, {"try": {"lang": "python", "answer": "print('x')"}})]
    assert ctx.guessed == 1


def test_unmatched_error_carries_question():
    with pytest.raises(qureo.UnmatchedAnswer) as info:
        qureo._answer_code("Zzzz سؤال غريب جدا", _OTHER)
    assert "Zzzz" in info.value.question


def test_prefetch_loads_in_parallel_and_ignores_failures():
    class C:
        def lecture(self, lid):
            if lid == 3:
                raise qureo.PlatformError(403, "locked")
            return {"id": lid}

        def student_lecture(self, lid):
            return {"talks": []}

    out = qureo._prefetch_lectures(C(), [1, 2, 3], None)
    assert out == {1: ({"id": 1}, {"talks": []}), 2: ({"id": 2}, {"talks": []})}   # 3 بيتجلب وقت حلّه


def test_prefetch_propagates_stop():
    import threading
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(qureo.Stopped):
        qureo._prefetch_lectures(object(), [1, 2], cancel)


def test_confirm_lectures_reports_missing_ones():
    class C:
        def lecture_progress(self, _cid):
            return [{"lecture_id": 1, "completed_at": "x"}, {"lecture_id": 2}]

    qureo._confirm_lectures(C(), 9, [1], "ch")
    with pytest.raises(qureo.UnknownStructure, match="2"):
        qureo._confirm_lectures(C(), 9, [1, 2], "ch")


def test_solve_lecture_uses_prefetched_content_without_refetching():
    class C:
        saved = []
        def lecture(self, _id): raise AssertionError("should not refetch")
        def student_lecture(self, _id): raise AssertionError("should not refetch")
        def complete_lecture(self, _id): pass

    qureo._solve_lecture(
        C(), 7, "t", _OTHER, None, None, None,
        ({"lecture": {"chapter_id": 1}, "talks": []}, {"talks": []}), confirm=False,
    )
