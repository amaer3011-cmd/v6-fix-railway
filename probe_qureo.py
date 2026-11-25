"""
probe_qureo.py — أداة قراءة فقط: بتطلّع اللي منصة Qureo بترجّعه لحساب معيّن عشان نعرف الجواهر مخزّنة فين.

مابتعدّلش في الحساب ولا بتجاوب ولا بتبدأ ولا بتعيد أي اختبار: GET بس.
الباسورد بيتسأل عنه في الطرفية (مش بيتكتب في أي ملف). الاسم/الإيميل/التليفون بيتحجبوا من الناتج.

الاستخدام (من فولدر البوت):
    python probe_qureo.py كود_الطالب javascript      ← أو python
ويطلع ملف  qureo_probe_javascript.json  ابعته لي (فيه هيكل الردود بس، من غير محتوى الدروس).

شغّله على حساب ناقص جواهر، وياريت كمان على حساب كامل الجواهر للمقارنة.
"""
import getpass
import json
import re
import sys
from pathlib import Path

import qureo_engine as q

HERE = Path(__file__).resolve().parent
PERSONAL = re.compile(r"(^|_)(name|email|mail|phone|mobile|avatar|photo|picture|address|birth)", re.I)
INTEREST = re.compile(
    r"gem|diamond|jewel|crystal|star|score|point|xp|reward|badge|medal|trophy|coin|"
    r"mastery|perfect|percent|rate|rank|level|grade|result|correct|bonus|achiev",
    re.I,
)


def shrink(obj, depth=0):
    """نحتفظ بكل أسماء الحقول (هي اللي محتاجينها) ونقصّ القيم الطويلة والقوائم."""
    if isinstance(obj, dict):
        return {
            k: ("<محجوب>" if PERSONAL.search(str(k)) else shrink(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        head = [shrink(v, depth + 1) for v in obj[:3]]
        return head + ([f"…(+{len(obj) - 3} عنصر)"] if len(obj) > 3 else [])
    if isinstance(obj, str):
        return obj if len(obj) <= 60 else obj[:60] + "…"
    return obj


def key_paths(obj, prefix=""):
    """كل مسارات الحقول + نوع قيمتها."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(key_paths(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for v in obj[:3]:
            out.update(key_paths(v, prefix + "[]"))
    else:
        out[prefix] = type(obj).__name__
    return out


def call(label, fn, log):
    try:
        data = fn()
        log[label] = shrink(data)
        return data
    except Exception as exc:  # noqa: BLE001
        log[label] = {"_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    student_id = argv[1]
    course = argv[2] if len(argv) > 2 else "python"
    if course not in q.COURSES:
        print("الكورس لازم يكون python أو javascript")
        return 1
    password = getpass.getpass("الباسورد (مش هيتحفظ): ")
    client = q.QureoClient(None, course)
    log = {}
    try:
        client.login(student_id, password)
        del password
        getter = getattr(client, "_get", None)
        if getter:
            call("userinfo", lambda: getter("/oauth2/userinfo", relogin=False), log)
            call("students_me", lambda: getter("/api/study/students/me", relogin=False), log)
        course_data = call("course", client.course, log) or {}
        section_id = (course_data.get("basic_section") or {}).get("id")
        if section_id is not None:
            section = call("section", lambda: client.section(section_id), log) or {}
            call("section_progress", lambda: client.section_progress(section_id), log)
            for chapter in (section.get("chapters") or [])[:40]:
                cid = chapter.get("id")
                tag = f"chapter_{cid}"
                call(f"{tag}.progress_lectures", lambda c=cid: client.lecture_progress(c), log)
                call(f"{tag}.test_state", lambda c=cid: client.test_state(c), log)
                fetch = client.test_result if chapter.get("test_type") == "test" else client.review_result
                call(f"{tag}.result", lambda c=cid, f=fetch: f(c), log)
                detail = call(f"{tag}.detail", lambda c=cid: client.chapter(c), log) or {}
                for lecture in (detail.get("lectures") or [])[:2]:      # عينة بس من الدروس
                    lid = lecture.get("id")
                    call(f"{tag}.lecture_{lid}.student_state",
                         lambda l=lid: client.student_lecture(l), log)
    finally:
        client.close()

    paths = {}
    for label, data in log.items():
        for path, kind in key_paths(data).items():
            if INTEREST.search(path):
                paths.setdefault(path, set()).add(label.split(".")[-1] if "." in label else label)
    summary = {p: {"type": "—", "seen_in": sorted(v)[:4]} for p, v in sorted(paths.items())}
    out = HERE / f"qureo_probe_{course}.json"
    out.write_text(
        json.dumps({"course": course, "interesting_fields": summary, "responses": log},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"✅ اتحفظ: {out.name}  ({len(log)} رد)")
    print("حقول ممكن تكون ليها علاقة بالجواهر/الدرجات:")
    for path in list(summary)[:40]:
        print("  •", path)
    if not summary:
        print("  (مفيش أي حقل بالأسماء دي: يعني الجواهر مش في الردود دي، ومحتاجين نعرف صفحتها في المنصة)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
