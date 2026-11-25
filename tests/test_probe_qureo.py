"""أداة probe_qureo: قراءة فقط، بتحجب البيانات الشخصية، ومابتسرّبش الباسورد."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import probe_qureo
import qureo_engine as q


class _Stub:
    calls = []

    def __init__(self, cancel=None, course="python", course_id=None):
        pass

    def login(self, sid, pw):
        _Stub.calls.append(("login", pw))

    def _get(self, path, **kw):
        _Stub.calls.append(("GET", path))
        return {"name": "Ahmed Secret", "email": "a@b.c", "gems_total": 12}

    def course(self):
        return {"basic_section": {"id": 10}}

    def section(self, _):
        return {"chapters": [{"id": 1, "test_type": "review"}, {"id": 2, "test_type": "test"}]}

    def section_progress(self, _):
        return [{"chapter_id": 1, "completed_at": "x", "gems_earned": 2, "gems_max": 3}]

    def lecture_progress(self, cid):
        return [{"lecture_id": 100, "completed_at": "x", "stars": 3}]

    def test_state(self, cid):
        return {"visible_result": True}

    def review_result(self, cid):
        return {"correct_count": 3, "score_percent": 100}

    test_result = review_result

    def chapter(self, cid):
        return {"lectures": [{"id": 100}, {"id": 101}, {"id": 102}]}

    def student_lecture(self, lid):
        return {"talks": [], "diamonds": 1}

    def close(self):
        pass


def test_probe_writes_structure_redacts_personal_and_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "QureoClient", _Stub)
    monkeypatch.setattr(probe_qureo.q, "QureoClient", _Stub)
    monkeypatch.setattr(probe_qureo, "HERE", tmp_path)
    monkeypatch.setattr(probe_qureo.getpass, "getpass", lambda _p: "TopSecretPw")
    _Stub.calls.clear()
    assert probe_qureo.main(["probe_qureo.py", "stud1", "javascript"]) == 0

    text = (tmp_path / "qureo_probe_javascript.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert "TopSecretPw" not in text and "Ahmed Secret" not in text and "a@b.c" not in text
    fields = " ".join(data["interesting_fields"])
    assert "gems_total" in fields and "gems_earned" in fields and "stars" in fields and "diamonds" in fields
    assert all(method in ("login", "GET") for method, _ in _Stub.calls)        # قراءة فقط، مفيش PUT/POST
