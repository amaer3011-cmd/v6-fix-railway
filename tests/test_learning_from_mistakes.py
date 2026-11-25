"""التعلّم من الأخطاء: حفظ الإجابات الغلط، الاستبعاد، وتجاهل إجابة البنك لو المنصة أثبتت إنها غلط."""
import json

import api_engine as eng
import qureo_engine as qureo

Q = "سؤال جديد تمامًا مش في أي بنك zzqq"
OPTS = ["أ", "ب أطول شوية", "جـ أطول خيار في السؤال كله", "د"]


def test_add_wrong_persists_and_keeps_through_add(tmp_path):
    path = tmp_path / "l.json"
    learned = eng.LearnedAnswers(path)
    assert learned.add_wrong(Q, OPTS[2]) is True
    assert learned.add_wrong(Q, OPTS[2]) is False          # مش بيتكرر
    assert learned.lookup(Q, OPTS) is None                 # مفيش إجابة صحيحة لسه
    assert len(learned) == 0                               # العدّاد للإجابات الصحيحة بس
    assert learned.wrong_indices(Q, OPTS) == {2}
    learned.add(Q, OPTS[1])                                # اتكشف الصح
    again = eng.LearnedAnswers(path)                       # إعادة تحميل من الملف
    assert again.lookup(Q, OPTS) == 1
    assert again.wrong_indices(Q, OPTS) == {2}
    assert len(again) == 1


def test_guess_skips_known_wrong():
    assert eng._guess_index(OPTS) == 2
    assert eng._guess_index(OPTS, {2}) == 1
    assert eng._guess_index(OPTS, {0, 1, 2, 3}) == 2       # الكل اتجرّب: ما نقفش
    assert qureo._guess_index(OPTS, {2}) == 1


def test_choose_changes_guess_after_wrong(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    assert eng._choose(Q, OPTS, [], learned) == (2, "guess")
    learned.add_wrong(Q, OPTS[2])
    assert eng._choose(Q, OPTS, [], learned) == (1, "guess")
    learned.add_wrong(Q, OPTS[1])
    assert eng._choose(Q, OPTS, [], learned) == (0, "guess")   # الباقي أ و د بنفس الطول: الأول


def test_wrong_beats_stale_learned_answer(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    learned.add(Q, OPTS[0])
    learned.add_wrong(Q, OPTS[0])                          # المنصة اتغيّرت وقالت إنها غلط
    assert learned.lookup(Q, OPTS) is None
    index, how = eng._choose(Q, OPTS, [], learned)
    assert how == "guess" and index != 0


class _Client:
    """منصة وهمية: الصح هو الخيار id=30 ومش بتكشفه."""
    def __init__(self, right):
        self.right, self.calls = right, []

    def chapter_answer(self, item_id, block_id, option_id):
        self.calls.append(option_id)
        return {"is_correct": option_id == self.right}


class _Cancel:
    def is_set(self): return False


def test_lesson_eliminates_and_learns(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    ctx = eng._Ctx(_Client(right=10), [], learned, None, _Cancel())
    block = {"id": 1, "question": {"question_text": Q},
             "options": [{"id": (10, 20, 30, 40)[i], "text": t, "sort_order": i}
                         for i, t in enumerate(OPTS)]}
    q_text, how, retries, added = eng._answer_block(ctx, {"id": 5}, block)
    assert ctx.client.calls == [30, 20, 10]                # الأطول الأول، بعدين الاستبعاد
    assert retries == 2 and added >= 1
    assert learned.lookup(Q, OPTS) == 0                    # اتعلّم الصح بالاستبعاد
    assert learned.wrong_indices(Q, OPTS) == {1, 2}
    # المرة الجاية: يجاوب صح من أول مرة
    ctx2 = eng._Ctx(_Client(right=10), [], learned, None, _Cancel())
    _, how2, retries2, _ = eng._answer_block(ctx2, {"id": 5}, block)
    assert how2 == "learned" and retries2 == 0 and ctx2.client.calls == [10]


def test_test_submission_remembers_wrong_when_correct_hidden(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    ctx = eng._Ctx(None, [], learned, None, _Cancel())
    submitted = {"question_responses": [{"test_question_id": 7, "is_correct": False}],
                 "questions": [{"id": 1, "question_text": Q}], "options": []}
    eng._learn_from_submission(ctx, submitted, {7: 1}, {}, chosen={7: (Q, OPTS[2])})
    assert learned.wrong_indices(Q, OPTS) == {2}


class _QClient:
    def __init__(self, right_id):
        self.right_id, self.sent = right_id, []

    def answer_choice(self, question_id, value):
        self.sent.append(value)
        return {"result": {"correct": value == self.right_id}}


def test_qureo_review_choice_eliminates(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    ctx = qureo._Ctx(learned)
    item = {"question": {"id": 9, "content": Q, "type": "choice"},
            "choices": [{"id": 100 + i, "content": t} for i, t in enumerate(OPTS)]}
    client = _QClient(right_id=100)
    qureo._answer_review_question(client, False, item, "t", [], {}, {}, ctx)
    assert client.sent[0] == 102 and client.sent[-1] == 100
    assert learned.lookup(Q, OPTS) == 0
    assert 2 in learned.wrong_indices(Q, OPTS)


# ───────────── سؤال كتابة كود مش في البنك: مفيش وقوف ─────────────
DESC_Q = 'أنشئ دالة zzqq بوسيطين. ① أنشئ دالة بالاسم "zzqq"'


class _DescClient:
    lang = "javascript"

    def __init__(self, right):
        self.right, self.sent = right, []

    def answer_description(self, question_id, code):
        self.sent.append(code)
        ok = code == self.right
        return {"result": {"correct": ok, "model_answer": "" if ok else ""}}


def test_unmatched_code_question_submits_placeholder_instead_of_stopping(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    ctx = qureo._Ctx(learned)
    item = {"question": {"id": 4, "content": DESC_Q, "type": "description"}, "descriptions": [{"code": ""}]}
    client = _DescClient(right="x")
    qureo._answer_review_question(client, False, item, "مراجعة الفهم", [], {}, {}, ctx)
    assert client.sent == ["console.log();"] and ctx.guessed == 1
    client.lang = "python"
    client.sent.clear()
    qureo._answer_review_question(client, False, item, "t", [], {}, {}, ctx)
    assert client.sent == ["print()"]


class _ReviewFlow:
    """مراجعة فيها سؤال كود واحد: أول محاولة غلط، والمنصة بتكشف الإجابة النموذجية في النتيجة."""
    lang = "javascript"
    MODEL = "function zzqq(a, b) { return a * b; }"

    def __init__(self):
        self.sent, self.started, self.last = [], 0, None

    def test_questions(self, cid):
        return [{"question": {"id": 4, "content": DESC_Q, "type": "description"}, "descriptions": [{"code": ""}]}]

    def test_state(self, cid):
        done = bool(self.sent) and self.sent[-1] == self.MODEL
        return {"work_in_process": self.started > 0 and not done,
                "visible_result": bool(self.sent),
                "last_answered_question_id": 4 if (self.sent and not done and False) else None}

    def start_test(self, cid):
        self.started += 1

    def answer_description(self, qid, code):
        self.sent.append(code)
        return {"result": {"correct": code == self.MODEL}}

    def review_result(self, cid):
        good = bool(self.sent) and self.sent[-1] == self.MODEL
        return {"correct_count": 1 if good else 0,
                "description_results": [{"question_id": 4, "model_answer": self.MODEL}],
                "choice_results": []}

    test_result = review_result


def test_review_flow_unmatched_code_learns_model_answer_and_finishes(tmp_path):
    learned = eng.LearnedAnswers(tmp_path / "l.json")
    ctx = qureo._Ctx(learned)
    client = _ReviewFlow()
    result = qureo._solve_review(client, {"id": 1, "name": "مراجعة الفهم", "test_type": "review"}, [], None, None, ctx)
    assert result["correct_count"] == 1
    assert client.sent[0] == "console.log();" and client.sent[-1] == _ReviewFlow.MODEL
    assert learned.get(DESC_Q) == _ReviewFlow.MODEL       # اتحفظت للحسابات الجاية
