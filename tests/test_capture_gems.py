"""أداة capture_gems.py: منطق التسجيل (من غير متصفح حقيقي) + ربط أحداث Playwright بشكل وهمي."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze_gems as ag
import capture_gems as cg


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_http_redacts_secrets_and_flags_gem_fields():
    rec = cg.Recorder(clock=Clock())
    entry = rec.http("get", "https://x/api/study/students/me?token=SECRET&x=1", 200, None,
                     json.dumps({"name": "Ahmed", "email": "a@b.c", "gems": 10, "token": "SECRET"}))
    text = json.dumps(entry, ensure_ascii=False)
    assert "SECRET" not in text and "a@b.c" not in text and "Ahmed" not in text
    assert entry["gem_fields"][0]["path"] == "gems" and entry["gem_fields"][0]["value"] == 10
    assert "x=1" in entry["url"]


def test_static_assets_are_skipped_and_request_body_shrunk():
    rec = cg.Recorder(clock=Clock())
    assert rec.http("GET", "https://x/app.js", 200, None, "x") is None
    e = rec.http("POST", "https://x/api/a", 200, json.dumps({"choice_id": 3, "password": "p"}), "{}")
    assert e["request"] == {"choice_id": 3, "password": "<محجوب>"}


def test_screen_hits_deduped_and_capped():
    rec = cg.Recorder(clock=Clock())
    assert rec.screen({"text": "حصلت على 5 جواهر", "tag": "DIV", "page_text": "full"})
    assert rec.screen({"text": "حصلت على 5 جواهر"}) is None
    assert rec.dom[0]["page_text"] == "full"


class _Req:
    resource_type = "xhr"
    method = "POST"
    post_data = '{"a": 1}'


class _Resp:
    def __init__(self, body, rtype="xhr"):
        self.request = type("R", (), {"resource_type": rtype, "method": "GET", "post_data": None})()
        self.url = "https://x/api/study/students/chapters/9/test/result"
        self.status = 200
        self._body = body

    def text(self):
        return self._body


class _Ctx:
    def __init__(self):
        self.handlers, self.pages, self.scripts, self.bindings = {}, [], [], {}

    def on(self, name, fn):
        self.handlers[name] = fn

    def add_init_script(self, js):
        self.scripts.append(js)

    def expose_binding(self, name, fn):
        self.bindings[name] = fn


def test_attach_wires_responses_and_screen_binding():
    rec = cg.Recorder(clock=Clock())
    ctx = _Ctx()
    cg.attach(ctx, rec)
    ctx.handlers["response"](_Resp(json.dumps({"result": {"correct_count": 8, "gems_earned": 5}})))
    ctx.handlers["response"](_Resp("<html>", rtype="document"))          # مش xhr/fetch: يتجاهل
    ctx.bindings["__gemsDom"]({"page": None}, {"text": "مبروك! 5 جواهر", "tag": "DIV"})
    assert len(rec.entries) == 1 and rec.entries[0]["gem_fields"][0]["value"] == 5
    assert len(rec.dom) == 1 and "__gemsDom" in ctx.scripts[0]


def test_result_contains_analysis_and_prefers_last_strict_gem_text():
    clock = Clock()
    rec = cg.Recorder(clock=clock)
    clock.t = 1; rec.screen({"text": "مبروك أجبت صح"})
    clock.t = 5; rec.http("GET", "https://x/api/study/students/me", 200, None, json.dumps({"gems": 10}))
    clock.t = 9; rec.screen({"text": "حصلت على 5 جواهر"})
    clock.t = 10; rec.http("GET", "https://x/api/study/students/me", 200, None, json.dumps({"gems": 15}))
    cap = rec.result()
    assert cap["analysis"]["anchor"]["t"] == 9000                       # آخر نص فيه «جواهر»
    changed = {c["path"]: [v for _, v in c["values_by_seq"]] for c in cap["analysis"]["changed_numbers"]}
    assert changed["gems"] == [10, 15]


def test_save_writes_json_file(tmp_path, capsys):
    rec = cg.Recorder(clock=Clock())
    rec.http("GET", "https://x/api/a", 200, None, "{}")
    out = tmp_path / "o.json"
    cg.save(rec, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["entries"] and "analysis" in data
    assert "اتكتب الملف" in capsys.readouterr().out


def test_main_without_playwright_installed_is_friendly(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert cg.main([]) == 1
    assert "pip install playwright" in capsys.readouterr().out
