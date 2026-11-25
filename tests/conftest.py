"""عزل الاختبارات عن بيانات البوت الحقيقية.

الاختبارات كانت بتكتب أسئلة تجريبية في unmatched_questions.jsonl اللي جنب bot.db في مجلد المشروع.
هنا بنوجّه DB_PATH (ومعاه unmatched_questions.jsonl و learned_*.json الافتراضية) لمجلد مؤقت قبل ما أي موديول يتحمّل،
فتشغيل `pytest` جوه مجلد بوت شغّال مايلمسش أي ملف بيانات فيه.
"""
import atexit
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="finbot-tests-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["DB_PATH"] = os.path.join(_TMP, "bot.db")
os.environ.setdefault("LEARNED_FILE", os.path.join(_TMP, "learned_answers.json"))
