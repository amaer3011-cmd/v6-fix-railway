"""قراءة بيانات الفصل (chapter + lecture_progress) في Qureo بالتوازي بدون ما تتغيّر النتيجة أو ترتيب الأخطاء."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import qureo_engine as qureo


class _Client:
    def __init__(self, delay=0.0, chapter_exc=None, states_exc=None):
        self.delay, self.chapter_exc, self.states_exc = delay, chapter_exc, states_exc
        self.threads = set()

    def chapter(self, chapter_id):
        self.threads.add(threading.get_ident())
        time.sleep(self.delay)
        if self.chapter_exc:
            raise self.chapter_exc
        return {"lectures": [{"id": 1}], "id": chapter_id}

    def lecture_progress(self, chapter_id):
        self.threads.add(threading.get_ident())
        time.sleep(self.delay)
        if self.states_exc:
            raise self.states_exc
        return [{"lecture_id": 1, "completed_at": "x"}]


def test_returns_same_data_as_sequential():
    data, states = qureo._load_chapter(_Client(), 7, None)
    assert data == {"lectures": [{"id": 1}], "id": 7}
    assert states == [{"lecture_id": 1, "completed_at": "x"}]


def test_runs_in_parallel_and_is_faster():
    c = _Client(delay=0.15)
    t0 = time.perf_counter()
    qureo._load_chapter(c, 1, None)
    took = time.perf_counter() - t0
    assert took < 0.27, f"المفروض الطلبين مع بعض (~0.15ث) مش ورا بعض (0.30ث): {took:.2f}"
    assert len(c.threads) == 2


def test_prefetch_one_keeps_sequential(monkeypatch):
    monkeypatch.setattr(qureo, "PREFETCH", 1)
    c = _Client(delay=0.05)
    qureo._load_chapter(c, 1, None)
    assert c.threads == {threading.get_ident()}              # نفس الـ thread: متتابع زي الأول


def test_chapter_error_wins_like_before():
    c = _Client(chapter_exc=qureo.UnknownStructure("chapter"), states_exc=qureo.PlatformError(500, "states"))
    with pytest.raises(qureo.UnknownStructure):
        qureo._load_chapter(c, 1, None)


def test_states_error_propagates():
    c = _Client(states_exc=qureo.PlatformError(403, "states"))
    with pytest.raises(qureo.PlatformError) as err:
        qureo._load_chapter(c, 1, None)
    assert err.value.status == 403


def test_stopped_propagates():
    with pytest.raises(qureo.Stopped):
        qureo._load_chapter(_Client(chapter_exc=qureo.Stopped()), 1, None)
