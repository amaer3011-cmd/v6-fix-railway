import json, os, sys, tempfile, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LEARNED_FILE"] = str(Path(tempfile.mkdtemp()) / "l.json")
import api_engine as eng
from matching import parse_answer_file

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = parse_answer_file(ROOT / "answers.txt")
BLOCKS = json.loads((Path(__file__).parent / "fixtures" / "blocks70.json").read_text(encoding="utf-8"))
BLOCKS = BLOCKS.get("data", BLOCKS)


class Fake:
    """منصة وهمية: درس حقيقي (70) + مراجعة + اختبار؛ كل نشاط بيتفتح بعد اللي قبله."""
    def __init__(self, cancel=None):
        self.done = {"chapter": False, "review": False, "test": False}
        self.answered, self.calls, self.attempts = [], [], 0
        self.token_ok = True
    def close(self): pass
    def login(self, u, p):
        if p == "bad": raise eng.LoginFailed("x")
        return {"name": "t"}
    def curriculum(self):
        items = []
        prev = True
        for i, (t, k) in enumerate([("chapter", 70), ("review", 5), ("test", 9)]):
            items.append({"id": k, "item_type": t, "title": t, "item_order": i,
                          "is_completed": self.done[t], "is_unlocked": prev, "is_hidden": False})
            prev = self.done[t]
        n = sum(self.done.values())
        return [{"unit_order": 1, "title": "U", "is_accessible": True, "is_hidden": False,
                 "completed_items": n, "total_items": 3, "sections": [{"sort_order": 1, "items": items}]}]
    def chapter_blocks(self, cid): return BLOCKS
    def chapter_answer(self, cid, bid, oid):
        self.answered.append((bid, oid)); return {"is_correct": True}
    def chapter_complete(self, cid): self.done["chapter"] = True; return {"completed": True}
    def unlock_achievements(self): pass
    def review_items(self, rid): return {}
    def review_complete(self, rid): self.done["review"] = True; return {"completed": True}
    def test_start(self, tid):
        qs = [q for q in BLOCKS["blocks"] if q.get("type") == "question"]
        self.attempts += 1
        return {"question_responses": [{"test_question_id": 100 + i, "selected_option_id": None} for i in range(len(qs))],
                "questions": [{"id": 500 + i, "question_text": b["question"]["question_text"]} for i, b in enumerate(qs)],
                "options": {str(500 + i): b["options"] for i, b in enumerate(qs)}}
    def test_answer(self, tid, tq, oid): return True
    def test_submit(self, tid):
        ok = self.attempts >= 2
        if ok: self.done["test"] = True
        return {"response": {"passed": ok, "total_score": 100 if ok else 40}, "question_responses": [], "options": {}, "questions": []}


import pytest


@pytest.fixture(autouse=True)
def _restore_platform_client():
    original = eng.PlatformClient
    yield
    eng.PlatformClient = original


def run(monkey=Fake, pw="x"):
    eng.PlatformClient = monkey
    events = []
    return eng.run_job("u", pw, ENTRIES, lambda k, t: events.append(k)), events


def test_blocks_have_questions():
    assert any(b.get("type") == "question" for b in BLOCKS["blocks"])

def test_full_flow_finishes():
    r, ev = run()
    assert r["status"] == "finished", r
    assert "plan_progress" in ev and "quiz_done" in ev

def test_login_failed():
    r, _ = run(pw="bad")
    assert r["status"] == "login_failed"

def test_stop():
    ev = threading.Event(); ev.set()
    eng.PlatformClient = Fake
    r = eng.run_job("u", "x", ENTRIES, None, ev)
    assert r["status"] == "stopped"

def test_no_infinite_loop_when_never_completes():
    class Never(Fake):
        def chapter_complete(self, cid): return {"completed": False}
    r, _ = run(Never)
    assert r["status"] == "stuck"

def test_unescape_entities():
    assert eng._clean("قال &#34;نعم&#34;") == 'قال "نعم"'
