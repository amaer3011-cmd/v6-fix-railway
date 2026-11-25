"""فحص جاهزية قبل التشغيل.

  python selfcheck.py --build        وقت بناء Docker
  python selfcheck.py                وقت تشغيل البوت
"""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
BASE  = Path(__file__).resolve().parent
BUILD = "--build" in sys.argv
failed = []


def check(name, fn, hard=True):
    try:
        detail = fn()
        print(f"✅ {name}" + (f" — {detail}" if detail else ""), flush=True)
    except Exception as exc:  # noqa: BLE001
        mark = "❌" if hard else "⚠️"
        print(f"{mark} {name}: {exc}", flush=True)
        if hard:
            failed.append(name)


def check_answers():
    import matching as core
    path = BASE / os.getenv("ANSWERS_FILE", "answers.txt")
    if not path.exists():
        raise FileNotFoundError(f"ملف الإجابات مش موجود: {path.name}")
    n = len(core.parse_answer_file(path))
    if n < 100:
        raise RuntimeError(f"عدد الأسئلة المحمّلة قليل ({n})")
    return f"{n} سؤال"


def check_qureo_answers():
    import qureo_engine
    path = BASE / os.getenv("QUREO_ANSWERS_FILE", "qureo_answers.txt")
    if not path.exists():
        raise FileNotFoundError(f"ملف إجابات Qureo مش موجود: {path.name}")
    n = len(qureo_engine.parse_answer_file(path))
    if n < 100:
        raise RuntimeError(f"عدد أسئلة Qureo المحمّلة قليل ({n})")
    return f"{n} سؤال"


def check_qureo_js_answers():
    import qureo_engine
    path = BASE / os.getenv("JAVASCRIPT_ANSWERS_FILE", "javascript_answers.md")
    if not path.exists():
        raise FileNotFoundError(f"ملف إجابات JavaScript مش موجود: {path.name}")
    n = len(qureo_engine.parse_js_answer_file(path))
    if n < 100:
        raise RuntimeError(f"عدد أسئلة JavaScript المحمّلة قليل ({n})")
    return f"{n} سؤال"


def check_httpx():
    import httpx
    # تحقق إن HTTP/2 متاح
    try:
        import h2  # noqa: F401
        h2_status = "HTTP/2 ✓"
    except ImportError:
        h2_status = "HTTP/2 ✗ (تأكد من httpx[http2])"
    return f"httpx {httpx.__version__} — {h2_status}"


def check_token():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("متغير BOT_TOKEN فاضي — ضيفه في Variables")
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
        raise RuntimeError("شكل BOT_TOKEN غلط — تأكد إنك نسخته كامل من BotFather")


def check_db_path():
    path = Path(os.getenv("DB_PATH", str(BASE / "bot.db")))
    path.parent.mkdir(parents=True, exist_ok=True)
    probe = path.parent / ".write_probe"
    probe.write_text("ok")
    probe.unlink()
    return str(path)


check("ملف الإجابات",   check_answers)
check("إجابات Qureo Python", check_qureo_answers)
check("إجابات Qureo JavaScript", check_qureo_js_answers)
check("مكتبة httpx",    check_httpx)

if not BUILD:
    check("BOT_TOKEN",             check_token)
    check("مكان قاعدة البيانات",  check_db_path,
          hard=not os.getenv("RAILWAY_ENVIRONMENT"))

if failed:
    print(f"\n🛑 الفحص فشل في: {', '.join(failed)}", flush=True)
    sys.exit(1)
print("\n🚀 كل شيء جاهز", flush=True)
