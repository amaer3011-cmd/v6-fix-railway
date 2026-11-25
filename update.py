"""
update.py — تحديث البوت من ZIP جديد من غير ما تتأثر بياناتك أبدًا.

الاستخدام (وقّف البوت الأول، وشغّل الأمر من جوه فولدر البوت):
    python update.py  مسار_الـZIP_الجديد.zip

اللي بيحصل:
  • الملفات دي «بيانات» ومش بتتلمس أبدًا: .env  /  bot.db (+wal/shm)  /  learned_*.json
    /  unmatched_questions.jsonl  /  backups/
  • أي ملف كود تاني بيتغير: النسخة القديمة بتتحفظ الأول في backups/pre-update-<وقت>/
  • قبل أي حاجة بياخد نسخة احتياطية من قاعدة البيانات.
  • لو مفيش ملف اتغير بيقولك ومبيعملش حاجة.
"""
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# بيانات المستخدمين: ممنوع نلمسها
PROTECTED_NAMES = {
    ".env", "bot.db", "bot.db-wal", "bot.db-shm",
    "learned_answers.json", "learned_js_answers.json", "unmatched_questions.jsonl",
}
PROTECTED_DIRS = {"backups", "__pycache__", ".pytest_cache", ".git"}


def _is_protected(rel: Path) -> bool:
    return rel.name in PROTECTED_NAMES or any(part in PROTECTED_DIRS for part in rel.parts)


def _project_root(extracted: Path) -> Path:
    """الـZIP ممكن يكون فيه فولدر v6_fix جوّاه أو الملفات مباشرة."""
    if (extracted / "bot.py").exists():
        return extracted
    for child in extracted.iterdir():
        if child.is_dir() and (child / "bot.py").exists():
            return child
        if child.is_dir():
            for grand in child.iterdir():
                if grand.is_dir() and (grand / "bot.py").exists():
                    return grand
    raise SystemExit("❌ مش لاقي bot.py جوه الـZIP. تأكد إنه ZIP المشروع.")


def _backup_db(stamp: str) -> None:
    db_path = HERE / "bot.db"
    if not db_path.exists():
        return
    dest = HERE / "backups"
    dest.mkdir(exist_ok=True)
    target = dest / f"bot-pre-update-{stamp}.db"
    source = sqlite3.connect(str(db_path))
    try:
        out = sqlite3.connect(str(target))
        try:
            source.backup(out)          # آمن مع WAL
        finally:
            out.close()
    finally:
        source.close()
    print(f"💾 نسخة من قاعدة البيانات: backups/{target.name}")


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    zip_path = Path(argv[1])
    if not zip_path.exists():
        print(f"❌ الملف مش موجود: {zip_path}")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)
        new_root = _project_root(Path(tmp))

        changed, added, kept = [], [], []
        for src in sorted(p for p in new_root.rglob("*") if p.is_file()):
            rel = src.relative_to(new_root)
            if _is_protected(rel):
                kept.append(rel)
                continue
            dst = HERE / rel
            if not dst.exists():
                added.append((rel, src))
            elif dst.read_bytes() != src.read_bytes():
                changed.append((rel, src))

        if not changed and not added:
            print("✅ مفيش تغييرات: البوت عندك محدّث بالفعل.")
            return 0

        _backup_db(stamp)
        pre = HERE / "backups" / f"pre-update-{stamp}"
        for rel, src in changed:
            old = HERE / rel
            (pre / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, pre / rel)                  # نحفظ القديم الأول
            shutil.copy2(src, old)
        for rel, src in added:
            (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, HERE / rel)

    print(f"🔄 اتحدّث {len(changed)} ملف، واتضاف {len(added)} ملف جديد.")
    if changed:
        print(f"🗂️ النسخ القديمة محفوظة في: backups/pre-update-{stamp}/")
    print(f"🔒 ملفات البيانات اللي ما اتلمستش: {len(kept)} (قاعدة البيانات، .env، التعلّم، النسخ الاحتياطية)")
    print("▶️ دلوقتي شغّل البوت عادي. المستخدمين والمحاولات زي ما هم.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
