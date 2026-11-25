"""
matching.py — مطابقة أسئلة المنصة مع بنك الإجابات (منطق خالص بلا متصفح ولا شبكة).

مستخرج من core.py القديم (محرك Playwright) ليشتغل المحرك الجديد api_engine بدون Playwright.
"""
import difflib
import json
import os
import re
import threading
import unicodedata
from collections import defaultdict
from pathlib import Path

QUESTION_RE = re.compile(
    r"^السؤال\s+(?:الأول|الاول|الثاني|الثالث|الرابع|الخامس|السادس|السابع|"
    r"الثامن|التاسع|العاشر|[0-9٠-٩]+)(?:\s+(.*))?$"
)
ANSWER_LABELS = {"الاجابة", "الإجابة"}

class AnswerMatchError(RuntimeError):
    pass


def normalize_text(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ٱ": "ا",
                "ى": "ي",
                "ـ": None,
            }
        )
    )
    value = value.translate(
        str.maketrans(
            "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
            "01234567890123456789",
        )
    )
    value = re.sub(r"[\u064B-\u065F\u0670]", "", value)
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


class AnswerIndex:
    """فهرس الإجابات: التطبيع بيتعمل مرة واحدة وقت التحميل + قاموس للتطابق التام."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.rows = []
        self.exact = defaultdict(list)
        self.match_cache = {}
        self.fuzzy_cache = {}
        for question, answer in self.entries:
            row = (normalize_text(question), normalize_text(answer), question, answer)
            self.rows.append(row)
            self.exact[row[0]].append(row)

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


def locate_answer_file():
    candidates = sorted(
        p
        for p in Path(__file__).resolve().parent.glob("*.txt")
        if not p.name.lower().startswith("requirements")
    )
    if len(candidates) != 1:
        raise FileNotFoundError(
            "يجب وجود ملف إجابات نصي واحد بجوار الأداة؛ لم أستطع تحديده تلقائيًا."
        )
    return candidates[0]


def parse_answer_file(path):
    path = Path(path)
    if path.suffix.casefold() == ".md":
        return parse_markdown_answer_file(path)

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    entries = []
    question_lines = []
    answer_lines = []
    state = "seek"

    def save_entry():
        question = " ".join(line.strip() for line in question_lines if line.strip())
        answer = " ".join(line.strip() for line in answer_lines if line.strip())
        if question and answer:
            entries.append((question, answer))

    for raw_line in lines:
        line = raw_line.strip()
        marker = QUESTION_RE.match(line)
        if marker:
            if state == "answer":
                save_entry()
            question_lines = [marker.group(1) or ""]
            answer_lines = []
            state = "question"
            continue

        if line in ANSWER_LABELS:
            if state == "question":
                state = "answer"
            continue

        if state == "answer" and (
            line.startswith(
                (
                    "المستوي",
                    "المستوى",
                    "الدرس",
                    "الوحدة",
                    "نضغط ",
                    "حل اختبار",
                    "اختبار المعرفة",
                )
            )
            or line.startswith(("الف مبروك", "ألف مبروك"))
            or line.startswith("…")
            or line.startswith("---")
        ):
            save_entry()
            question_lines = []
            answer_lines = []
            state = "seek"
            continue

        if state == "question" and line:
            question_lines.append(line)
        elif state == "answer" and line:
            answer_lines.append(line)

    if state == "answer":
        save_entry()

    if not entries:
        raise ValueError(f"لم أجد أسئلة وأجوبة قابلة للقراءة في: {path.name}")
    return AnswerIndex(entries)


def parse_markdown_answer_file(path):
    """Load question/answer pairs from the structured Markdown answer bank."""
    entries = []
    question_lines = []
    answer_lines = []
    state = None

    def save_entry():
        question = " ".join(line.strip() for line in question_lines if line.strip())
        answer = " ".join(line.strip() for line in answer_lines if line.strip())
        if question and answer:
            entries.append((question, answer))

    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line.startswith("**السؤال:**"):
            save_entry()
            question_lines = [line.removeprefix("**السؤال:**").strip()]
            answer_lines = []
            state = "question"
        elif line.startswith("**الإجابة:**"):
            answer_lines = [line.removeprefix("**الإجابة:**").strip()]
            state = "answer"
        elif line.startswith("#") or line.startswith("**") or line == "---":
            save_entry()
            question_lines = []
            answer_lines = []
            state = None
        elif state == "question" and line:
            question_lines.append(line)
        elif state == "answer" and line:
            answer_lines.append(line)

    save_entry()
    if not entries:
        raise ValueError(f"لم أجد أسئلة وأجوبة Markdown قابلة للقراءة في: {Path(path).name}")
    return AnswerIndex(entries)


def match_answer(question, entries):
    index = entries if isinstance(entries, AnswerIndex) else AnswerIndex(entries)
    normalized_question = normalize_text(question)
    if not normalized_question:
        raise AnswerMatchError("لم أستطع استخراج نص السؤال من الصفحة.")

    cached = index.match_cache.get(normalized_question)
    if cached:
        if cached[0] == "answer":
            return cached[1], cached[2]
        if cached[0] == "ambiguous":
            raise AnswerMatchError(
                f"وجدت أكثر من إجابة محتملة للسؤال: {question[:180]}"
            )
        raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")

    exact_rows = index.exact.get(normalized_question)
    if exact_rows:
        if len({row[1] for row in exact_rows}) != 1:
            index.match_cache[normalized_question] = ("ambiguous",)
            raise AnswerMatchError(
                f"وجدت إجابات متعارضة للسؤال: {question[:180]}"
            )
        result = (exact_rows[0][3], exact_rows[0][2])
        index.match_cache[normalized_question] = ("answer", *result)
        return result

    # حدود التطابق: 0.82 للفحص السريع، 0.85 للقبول النهائي (أكثر مرونة قليلاً مع الحفاظ على الدقة)
    floor = 0.82
    accept_threshold = 0.85
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(normalized_question)
    q_len = len(normalized_question)

    scored = []
    for normalized_source, normalized_answer, source_question, answer in index.rows:
        src_len = len(normalized_source)
        # تطابق احتوائي قوي للأسئلة الطويلة
        if (
            min(q_len, src_len) >= 25
            and (
                normalized_question in normalized_source
                or normalized_source in normalized_question
            )
        ):
            score = 0.96
        else:
            matcher.set_seq1(normalized_source)
            if (
                matcher.real_quick_ratio() < floor
                or matcher.quick_ratio() < floor
            ):
                continue
            score = matcher.ratio()
            if score < floor:
                continue
        scored.append((score, answer, source_question, normalized_answer))

    if not scored:
        index.match_cache[normalized_question] = ("no_match",)
        raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_answer, best_question, best_norm_answer = scored[0]
    if best_score < accept_threshold:
        index.match_cache[normalized_question] = ("no_match",)
        raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")

    # رفض لو فيه إجابات متعارضة قريبة جدًا
    close_matches = [
        item
        for item in scored
        if best_score - item[0] < 0.03 and item[3] != best_norm_answer
    ]
    if close_matches:
        index.match_cache[normalized_question] = ("ambiguous",)
        raise AnswerMatchError(
            f"وجدت أكثر من إجابة محتملة للسؤال: {question[:180]}"
        )
    index.match_cache[normalized_question] = ("answer", best_answer, best_question)
    return best_answer, best_question


def _option_variants(option):
    """صيغ الخيار المحتملة: كما هو، وبدون شارة الحرف (A/B/C/D)، وبدون ترقيم '1.' الصريح."""
    no_letter = re.sub(r"^\s*[A-Da-d]\s*[\.\)\-:]?\s+", "", option)
    no_letter = re.sub(r"^\s*[أابجدهـ]\s*[\.\)\-:،]\s*", "", no_letter)
    no_number = re.sub(r"^\s*[0-9٠-٩]+\s*[\.\)\-]\s+", "", no_letter)
    variants = {normalize_text(option), normalize_text(no_letter), normalize_text(no_number)}
    variants.discard("")
    return variants


def answer_option_index(options, answer):
    normalized_answer = normalize_text(answer)
    exact = []
    partial = []
    fuzzy = []

    for index, option in enumerate(options):
        variants = _option_variants(option)
        if not variants:
            continue

        if normalized_answer in variants:
            exact.append(index)
        elif any(
            min(len(v), len(normalized_answer)) >= 10
            and (normalized_answer in v or v in normalized_answer)
            for v in variants
        ):
            partial.append(index)
        else:
            ratio = max(
                difflib.SequenceMatcher(None, v, normalized_answer, autojunk=False).ratio()
                for v in variants
            )
            if ratio >= 0.88:
                fuzzy.append((ratio, index))

    if exact:
        if len(exact) > 1:
            raise AnswerMatchError("وجدت أكثر من خيار مطابق تمامًا للإجابة.")
        return exact[0]

    if partial:
        if len(partial) > 1:
            raise AnswerMatchError("وجدت أكثر من خيار جزئي مطابق للإجابة.")
        return partial[0]

    if fuzzy:
        fuzzy.sort(reverse=True)
        if len(fuzzy) == 1 or (fuzzy[0][0] - fuzzy[1][0] > 0.04):
            return fuzzy[0][1]

    answer_tokens = set(normalized_answer.split())
    if len(answer_tokens) >= 3:
        scored_tokens = []
        for index, option in enumerate(options):
            best = None
            for variant in _option_variants(option):
                tokens = set(variant.split())
                if len(tokens) < 3:
                    continue
                shared = len(answer_tokens & tokens)
                candidate = (
                    shared / min(len(answer_tokens), len(tokens)),
                    shared / len(answer_tokens | tokens),
                )
                if best is None or candidate > best:
                    best = candidate
            if best and best[0] >= 0.85:
                scored_tokens.append((best, index))
        if scored_tokens:
            scored_tokens.sort(reverse=True)
            if len(scored_tokens) == 1 or (
                scored_tokens[0][0][0] - scored_tokens[1][0][0] >= 0.15
                or scored_tokens[0][0][1] - scored_tokens[1][0][1] >= 0.2
            ):
                return scored_tokens[0][1]

    raise AnswerMatchError("لم أستطع تحديد خيار واحد مطابق للإجابة في المنصة.")


def _fuzzy_candidates(normalized_question, index):
    cached = index.fuzzy_cache.get(normalized_question)
    if cached is not None:
        return cached
    floor = 0.82
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(normalized_question)
    question_length = len(normalized_question)
    scored = []
    for normalized_source, normalized_answer, source_question, answer in index.rows:
        source_length = len(normalized_source)
        if (
            min(question_length, source_length) >= 25
            and (
                normalized_question in normalized_source
                or normalized_source in normalized_question
            )
        ):
            score = 0.96
        else:
            matcher.set_seq1(normalized_source)
            if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
                continue
            score = matcher.ratio()
            if score < floor:
                continue
        scored.append((score, answer, source_question, normalized_answer))
    scored.sort(key=lambda item: item[0], reverse=True)
    index.fuzzy_cache[normalized_question] = scored
    return scored


def resolve_answer(question, entries, option_texts):
    """يعثر على إجابة البنك ولا يختار إلا خيارًا ظاهرًا واحدًا غير ملتبس."""
    index = entries if isinstance(entries, AnswerIndex) else AnswerIndex(entries)
    normalized_question = normalize_text(question)
    if not normalized_question:
        raise AnswerMatchError("لم أستطع استخراج نص السؤال من الصفحة.")

    exact_rows = index.exact.get(normalized_question)
    if exact_rows:
        rows = [(1.0, row[3], row[2], row[1]) for row in exact_rows]
    else:
        scored = _fuzzy_candidates(normalized_question, index)
        if not scored or scored[0][0] < 0.85:
            raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")
        best_score = scored[0][0]
        rows = [item for item in scored if best_score - item[0] < 0.03]

    resolved = {}
    last_error = None
    for _score, answer, source_question, _normalized_answer in rows:
        try:
            option_index = answer_option_index(option_texts, answer)
        except AnswerMatchError as exc:
            last_error = exc
            continue
        resolved.setdefault(option_index, (answer, source_question))

    if not resolved:
        raise last_error or AnswerMatchError(
            "لم أستطع تحديد خيار مطابق للإجابة في المنصة."
        )
    if len(resolved) > 1:
        raise AnswerMatchError(f"وجدت أكثر من إجابة محتملة للسؤال: {question[:180]}")
    option_index, (answer, source_question) = next(iter(resolved.items()))
    return option_index, answer, source_question



def answer_bank_stats(entries):
    """ملخص سريع لبنك الإجابات: العدد الكلي والفريد بعد التطبيع."""
    total = len(entries)
    unique = len({normalize_text(question) for question, _answer in entries})
    return {"total": total, "unique": unique, "duplicates": total - unique}


# ─────────── سجل الأسئلة اللي مش في البنك (عشان نكمّل البنك وتختفي التخمينات) ───────────
_unmatched_lock = threading.Lock()
_unmatched_seen = None


def unmatched_log_path() -> Path:
    custom = os.getenv("UNMATCHED_FILE")
    if custom:
        return Path(custom)
    return Path(os.getenv("DB_PATH", "bot.db")).resolve().parent / "unmatched_questions.jsonl"


def record_unmatched(source: str, question: str, options) -> None:
    """بيسجّل سؤال اتخمّن (مش موجود في البنك) مرة واحدة بس. أي فشل هنا مايوقفش الحل أبدًا."""
    global _unmatched_seen
    try:
        key = " ".join(str(question).split())[:300]
        path = unmatched_log_path()
        with _unmatched_lock:
            if _unmatched_seen is None:
                _unmatched_seen = set()
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            _unmatched_seen.add(json.loads(line).get("q", ""))
                        except ValueError:
                            pass
            if key in _unmatched_seen:
                return
            _unmatched_seen.add(key)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"src": source, "q": key, "options": [str(o)[:200] for o in options]}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
