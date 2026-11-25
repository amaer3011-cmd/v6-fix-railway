"""أداة تحليل التقاط الجواهر: لحظة الظهور، الحقول، والأرقام المتغيّرة (ملف التقاط + HAR)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze_gems as ag


def _capture():
    return {
        "entries": [
            {"seq": 1, "t": 1000, "method": "GET", "url": "https://x/api/study/students/me",
             "status": 200, "response": {"gems": 10, "email": "<محجوب>"}},
            {"seq": 2, "t": 5000, "method": "POST", "url": "https://x/api/study/students/questions/77/answer",
             "status": 200, "request": {"choice_id": 3}, "response": {"result": {"correct": True}}},
            {"seq": 3, "t": 6000, "method": "GET", "url": "https://x/api/study/students/chapters/9/test/result",
             "status": 200, "response": {"correct_count": 8}},
            {"seq": 4, "t": 6500, "method": "GET", "url": "https://x/api/study/students/me",
             "status": 200, "response": {"gems": 15}},
        ],
        "dom": [{"t": 6400, "text": "مبروك! حصلت على 5 جواهر", "tag": "DIV", "class": "reward"}],
        "marks": [{"t": 6600, "note": "ظهرت الجواهر"}],
    }


def test_endpoint_key_collapses_ids():
    assert ag.endpoint_key("get", "https://x/api/a/12/b/34?q=1") == "GET /api/a/{id}/b/{id}"


def test_anchor_before_calls_and_changed_numbers():
    f = ag.analyze(_capture())
    assert f["anchor"]["t"] == 6600 and "علامة" in f["anchor"]["label"]
    calls = [c["call"] for c in f["before_anchor"]]
    assert "POST /api/study/students/questions/{id}/answer" in calls
    changed = {(c["endpoint"], c["path"]): [v for _, v in c["values_by_seq"]] for c in f["changed_numbers"]}
    assert changed[("GET /api/study/students/me", "gems")] == [10, 15]      # عدّاد الجواهر اتغيّر
    assert "GET /api/study/students/me" in f["gem_fields"]


def test_anchor_falls_back_to_dom_then_gem_field():
    cap = _capture(); cap["marks"] = []
    assert "الشاشة" in ag.analyze(cap)["anchor"]["label"]
    cap["dom"] = []
    assert "حقل" in ag.analyze(cap)["anchor"]["label"]


def test_har_is_converted_and_secrets_ignored(tmp_path):
    har = {"log": {"entries": [
        {"startedDateTime": "2026-11-25T10:00:00.000Z",
         "request": {"method": "GET", "url": "https://x/api/study/students/me?token=SECRET",
                     "headers": [{"name": "Authorization", "value": "Bearer SECRET"}]},
         "response": {"status": 200, "content": {"text": json.dumps({"gems": 1, "token": "SECRET"})}}},
        {"startedDateTime": "2026-11-25T10:00:05.000Z",
         "request": {"method": "GET", "url": "https://x/api/study/students/me"},
         "response": {"status": 200, "content": {"text": json.dumps({"gems": 6})}}},
        {"startedDateTime": "2026-11-25T10:00:06.000Z",
         "request": {"method": "GET", "url": "https://x/static/app.js"},
         "response": {"status": 200, "content": {"text": "x"}}},
    ]}}
    path = tmp_path / "c.har"
    path.write_text(json.dumps(har), encoding="utf-8")
    cap = ag.load(path)
    assert len(cap["entries"]) == 2 and cap["entries"][1]["t"] == 5000
    assert "SECRET" not in json.dumps(cap["entries"][0]["url"])           # الكويري اتشال
    f = ag.analyze(cap)
    assert f["changed_numbers"][0]["path"] == "gems"
    assert "SECRET" not in json.dumps(f["gem_fields"])


def test_main_writes_findings(tmp_path, capsys):
    path = tmp_path / "cap.json"
    path.write_text(json.dumps(_capture()), encoding="utf-8")
    assert ag.main(["analyze_gems.py", str(path)]) == 0
    assert (tmp_path / "gems_findings.json").exists()
    assert "لحظة الجواهر" in capsys.readouterr().out
