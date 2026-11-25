"""التحديث الآمن + النسخ الاحتياطي + سجل الأسئلة المتخمّنة."""
import importlib.util
import os
import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import matching

ROOT = Path(__file__).resolve().parent.parent


def _load_update(project_dir):
    """نحمّل update.py كأنه جوه فولدر المشروع (HERE = project_dir)."""
    spec = importlib.util.spec_from_file_location("update_under_test", ROOT / "update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = project_dir
    return mod


def _make_db(path, users):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users(tg_id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO users VALUES (?,?)", [(i, f"u{i}") for i in range(users)])
    conn.commit()
    conn.close()


def test_update_never_touches_user_data(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bot.py").write_text("OLD", encoding="utf-8")
    (proj / ".env").write_text("TOKEN=real-secret", encoding="utf-8")
    (proj / "learned_answers.json").write_text('{"real":"learning"}', encoding="utf-8")
    _make_db(proj / "bot.db", users=490)                      # قاعدة المستخدمين الحقيقية

    # ZIP جديد فيه كود جديد + محاولة (بالغلط) تستبدل الداتابيز والـ .env والتعلّم بنسخ قديمة/فاضية
    newzip = tmp_path / "new.zip"
    empty = tmp_path / "empty.db"
    _make_db(empty, users=0)
    with zipfile.ZipFile(newzip, "w") as z:
        z.writestr("v6_fix/bot.py", "NEW")
        z.writestr("v6_fix/helper.py", "ADDED")
        z.writestr("v6_fix/.env", "TOKEN=stale")
        z.writestr("v6_fix/learned_answers.json", "{}")
        z.write(empty, "v6_fix/bot.db")

    assert _load_update(proj).main(["update.py", str(newzip)]) == 0

    assert (proj / "bot.py").read_text() == "NEW"                           # الكود اتحدّث
    assert (proj / "helper.py").read_text() == "ADDED"                      # ملف جديد اتضاف
    assert (proj / ".env").read_text() == "TOKEN=real-secret"               # السر ما اتمسش
    assert (proj / "learned_answers.json").read_text() == '{"real":"learning"}'
    live = sqlite3.connect(proj / "bot.db")
    assert live.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 490  # المستخدمين زي ما هم
    live.close()
    backups = list((proj / "backups").rglob("*"))
    assert any(p.name.startswith("bot-pre-update-") for p in backups)       # نسخة من الداتابيز
    assert any(p.name == "bot.py" and p.read_text() == "OLD" for p in backups)   # القديم محفوظ


def test_update_noop_when_nothing_changed(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bot.py").write_text("SAME", encoding="utf-8")
    z = tmp_path / "same.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("bot.py", "SAME")
    assert _load_update(proj).main(["update.py", str(z)]) == 0
    assert not (proj / "backups").exists()


def test_backup_file_is_consistent_and_rotates(tmp_path):
    src = tmp_path / "bot.db"
    _make_db(src, users=25)
    (tmp_path / "learned_answers.json").write_text("{}", encoding="utf-8")
    import time
    for _ in range(4):
        target = db.backup_file(src, tmp_path / "backups", keep=2)
        time.sleep(1.1)                                      # الاسم فيه ثواني
    assert target is not None
    copies = sorted((tmp_path / "backups").glob("bot-*.db"))
    assert len(copies) == 2                                  # بيحتفظ بآخر نسختين بس
    check = sqlite3.connect(copies[-1])
    assert check.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 25
    check.close()
    assert db.backup_file(tmp_path / "missing.db", tmp_path / "b") is None


def test_unmatched_questions_logged_once(tmp_path, monkeypatch):
    monkeypatch.setenv("UNMATCHED_FILE", str(tmp_path / "u.jsonl"))
    monkeypatch.setattr(matching, "_unmatched_seen", None)
    matching.record_unmatched("financial", "سؤال   جديد؟", ["أ", "ب"])
    matching.record_unmatched("financial", "سؤال جديد؟", ["أ", "ب"])    # نفس السؤال (مسافات مختلفة)
    matching.record_unmatched("qureo", "سؤال تاني", ["x", "y"])
    lines = (tmp_path / "u.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["src"] for l in lines] == ["financial", "qureo"]


def test_engines_log_guessed_questions(tmp_path, monkeypatch):
    import api_engine
    monkeypatch.setenv("UNMATCHED_FILE", str(tmp_path / "u.jsonl"))
    monkeypatch.setattr(matching, "_unmatched_seen", None)
    index, how = api_engine._choose("Zzzz qqqq سؤال مش موجود أبدًا؟", ["اختيار أول", "اختيار تاني طويل شوية"], [], None)
    assert how == "guess"
    rec = json.loads((tmp_path / "u.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["src"] == "financial" and "سؤال مش موجود" in rec["q"]


def test_relative_db_path_is_pinned_to_bot_folder(tmp_path):
    """تشغيل البوت من فولدر تاني ما يفتحش قاعدة فاضية جديدة."""
    import subprocess
    env = dict(os.environ, DB_PATH="bot.db", BOT_TOKEN="123:abc")
    code = "import bot; print(bot._db_path())"
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, r'{ROOT}'); {code}"],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == str(ROOT / "bot.db"), out.stderr[-300:]
