"""Qureo course client (Python + JavaScript) using the documented/observed study API."""

import contextvars
import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import httpx

import matching
from api_engine import LearnedAnswers, _where, get_learned

_log = logging.getLogger(__name__)

PORTAL_URL = "https://me-portal.qureo.education"
TP_URL = "https://me-tp.qureo.education"
COURSE_ID = "python_short_ar"   # للتوافق مع القديم؛ الدورة الفعلية بتيجي من COURSES
# كل دورة على Qureo: معرّفها + لغة الكود + كلمات بنبحث بيها في available_courses لو المعرّف مش معروف.
COURSES = {
    "python": {
        "id": COURSE_ID, "lang": "python", "label": "Python",
        "hints": ("python",),
    },
    "javascript": {
        "id": os.getenv("QUREO_JS_COURSE_ID", "javascript_ar").strip() or "javascript_ar",
        "lang": "javascript", "label": "JavaScript",
        "hints": ("javascript", "java_script", "js_"),
    },
}
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "4"))
ANSWER_DELAY = float(os.getenv("QUREO_ANSWER_DELAY", "0"))   # فاصل بين الإجابات (ثواني)
PREFETCH = max(1, int(os.getenv("QUREO_PREFETCH", "4")))     # طلبات قراءة متوازية للدروس
try:
    import h2  # noqa: F401
    _HTTP2 = True
except ImportError:
    _HTTP2 = False
RETRY_STATUSES = {429, 500, 502, 503, 504}
UNMATCHED_POLICY = os.getenv("UNMATCHED_POLICY", "guess").strip().lower()  # guess | stop
# حفظ إجابات الدرس الواحد بالتوازي (1 = متتابع، الافتراضي). لو اتفعّل وفشل أي حفظ بيتعاد متتابعًا قبل ما نقول فشل.
TALK_PARALLEL = max(1, int(os.getenv("QUREO_TALK_PARALLEL", "1")))


class Stopped(RuntimeError):
    pass


class LoginFailed(RuntimeError):
    pass


class PlatformError(RuntimeError):
    def __init__(self, status, path, payload=None):
        super().__init__(f"Qureo HTTP {status}: {path}")
        self.status = status
        self.path = path
        self.payload = payload


class UnknownStructure(RuntimeError):
    pass


class UnmatchedAnswer(RuntimeError):
    def __init__(self, question=""):
        super().__init__(question)
        self.question = question


class _Ctx:
    """حالة الشغل الواحد: الإجابات المتعلَّمة + عدّادات الأداء."""

    def __init__(self, learned=None):
        self.learned = learned
        self.guessed = 0
        self.retried = 0
        self.learned_new = 0


_js_learned = None
_js_learned_lock = threading.Lock()


def get_js_learned():
    """مخزن إجابات JavaScript المتعلَّمة منفصل عن Python/المالية (نفس نص السؤال ممكن إجابته تختلف)."""
    global _js_learned
    with _js_learned_lock:
        if _js_learned is None:
            path = os.getenv("LEARNED_JS_FILE") or str(
                Path(os.getenv("DB_PATH", "bot.db")).resolve().parent / "learned_js_answers.json"
            )
            _js_learned = LearnedAnswers(path)
        return _js_learned


def _safe_learned(js=False):
    try:
        return get_js_learned() if js else get_learned()
    except Exception:  # noqa: BLE001
        _log.warning("learned answers unavailable", exc_info=True)
        return None


def _guess_index(option_texts, excluded=()):
    """سؤال مش في البنك: أطول خيار (الصح غالبًا الأكثر تفصيلًا) بعد استبعاد اللي اتجرّب وطلع غلط."""
    if not option_texts:
        return 0
    pool = [i for i in range(len(option_texts)) if i not in excluded] or list(range(len(option_texts)))
    return max(pool, key=lambda i: (len(option_texts[i]), -i))


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Stopped()


def _sleep(cancel, seconds):
    if seconds <= 0:
        return
    if cancel is not None:
        if cancel.wait(seconds):
            raise Stopped()
    else:
        time.sleep(seconds)


def _say(hook, kind, text=""):
    if hook:
        try:
            hook(kind, text)
        except Exception:  # noqa: BLE001
            pass


_JS_MODE = contextvars.ContextVar("qureo_js_mode", default=False)


def _clean(value):
    if _JS_MODE.get():
        # دورة JavaScript: فيها HTML/CSS فمينفعش نشيل الوسوم <...>، وفيها علامات [[bold]] [[code]] [[img]]
        text = html.unescape(str(value or ""))
        text = re.sub(r"\[\[img\]\].*?\[\[/img\]\]", " ", text, flags=re.I | re.S)
        text = re.sub(r"\[\[/?(?:bdi|copy|bold|code)\]\]", "", text, flags=re.I)
        return " ".join(text.split())
    text = html.unescape(str(value or ""))
    text = re.sub(r"\[\[/?(?:bdi|copy)\]\]", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


_CODE_START = re.compile(
    r"#|(?:print|input|range|len|str|int|float|round|min|max|sum|abs)\s*\(|"
    r"(?:if|elif|else|for|while|def|class|return|import|from|try|except|finally|with)\b|"
    r"[A-Za-z_]\w*(?:\[[^\]]+\])?\s*(?:==|!=|>=|<=|\+=|-=|\*=|/=|%=|=|>|<)|"
    r"(?<!\w)\d[\d\s()+*/%.-]*[+*/%\-][\d\s()+*/%.-]*"
)
_ARABIC = re.compile(r"[\u0600-\u06ff]")


def _trim_inline_code(line):
    if line.lstrip().startswith("#"):
        return line.rstrip()
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif _ARABIC.match(char):
            return line[:index].rstrip()
    return line.rstrip()


def extract_code_answer(answer):
    """Keep the longest contiguous Python-looking snippet from a solution entry."""
    blocks = []
    current = []
    pending_blank = False

    def finish():
        nonlocal current, pending_blank
        if current:
            blocks.append("\n".join(current).strip())
        current = []
        pending_blank = False

    for raw in str(answer or "").splitlines():
        match = _CODE_START.search(raw)
        if match:
            prefix = raw[:match.start()]
            line = (prefix if not prefix.strip() else "") + raw[match.start():]
            line = _trim_inline_code(line)
            if line.strip():
                if pending_blank and current:
                    current.append("")
                current.append(line)
                pending_blank = False
                continue
        if not raw.strip() and current:
            pending_blank = True
            continue
        finish()
    finish()

    candidates = [
        block for block in blocks
        if block and not all(re.fullmatch(r"[.\s…_-]+", line) for line in block.splitlines())
    ]
    if not candidates:
        return ""
    return max(candidates, key=lambda block: (len(block.splitlines()), len(block)))


def parse_answer_file(path):
    """Parse the supplied Qureo text bank while preserving code line breaks."""
    if Path(path).suffix.casefold() == ".md":
        return parse_js_answer_file(path)
    question_lines = []
    answer_lines = []
    state = "seek"
    entries = []

    def save_entry():
        question = " ".join(line.strip() for line in question_lines if line.strip())
        answer = "\n".join(line.rstrip() for line in answer_lines if line.strip())
        if question and answer:
            entries.append((question, extract_code_answer(answer) or answer.strip()))

    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        marker = matching.QUESTION_RE.match(line)
        plain_question = line == "سؤال" or re.match(r"^سؤال\s+", line)
        if plain_question:
            marker = re.match(r"^سؤال(?:\s+(.*))?$", line)
        if marker:
            if state == "answer":
                save_entry()
            question_lines = [marker.group(1) or ""]
            answer_lines = []
            state = "question"
            continue
        if line in matching.ANSWER_LABELS:
            if state == "question":
                state = "answer"
            continue
        if state == "answer" and line.startswith(
            ("المستوي", "المستوى", "الدرس", "الوحدة", "نضغط ", "حل اختبار", "اختبار المعرفة", "---")
        ):
            save_entry()
            question_lines = []
            answer_lines = []
            state = "seek"
            continue
        if state == "question" and line:
            question_lines.append(line)
        elif state == "answer" and line:
            answer_lines.append(raw_line)

    if state == "answer":
        save_entry()
    if not entries:
        raise ValueError(f"No Qureo Python answers were found in {Path(path).name}.")
    return matching.AnswerIndex(entries)


# ───────────── بنك إجابات JavaScript (ملف Markdown مجمّع) ─────────────

_META_WORDS = ("الدرس", "المستوى", "المستوي", "مراجعة", "اختبار", "الاختبار", "السؤال", "المهمة")
_JS_NO_ANSWER = (
    "لا يمكن", "تعليمات الصفحة", "انقر", "اضغط", "تشغيل الكود", "لا تغيّر", "لا تغير", "لم أ", "لم يتم",
    "غير ظاهر", "لا تظهر", "لا يوجد", "لا توجد",
)
_BACKTICKS = re.compile(r"`([^`]+)`")
_TRAILING_NOTE = re.compile(r"\s*\([^()]*[\u0600-\u06ff][^()]*\)\s*\.?\s*$")
_CODE_CHARS = re.compile(r"[;{}=<>()\[\]]")


def _strip_meta_prefix(question):
    """يشيل «(الدرس الثالث، السؤال الأول)» / «مراجعة المستوى 32، السؤال 3:» من أول السؤال."""
    text = question.strip()
    for _ in range(3):
        before = text
        match = re.match(r"^[(\[]([^)\]]{0,60})[)\]]\s*[:：—-]?\s*", text)
        if match and any(word in match.group(1) for word in _META_WORDS):
            text = text[match.end():].strip()
        match = re.match(r"^([^:：—`]{0,70}?)\s*[:：—]\s+", text)
        if match and any(word in match.group(1) for word in _META_WORDS):
            text = text[match.end():].strip()
        if text == before:
            break
    return text or question.strip()


def _looks_like_code(text):
    """كود لو مفيهوش عربي برّه النصوص المقتبسة / الوسوم، وفيه علامات كود."""
    bare = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'|`[^`]*`|>[^<\n]*<|//[^\n]*", "", text)
    if _ARABIC.search(bare):
        return False
    return bool(_CODE_CHARS.search(text)) or bool(re.search(r"\w+\.\w+|^\s*\w+\s*$", text))


def _clean_js_answer(answer):
    """ينضّف إجابة بنك JS: يطلّع الكود من علامات ` ` ويشيل الشرح العربي الزائد."""
    text = str(answer or "").strip()
    if not text:
        return ""
    segments = _BACKTICKS.findall(text)
    if segments:
        # لو الإجابة كلها `كود` أو كود + شرح: ناخد أطول مقطع.
        text = max(segments, key=len).strip("\n ")
    else:
        kept = []
        for line in text.splitlines():
            if _ARABIC.search(line) and not _CODE_CHARS.search(line):
                continue   # سطر شرح عربي صريح
            kept.append(line.rstrip())
        text = "\n".join(kept).strip("\n") or text
        text = _TRAILING_NOTE.sub("", text).strip()
    if "\n" not in text and not _CODE_CHARS.search(text):
        text = text.rstrip(".。 ")    # إجابة اختيار نصية: من غير نقطة النهاية
    return text


def parse_js_answer_file(path):
    """بنك JavaScript بصيغة Markdown (### السؤال / **الإجابة:** / **الشرح:**) → AnswerIndex."""
    blocks = []
    block = None
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if raw.startswith("### السؤال"):
            block = []
            blocks.append(block)
        elif raw.startswith("## "):
            block = None
        elif block is not None:
            block.append(raw)

    entries = []
    for lines in blocks:
        question_lines, answer_lines, state, fenced = [], [], "q", False
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("**الإجابة:**"):
                state = "a"
                continue
            if stripped.startswith("**الشرح:**"):
                state = "x"
                continue
            if state == "x":
                continue
            if stripped.startswith("````"):
                fenced = not fenced
                continue
            target = question_lines if state == "q" else answer_lines
            target.append(raw.rstrip() if fenced else stripped)
        question = _strip_meta_prefix(" ".join(x.strip() for x in question_lines if x.strip()))
        raw_answer = "\n".join(answer_lines).strip("\n")
        if not question or not raw_answer.strip():
            continue
        if raw_answer.lstrip("` \n").startswith(_JS_NO_ANSWER):
            continue   # المصدر نفسه مقدرش يحدد الإجابة
        answer = _clean_js_answer(raw_answer)
        if answer:
            entries.append((question, answer))
    if not entries:
        raise ValueError(f"No Qureo JavaScript answers were found in {Path(path).name}.")
    return matching.AnswerIndex(entries)


def _set_course_cookie(client, course_id):
    if hasattr(client, "http"):
        client.http.cookies.set(
            "student_selected_content_id",
            course_id,
            domain="me-tp.qureo.education",
            path="/",
        )


class QureoClient:
    def __init__(self, cancel=None, course="python", course_id=None):
        self.cancel = cancel
        profile = COURSES[course]
        self.course_key = course
        self.course_id = course_id or profile["id"]
        self.lang = profile["lang"]
        self.course_label = profile["label"]
        self.http = httpx.Client(
            http2=_HTTP2,
            limits=httpx.Limits(
                max_connections=PREFETCH + 2, max_keepalive_connections=PREFETCH + 2
            ),
            timeout=httpx.Timeout(connect=10, read=30, write=15, pool=10),
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                "Origin": PORTAL_URL,
                "Referer": f"{PORTAL_URL}/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        )
        self.stats = {}
        self._stats_lock = threading.Lock()
        self._creds = None

    def close(self):
        self.http.close()

    def request_count(self):
        return sum(count for count, _seconds in self.stats.values())

    def timing_report(self, top=8):
        rows = sorted(self.stats.items(), key=lambda item: item[1][1], reverse=True)
        return [(path, count, round(seconds, 2)) for path, (count, seconds) in rows[:top]]

    def _request(
        self,
        method,
        base_url,
        path,
        *,
        body=None,
        params=None,
        allow_404=False,
        expect_json=True,
        follow_redirects=True,
        relogin=True,
        allow_scalar=False,
    ):
        last_error = None
        for attempt in range(HTTP_RETRIES + 1):
            _check(self.cancel)
            started = time.monotonic()
            try:
                response = self.http.request(
                    method,
                    base_url + path,
                    json=body,
                    params=params,
                    follow_redirects=follow_redirects,
                )
            except httpx.TransportError:
                last_error = PlatformError(0, path)
                _sleep(self.cancel, min(8.0, 0.6 * (2 ** attempt)))
                continue
            finally:
                key = f"{method} {path}"
                with self._stats_lock:
                    stats = self.stats.setdefault(key, [0, 0.0])
                    stats[0] += 1
                    stats[1] += time.monotonic() - started

            if response.status_code == 404 and allow_404:
                return None
            if response.status_code in RETRY_STATUSES and attempt < HTTP_RETRIES:
                try:
                    wait = float(response.headers.get("retry-after", "0"))
                except ValueError:
                    wait = 0.0
                last_error = PlatformError(response.status_code, path)
                _sleep(self.cancel, min(15.0, max(wait, 0.6 * (2 ** attempt))))
                continue
            if response.status_code == 401 and relogin and self._creds:
                self.login(*self._creds)   # الجلسة انتهت: ندخل تاني مرة واحدة
                return self._request(
                    method, base_url, path,
                    body=body, params=params, allow_404=allow_404,
                    expect_json=expect_json, follow_redirects=follow_redirects,
                    relogin=False, allow_scalar=allow_scalar,
                )
            if response.status_code >= 400:
                payload = None
                try:
                    payload = response.json()
                except ValueError:
                    pass
                raise PlatformError(response.status_code, path, payload)
            if not expect_json or response.status_code == 204 or not response.content:
                return {}
            try:
                payload = response.json()
            except ValueError:
                raise UnknownStructure(f"Qureo returned non-JSON data for {path}.") from None
            is_number = isinstance(payload, (int, float)) and not isinstance(payload, bool)
            if not isinstance(payload, (dict, list)) and not (allow_scalar and is_number):
                raise UnknownStructure(f"Unexpected Qureo response shape for {path}.")
            return payload
        raise last_error or PlatformError(0, path)

    def _get(
        self,
        path,
        *,
        params=None,
        allow_404=False,
        expect_json=True,
        follow_redirects=True,
        relogin=True,
        allow_scalar=False,
    ):
        return self._request(
            "GET",
            TP_URL,
            path,
            params=params,
            allow_404=allow_404,
            expect_json=expect_json,
            follow_redirects=follow_redirects,
            relogin=relogin,
            allow_scalar=allow_scalar,
        )

    def _put(self, path, body=None):
        return self._request("PUT", TP_URL, path, body=body)

    def login(self, student_id, password):
        course_key = getattr(self, "course_key", "python")
        course_id = getattr(self, "course_id", COURSES[course_key]["id"])
        label = getattr(self, "course_label", COURSES[course_key]["label"])
        try:
            self._request(
                "POST",
                PORTAL_URL,
                "/oauth2/password-login",
                body={"student_id": student_id.strip(), "password": password},
                expect_json=False,
                relogin=False,
            )
        except PlatformError as exc:
            if exc.status in (400, 401, 403, 404, 422):
                raise LoginFailed() from None
            raise

        self._get(
            "/oauth2/refresh",
            params={
                "redirect_url": "/api/study/students/me",
                "fallback_url": TP_URL + "/login",
            },
            expect_json=False,
            follow_redirects=False,
            relogin=False,
        )
        _set_course_cookie(self, course_id)
        # لو معرّف الدورة مش مظبوط (JavaScript) نكتشفه من الدورات المتاحة في userinfo.
        try:
            userinfo = self._get("/oauth2/userinfo", relogin=False)
        except UnknownStructure:
            try:
                userinfo = self._request(
                    "GET",
                    PORTAL_URL,
                    "/oauth2/userinfo",
                    relogin=False,
                )
            except (PlatformError, UnknownStructure):
                userinfo = {}
        available = userinfo.get("available_courses") or []
        available_ids = [
            str(course.get("content_id") or course.get("id"))
            for course in available
            if isinstance(course, dict) and (course.get("content_id") or course.get("id"))
        ]
        if available_ids and course_id not in available_ids:
            hints = COURSES[course_key]["hints"]
            found = next(
                (cid for cid in available_ids
                 if any(h in cid.lower() for h in hints) and "trial" not in cid.lower()),
                None
            )
            if found:
                course_id = found
                _set_course_cookie(self, course_id)
                try:
                    userinfo = self._get("/oauth2/userinfo", relogin=False)
                except (PlatformError, UnknownStructure):
                    pass
        if available and course_id not in available_ids:
            raise PlatformError(403, "course access")
        selected = userinfo.get("selected_course")
        selected_id = (
            selected.get("content_id") or selected.get("id")
            if isinstance(selected, dict) else selected
        )
        if selected_id and selected_id != course_id:
            raise PlatformError(409, f"select {label} course")
        try:
            student = self._get("/api/study/students/me", relogin=False)
        except UnknownStructure:
            student = {"course_code": course_id, "name": userinfo.get("name")}
        if not isinstance(student, dict):
            student = {"course_code": course_id, "name": userinfo.get("name")}
        if student.get("course_code") and student["course_code"] != course_id:
            raise PlatformError(409, f"select {label} course")
        self.course_id = course_id
        self._creds = (student_id, password)
        return student

    def course(self):
        return self._get(f"/api/study/courses/{self.course_id}")

    def section(self, section_id):
        return self._get(f"/api/study/sections/{section_id}")

    def course_diamond(self):
        """إجمالي الألماس (الجواهر) المكتسب في الكورس: رقم واحد (متأكَّد من capture: GET .../courses/<id>/diamond)."""
        return self._get(f"/api/study/students/courses/{self.course_id}/diamond", allow_scalar=True)

    def section_progress(self, section_id):
        return self._get(f"/api/study/students/sections/{section_id}/chapters")

    def chapter_access(self, chapter_id):
        # Returns {"not_contracted": bool, "not_fulfill_preconditions": bool, "values": [...], ...}
        return self._get(f"/api/study/students/chapters/{chapter_id}/cookies")

    def chapter(self, chapter_id):
        return self._get(f"/api/study/chapters/{chapter_id}")

    def lecture_progress(self, chapter_id):
        return self._get(f"/api/study/students/chapters/{chapter_id}/lectures")

    def lecture(self, lecture_id):
        return self._get(f"/api/study/lectures/{lecture_id}")

    def student_lecture(self, lecture_id):
        return self._get(
            f"/api/study/students/lectures/{lecture_id}",
            allow_404=True,
        )

    def save_lecture_talk(self, talk_id, value):
        return self._put(
            f"/api/study/students/lecture-talks/{talk_id}",
            {"value": value},
        )

    def complete_lecture(self, lecture_id):
        return self._put(f"/api/study/students/lectures/{lecture_id}/complete")

    def start_lecture(self, lecture_id):
        # API spec shows no request body for this PUT — send without body
        return self._put(f"/api/study/students/lectures/{lecture_id}")

    def test_questions(self, chapter_id):
        return self._get(f"/api/study/chapters/{chapter_id}/test")

    def test_state(self, chapter_id):
        return self._get(f"/api/study/students/chapters/{chapter_id}/test")

    def start_test(self, chapter_id):
        return self._put(f"/api/study/students/chapters/{chapter_id}/test")

    def answer_choice(self, question_id, choice_id):
        return self._put(
            f"/api/study/students/review-questions/{question_id}/answer/choice",
            {"choice_id": choice_id},
        )

    def answer_description(self, question_id, answer_code):
        return self._put(
            f"/api/study/students/review-questions/{question_id}/answer/description",
            {"answer_code": answer_code},
        )

    def answer_test_choice(self, question_id, choice_id):
        return self._put(
            f"/api/study/students/test-questions/{question_id}/answer/choice",
            {"choice_id": choice_id},
        )

    def answer_test_description(self, question_id, answer_code):
        return self._put(
            f"/api/study/students/test-questions/{question_id}/answer/description",
            {"answer_code": answer_code},
        )

    def review_result(self, chapter_id):
        return self._get(
            f"/api/study/students/chapters/{chapter_id}/review/result",
            allow_404=True,
        )

    def test_result(self, chapter_id):
        # API spec only has /review/result — try it first to avoid a wasted 404
        review_path = f"/api/study/students/chapters/{chapter_id}/review/result"
        test_path = f"/api/study/students/chapters/{chapter_id}/test/result"
        # دورة JavaScript: صفحة الاختبار النهائي نفسها بتقرأ /test/result (متأكَّد من الـ capture)
        for path in ((test_path, review_path) if getattr(self, "lang", "python") == "javascript"
                     else (review_path, test_path)):
            result = self._get(path, allow_404=True)
            if result is not None:
                if isinstance(result, dict) and isinstance(result.get("result"), dict):
                    inner = dict(result["result"])
                    histories = result.get("histories") or []
                    if not inner.get("correct_count") and histories:
                        latest = histories[-1]
                        results = latest.get("results") or []
                        if isinstance(results, list):
                            inner["correct_count"] = sum(
                                1 for value in results if value is True
                            )
                    return inner
                return result
        return None


def _talk_saved_values(data):
    values = {}
    if not isinstance(data, dict):
        return values
    for record in data.get("talks") or []:
        if isinstance(record, dict) and record.get("talk_id") is not None:
            values[str(record["talk_id"])] = record.get("value")
    return values


_AS_IS = re.compile(r"كما\s+هو\s*:?\s*`([^`]+)`")


def _js_answer_code(question, entries):
    """JavaScript: البنك نظيف أصلًا (parse_js_answer_file)، فمفيش استخراج كود من نص عربي."""
    as_is = _AS_IS.search(question)
    if as_is:
        return as_is.group(1).strip()
    copy_text = re.search(r"نسخ\s+النص:\s*(.+)", question)
    if copy_text and copy_text.group(1).strip():
        return f'console.log("{copy_text.group(1).strip()}");'
    try:
        answer, _source = matching.match_answer(question, entries)
    except matching.AnswerMatchError as exc:
        raise UnmatchedAnswer(question) from exc
    code = str(answer).strip()
    if not code or not _looks_like_code(code):
        raise UnmatchedAnswer(question)
    return code


def _answer_code(question, entries, lang="python"):
    if lang == "javascript":
        return _js_answer_code(question, entries)
    copy_text = re.search(r"نسخ\s+النص:\s*(.+)", question)
    if copy_text:
        text = copy_text.group(1).strip()
        if text:
            return f'print("{text}")'

    assignment_and_print = re.search(
        r"أسند\s+(-?\d+(?:\.\d+)?)\s+إلى\s+(?:المتغير\s+)?([A-Za-z_]\w*).*?"
        r"اطبع\s+قيمة\s+(?:المتغير\s+)?([A-Za-z_]\w*)",
        question,
        re.S,
    )
    if assignment_and_print:
        value, assigned_variable, printed_variable = assignment_and_print.groups()
        if assigned_variable == printed_variable:
            return f"{assigned_variable} = {value}\nprint({printed_variable})"

    variable_assignment = re.search(
        r"أسند\s+(-?\d+(?:\.\d+)?)\s+إلى\s+المتغير\s+([A-Za-z_]\w*)",
        question,
    )
    if variable_assignment:
        value, variable = variable_assignment.groups()
        return f"{variable} = {value}"

    generic_assignment = re.search(
        r"أسند\s+(-?\d+(?:\.\d+)?)\s+إلى\s+([A-Za-z_]\w*)",
        question,
    )
    if generic_assignment:
        value, variable = generic_assignment.groups()
        return f"{variable} = {value}"

    print_variable = re.search(
        r"اطبع\s+قيمة\s+المتغير\s+([A-Za-z_]\w*)",
        question,
    )
    if print_variable:
        return f"print({print_variable.group(1)})"

    print_variable_generic = re.search(
        r"اطبع\s+قيمة\s+([A-Za-z_]\w*)",
        question,
    )
    if print_variable_generic:
        return f"print({print_variable_generic.group(1)})"

    print_string = re.search(
        r'اطبع\s+["“”«»\'\u060c]([^"“”«»\'\u060c]+)["“”«»\'\u060c]|\bhello\b',
        question,
    )
    if print_string:
        text = print_string.group(1) if print_string.group(1) else "hello"
        return f'print("{text.strip()}")'

    try:
        answer, _source = matching.match_answer(question, entries)
    except matching.AnswerMatchError as exc:
        raise UnmatchedAnswer(question) from exc
    extracted = extract_code_answer(answer)
    code = extracted or answer.strip()
    if not code or (not extracted and _ARABIC.search(code)):
        raise UnmatchedAnswer(question)
    return code


def _placeholder_code(lang):
    """سؤال كتابة كود مش في البنك ومفيش كود مبدئي: نبعت محاولة بسيطة بدل ما نقف —
    المنصة بتكشف الإجابة النموذجية في ردّها/نتيجة المراجعة والبوت بيتعلمها ويعيد بيها."""
    return "console.log();" if lang == "javascript" else "print()"


def resolve_choice(question_text, options, entries):
    """Resolve an MCQ answer first by exact raw text, then by the existing matcher."""
    answer = None
    try:
        answer, _source = matching.match_answer(question_text, entries)
    except matching.AnswerMatchError:
        pass

    if answer:
        answer_text = _clean(answer).strip()
        exact_raw = [
            index
            for index, option in enumerate(options)
            if _clean(option).strip().casefold() == answer_text.casefold()
        ]
        if len(exact_raw) == 1:
            return exact_raw[0]

    normalized_options = [matching.normalize_text(option) for option in options]

    assignments = re.findall(
        r"([A-Za-z_]\w*)\s*=\s*(-?\d+(?:\.\d+)?)",
        question_text,
    )
    branch_pattern = re.compile(
        r"(?:if|elif)\s+([A-Za-z_]\w*)\s*(==|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)"
        r"\s*:\s*print\s*\(\s*[\"']([^\"']+)",
        re.I,
    )
    branch_matches = branch_pattern.findall(question_text)
    if assignments and branch_matches:
        assigned = dict(assignments)
        for variable, operator, expected_text, printed_text in branch_matches:
            if variable not in assigned:
                continue
            actual = float(assigned[variable])
            expected = float(expected_text)
            matched = (
                (operator == "==" and actual == expected)
                or (operator == ">=" and actual >= expected)
                or (operator == "<=" and actual <= expected)
                or (operator == ">" and actual > expected)
                or (operator == "<" and actual < expected)
            )
            if matched:
                printed_norm = matching.normalize_text(printed_text)
                for index, option in enumerate(normalized_options):
                    if matching.normalize_text(option) == printed_norm:
                        return index
    if "افعل" in matching.normalize_text(question_text) or "else" in matching.normalize_text(question_text):
        for index, option in enumerate(normalized_options):
            padded = f" {option} "
            if option.startswith("if condition"):
                a_pos = padded.find(" a ")
                else_pos = padded.find(" else ")
                b_pos = padded.find(" b ")
                if 0 <= a_pos < else_pos < b_pos:
                    return index
            if re.search(r"\bif\b.*\bA\b.*\belse\b.*\bB\b", f" {option} ", re.I):
                return index
            if "if condition" in option and " A " in f" {option} " and " else " in f" {option} " and " B" in option:
                return index
    if "true" in normalized_options and "false" in normalized_options:
        assignments = re.findall(
            r"([A-Za-z_]\w*)\s*=\s*(-?\d+(?:\.\d+)?)",
            question_text,
        )
        comparison = re.search(
            r"(?:print\s*\(\s*)?([A-Za-z_]\w*)\s*==\s*(-?\d+(?:\.\d+)?)",
            question_text,
        )
        if comparison:
            variable, expected = comparison.groups()
            assigned = next(
                (
                    value
                    for name, value in assignments
                    if name == variable
                ),
                None,
            )
            if assigned is not None:
                result = str(float(assigned) == float(expected)).casefold()
                for index, option in enumerate(normalized_options):
                    if option == result:
                        return index

    return matching.resolve_answer(
        question_text,
        entries,
        options,
    )[0]


def _choose_option(question_text, option_texts, entries, ctx, allow_guess):
    """يرجّع (رقم_الخيار, المصدر) — المصدر: learned | bank | guess.
    أي خيار المنصة أثبتت إنه غلط (حتى لو جاي من البنك) بيتتجاهل وبنخمّن من الباقي."""
    learned = ctx.learned if ctx is not None else None
    excluded = learned.wrong_indices(question_text, option_texts) if learned is not None else set()
    if learned is not None:
        index = learned.lookup(question_text, option_texts)
        if index is not None and index not in excluded:
            return index, "learned"
    try:
        index = resolve_choice(question_text, option_texts, entries)
        if index not in excluded:
            return index, "bank"
    except matching.AnswerMatchError as exc:
        if allow_guess:
            matching.record_unmatched("qureo", question_text, option_texts)
        if not allow_guess or UNMATCHED_POLICY == "stop":
            raise UnmatchedAnswer(question_text) from exc
    return _guess_index(option_texts, excluded), "guess"


def _prefetch_lectures(client, lecture_ids, cancel):
    """يجيب محتوى الدروس وحالتها بالتوازي (GET فقط). أي فشل بيتجاهل والدرس بيتجلب عادي وقت حلّه."""
    out = {}
    if PREFETCH < 2 or len(lecture_ids) < 2:
        return out

    def load(lecture_id):
        _check(cancel)
        return client.lecture(lecture_id), client.student_lecture(lecture_id)

    with ThreadPoolExecutor(max_workers=min(PREFETCH, len(lecture_ids))) as pool:
        futures = {lid: pool.submit(load, lid) for lid in lecture_ids}
        for lid, future in futures.items():
            try:
                out[lid] = future.result()
            except Stopped:
                raise
            except Exception:  # noqa: BLE001
                pass
    return out


def _load_chapter(client, chapter_id, cancel):
    """بيانات الفصل وحالة دروسه (طلبين GET مستقلين) في نفس الوقت بدل ورا بعض.
    الأخطاء بتطلع بنفس الترتيب القديم: خطأ بيانات الفصل الأول، وبعده خطأ حالة الدروس."""
    if PREFETCH < 2:
        return client.chapter(chapter_id), client.lecture_progress(chapter_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        chapter_future = pool.submit(client.chapter, chapter_id)
        states_future = pool.submit(client.lecture_progress, chapter_id)
        try:
            chapter_data = chapter_future.result()
        except BaseException:
            states_future.cancel()
            raise
        return chapter_data, states_future.result()


def _confirm_lectures(client, chapter_id, lecture_ids, title):
    """طلب واحد للفصل كله يتأكد إن المنصة سجّلت اكتمال كل الدروس."""
    states = {
        str(item.get("lecture_id")): item
        for item in client.lecture_progress(chapter_id)
        if isinstance(item, dict) and item.get("lecture_id") is not None
    }
    missing = [
        str(lid) for lid in lecture_ids if not states.get(str(lid), {}).get("completed_at")
    ]
    if missing:
        raise UnknownStructure(
            f"Qureo did not confirm lecture completion: {title} ({', '.join(missing)})."
        )


def _flush_talks(client, pending, hook, cancel):
    """يحفظ إجابات الدرس المتجمّعة بالتوازي. أي حفظ فشل (غير الإيقاف) بيتعاد متتابعًا بنفس الترتيب،
    فلو المنصة رفضت التوازي لأي سبب الحل يكمّل زي الأول بدل ما يفشل."""
    if not pending:
        return
    items = list(pending)
    pending.clear()

    def one(item):
        _check(cancel)
        client.save_lecture_talk(*item)

    failed = []
    with ThreadPoolExecutor(max_workers=min(TALK_PARALLEL, len(items))) as pool:
        futures = [(item, pool.submit(one, item)) for item in items]
        for item, future in futures:
            try:
                future.result()
            except Stopped:
                raise
            except Exception:  # noqa: BLE001
                failed.append(item)
                continue
            _say(hook, "q_ok", "")
    for item in failed:                      # إعادة متتابعة؛ لو فشلت تاني الخطأ بيطلع عادي
        _check(cancel)
        client.save_lecture_talk(*item)
        _say(hook, "q_ok", "")
        _sleep(cancel, ANSWER_DELAY)


def _solve_lecture(
    client, lecture_id, title, entries, hook, cancel, ctx=None, prefetched=None, confirm=True
):
    ctx = ctx or _Ctx()
    if prefetched:
        content, student_state = prefetched
    else:
        content = client.lecture(lecture_id)
        student_state = client.student_lecture(lecture_id)
    talks = content.get("talks")
    if not isinstance(talks, list):
        raise UnknownStructure(f"Lecture {lecture_id} has no talks list.")
    if not isinstance(student_state, dict):
        client.start_lecture(lecture_id)
        student_state = client.student_lecture(lecture_id)
    saved = _talk_saved_values(student_state)
    pending = []      # (talk_id, value) — لو QUREO_TALK_PARALLEL > 1 بتتبعت دفعة واحدة بالتوازي قبل إكمال الدرس

    def save(talk_id, value):
        if TALK_PARALLEL > 1:
            pending.append((talk_id, value))
            return
        client.save_lecture_talk(talk_id, value)
        _say(hook, "q_ok", "")
        _sleep(cancel, ANSWER_DELAY)

    for talk in talks:
        _check(cancel)
        if not isinstance(talk, dict):
            continue
        talk_id = talk.get("id")
        if talk_id is None:
            raise UnknownStructure(f"Interactive talk in lecture {lecture_id} has no id.")
        if saved.get(str(talk_id)):
            continue

        body = talk.get("body") or {}
        if not isinstance(body, dict):
            raise UnknownStructure(f"Unexpected talk body in lecture {lecture_id}.")

        talk_type = talk.get("type")
        if talk_type == "choice_question":
            question_text = _clean(body.get("question"))
            choices = body.get("choices")
            if not question_text or not isinstance(choices, list) or not choices:
                raise UnknownStructure(f"Choice talk is incomplete in lecture {lecture_id}.")
            valid_choices = [
                choice for choice in choices
                if isinstance(choice, dict)
            ]
            option_texts = [
                _clean(choice.get("label") or choice.get("content") or choice.get("text"))
                for choice in valid_choices
            ]
            index, source = _choose_option(
                question_text, option_texts, entries, ctx, allow_guess=True
            )
            if source == "guess":
                ctx.guessed += 1
            choice = valid_choices[index]
            selected = choice.get("value")
            if selected is None:
                raise UnknownStructure(
                    f"Choice talk has no value in lecture {lecture_id}."
                )
            save(talk_id, {"choice_survey": {"value": str(selected)}})
            continue

        if talk_type == "choice_survey" and _JS_MODE.get():
            # استبيان رأي في دروس JavaScript (مفيهوش إجابة صح): نختار أول خيار ونحفظه
            options = [
                c for c in (body.get("choices") or [])
                if isinstance(c, dict) and c.get("value") is not None
            ]
            if options:
                save(talk_id, {"choice_survey": {"value": str(options[0]["value"])}})
            continue

        if talk_type not in ("student", "try", "code"):
            continue

        codes = talk.get("codes") or body.get("codes") or []
        editable = [
            item for item in codes
            if isinstance(item, dict) and item.get("editable")
        ]
        if editable:
            code_item = editable[0]
            answer = html.unescape(str(code_item.get("model_answer") or ""))
            answer = re.sub(r"\[\[/?(?:bdi|copy)\]\]", "", answer, flags=re.I).strip()
            if not answer:
                prompt = talk.get("question") or body.get("question") or body.get("serif")
                if isinstance(prompt, dict):
                    prompt = prompt.get("content") or prompt.get("text") or prompt.get("question")
                question = _clean(
                    prompt
                )
                try:
                    answer = _answer_code(question, entries, getattr(client, "lang", "python"))
                except UnmatchedAnswer:
                    # نشاط تدريبي: نحفظ الكود المبدئي، وتأكيد اكتمال الدرس بعده بيكشف لو المنصة رفضته
                    answer = html.unescape(str(code_item.get("code") or "")).strip()
                    answer = re.sub(r"\[\[/?(?:bdi|copy)\]\]", "", answer, flags=re.I).strip()
                    if not answer:
                        answer = _placeholder_code(getattr(client, "lang", "python"))
                    ctx.guessed += 1
            lang = str(code_item.get("lang") or getattr(client, "lang", "python"))
            save(talk_id, {"try": {"lang": lang, "answer": answer}})
            continue

        question = talk.get("question") or body.get("question")
        choices = talk.get("choices") or body.get("choices") or []
        if isinstance(question, (str, dict)) and isinstance(choices, list) and choices:
            question_text = _clean(
                question.get("content") or question.get("text")
                if isinstance(question, dict) else question
            )
            valid_choices = [
                choice for choice in choices
                if isinstance(choice, dict)
            ]
            option_texts = [
                _clean(choice.get("content") or choice.get("text") or choice.get("label"))
                for choice in valid_choices
            ]
            index, source = _choose_option(
                question_text, option_texts, entries, ctx, allow_guess=True
            )
            if source == "guess":
                ctx.guessed += 1
            choice = valid_choices[index]
            selected = choice.get("value")
            if selected is None:
                raise UnknownStructure(
                    f"Survey choice has no documented value in lecture {lecture_id}."
                )
            save(talk_id, {"choice_survey": {"value": str(selected)}})

    _flush_talks(client, pending, hook, cancel)
    client.complete_lecture(lecture_id)
    if not confirm:
        return
    lecture_data = content.get("lecture") or {}
    chapter_id = lecture_data.get("chapter_id")
    if chapter_id is None:
        raise UnknownStructure(f"Chapter id missing from lecture {lecture_id}.")
    refreshed = client.lecture_progress(chapter_id)
    state_by_id = {
        str(item.get("lecture_id")): item
        for item in refreshed
        if isinstance(item, dict) and item.get("lecture_id") is not None
    }
    state = state_by_id.get(str(lecture_id))
    if not state or not state.get("completed_at"):
        raise UnknownStructure(f"Qureo did not confirm lecture completion: {title}.")


def _result_of(response):
    return response.get("result") if isinstance(response, dict) else None


def _correct_choice_id(payload):
    """id الخيار الصح اللي المنصة كشفته في ردّ إجابة أو نتيجة مراجعة."""
    for candidate in (payload or {}).get("choices") or []:
        if (
            isinstance(candidate, dict)
            and candidate.get("correct") is True
            and isinstance(candidate.get("choice"), dict)
            and candidate["choice"].get("id") is not None
        ):
            return candidate["choice"]["id"]
    return None


def _harvest_corrections(result, answers, choices):
    """يلتقط الإجابات الصحيحة اللي المنصة كشفتها في نتيجة المراجعة."""
    for item in result.get("description_results") or []:
        if isinstance(item, dict) and item.get("question_id") is not None:
            model_answer = str(item.get("model_answer") or "").strip()
            if model_answer:
                answers[str(item["question_id"])] = model_answer
    for item in result.get("choice_results") or []:
        if not isinstance(item, dict) or item.get("question_id") is None:
            continue
        correct_id = _correct_choice_id(item)
        if correct_id is not None:
            choices[str(item["question_id"])] = correct_id


def _submit(client, is_test, kind, question_id, value):
    """يبعت إجابة (kind = choice | description). يرجّع (ردّ_المنصة, already_answered)."""
    method = getattr(client, ("answer_test_" if is_test else "answer_") + kind)
    try:
        return method(question_id, value), False
    except PlatformError as exc:
        if isinstance(exc.payload, dict) and exc.payload.get("already_answered"):
            return {"result": {"correct": True}}, True
        raise


def _learn(ctx, question, answer):
    if ctx.learned is not None and ctx.learned.add(question, answer):
        ctx.learned_new += 1


def _eliminate_choices(client, is_test, question_id, question_text, options, tried_id, response, ctx):
    """المنصة قالت غلط ومكشفتش الصح: نجرّب باقي الخيارات واحد واحد (الأطول الأول) لحد ما نلاقي الصح ونحفظه."""
    tried = {tried_id}
    known_wrong = ctx.learned.wrong_indices(question_text, [t for _i, t in options]) if ctx.learned is not None else set()
    order = sorted(
        (i for i, (oid, _t) in enumerate(options) if oid not in tried),
        key=lambda i: (i in known_wrong, -len(options[i][1]), i),
    )
    for i in order:
        option_id, option_text = options[i]
        try:
            response, already = _submit(client, is_test, "choice", question_id, option_id)
        except PlatformError:
            break                      # المنصة مش بتقبل إعادة المحاولة: نكتفي بالغلط المتسجّل
        ctx.retried += 1
        if already:
            break
        outcome = _result_of(response)
        if isinstance(outcome, dict) and outcome.get("correct") is True:
            _learn(ctx, question_text, option_text)   # لقينا الصح بالاستبعاد
            break
        if isinstance(outcome, dict) and outcome.get("correct") is False and ctx.learned is not None:
            ctx.learned.add_wrong(question_text, option_text)
        else:
            break                      # المنصة ما حكمتش: ما نكمّلش تجريب عشوائي
    return response


def _answer_review_question(client, is_test, item, title, entries, answers, choices, ctx):
    """يجاوب سؤال واحد. يرجّع ردّ المنصة، أو None لو السؤال كان متجاوَب قبل كده."""
    question = item["question"]
    question_id = question.get("id")
    question_text = _clean(question.get("content"))
    if question_id is None or not question_text:
        raise UnknownStructure(f"Review question is missing id/content in {title}.")
    question_type = question.get("type")

    if question_type == "choice":
        raw_choices = item.get("choices")
        if not isinstance(raw_choices, list):
            raise UnknownStructure(f"Choice list missing in {title}.")
        options = [
            (choice.get("id"), _clean(choice.get("content")))
            for choice in raw_choices
            if isinstance(choice, dict) and choice.get("id") is not None
        ]
        if len(options) < 2:
            raise UnknownStructure(f"Choice list incomplete in {title}.")
        selected = choices.get(str(question_id))
        if selected is None:
            # نخمّن دايمًا — المنصة بتكشف الصح في النتيجة وإحنا بنعيد بالإجابة الصحيحة.
            index, source = _choose_option(
                question_text, [text for _id, text in options], entries, ctx,
                allow_guess=True,
            )
            if source == "guess":
                ctx.guessed += 1
            selected = options[index][0]
        response, already = _submit(client, is_test, "choice", question_id, selected)
        if already:
            return None
        outcome = _result_of(response)
        if isinstance(outcome, dict) and outcome.get("correct") is False:
            texts = dict(options)
            if ctx.learned is not None and selected in texts:
                ctx.learned.add_wrong(question_text, texts[selected])   # نتعلّم من الغلط حتى لو الصح مش متكشف
            correct_id = _correct_choice_id(outcome)
            if correct_id is not None:
                ctx.retried += 1
                correct_text = next((text for _id, text in options if _id == correct_id), "")
                _learn(ctx, question_text, correct_text)
                response, _ = _submit(client, is_test, "choice", question_id, correct_id)
            else:
                response = _eliminate_choices(
                    client, is_test, question_id, question_text, options, selected, response, ctx
                )
        return response

    if question_type == "description":
        answer = answers.get(str(question_id))
        if not answer and ctx.learned is not None:
            answer = ctx.learned.get(question_text)
        if not answer:
            try:
                answer = _answer_code(question_text, entries, getattr(client, "lang", "python"))
            except UnmatchedAnswer:
                # الكود المبدئي من المنصة (لو موجود) أو محاولة بسيطة: المنصة بتكشف الإجابة الصحيحة
                # وإحنا بنتعلمها ونعيد بيها — مفيش وقوف عند سؤال مش في البنك.
                descs = item.get("descriptions") or []
                fallback = ""
                if descs and isinstance(descs[0], dict):
                    fallback = str(descs[0].get("code") or "").strip()
                answer = fallback or _placeholder_code(getattr(client, "lang", "python"))
                ctx.guessed += 1
        response, already = _submit(client, is_test, "description", question_id, answer)
        if already:
            return None
        outcome = _result_of(response)
        if isinstance(outcome, dict) and outcome.get("correct") is False:
            model_answer = str(outcome.get("model_answer") or "").strip()
            if model_answer:
                ctx.retried += 1
                _learn(ctx, question_text, model_answer)
                response, _ = _submit(client, is_test, "description", question_id, model_answer)
        return response

    raise UnknownStructure(f"Unsupported review question type: {question_type!r}.")


def _persist_corrections(ctx, questions, answers, choices):
    """الإجابات الصحيحة اللي المنصة كشفتها في نتيجة المراجعة بتتحفظ في الكاش الدايم (للحسابات الجاية)."""
    if ctx is None or ctx.learned is None:
        return
    for item in questions:
        if not isinstance(item, dict) or not isinstance(item.get("question"), dict):
            continue
        question = item["question"]
        qid, text = str(question.get("id")), _clean(question.get("content"))
        if not text:
            continue
        if qid in answers:
            _learn(ctx, text, answers[qid])
        elif qid in choices:
            for choice in item.get("choices") or []:
                if isinstance(choice, dict) and choice.get("id") == choices[qid]:
                    _learn(ctx, text, _clean(choice.get("content")))
                    break


def _resume_index(questions, state, title):
    """من أي سؤال نكمّل (بعد آخر سؤال اتجاوب في الجلسة المحفوظة)."""
    last_answered = (state or {}).get("last_answered_question_id")
    if last_answered is None:
        return 0
    for index, item in enumerate(questions):
        question = item.get("question") if isinstance(item, dict) else None
        if isinstance(question, dict) and str(question.get("id")) == str(last_answered):
            return index + 1
    raise UnknownStructure(f"Saved review position is not in the question list for {title}.")


def _solve_review(client, chapter, entries, hook, cancel, ctx=None):
    ctx = ctx or _Ctx()
    chapter_id = chapter["id"]
    title = chapter.get("name") or f"chapter-{chapter_id}"
    is_test = chapter.get("test_type") == "test"

    def fetch_result():
        return client.test_result(chapter_id) if is_test else client.review_result(chapter_id)

    _say(hook, "quiz_solving", title)
    questions = client.test_questions(chapter_id)
    if not isinstance(questions, list) or not questions:
        raise UnknownStructure(f"Review questions missing for {title}.")
    total = len(questions)
    result = None
    correction_answers = {}
    correction_choices = {}

    for attempt in range(2):
        _check(cancel)
        state = client.test_state(chapter_id)
        if (state or {}).get("visible_result"):
            result = fetch_result()
            if result is not None:
                _harvest_corrections(result, correction_answers, correction_choices)
                _persist_corrections(ctx, questions, correction_answers, correction_choices)
                if int(result.get("correct_count") or 0) >= total:
                    _say(hook, "quiz_done", title)
                    return result

        if not (state or {}).get("work_in_process"):
            client.start_test(chapter_id)
            state = client.test_state(chapter_id)

        start_index = _resume_index(questions, state, title)
        for position, item in enumerate(questions[start_index:], start_index + 1):
            _check(cancel)
            if not isinstance(item, dict) or not isinstance(item.get("question"), dict):
                raise UnknownStructure(f"Unexpected review-question shape for {title}.")
            response = _answer_review_question(
                client, is_test, item, title, entries,
                correction_answers, correction_choices, ctx,
            )
            if response is not None:
                outcome = _result_of(response)
                if isinstance(outcome, dict) and outcome.get("correct") is False:
                    _say(hook, "log", f"⚠️ إجابة غير صحيحة في {title}؛ أعيد المحاولة.")
            _say(hook, "quiz_progress", f"{position}|{total}")
            if response is not None:
                _say(hook, "q_ok", "")
            _sleep(cancel, ANSWER_DELAY)

        result = fetch_result()
        if result is not None:
            _harvest_corrections(result, correction_answers, correction_choices)
            _persist_corrections(ctx, questions, correction_answers, correction_choices)
            if int(result.get("correct_count") or 0) >= total:
                _say(hook, "quiz_done", title)
                return result
        if attempt == 0:
            _say(hook, "log", f"🔁 إعادة الاختبار بالإجابات المصححة: {title}")
            client.start_test(chapter_id)

    if result is not None:
        raise UnknownStructure(
            f"Qureo review remains incomplete: {title} "
            f"(correct={result.get('correct_count')}/{total})."
        )
    raise UnknownStructure(f"Qureo did not confirm review completion for {title}.")


PERFECT_PASS = os.getenv("QUREO_PERFECT_PASS", "1").strip().lower() not in ("0", "false", "no", "off")


def _perfect_pass(client, chapters, entries, hook, cancel, ctx, stats=None, report=None, best=None):
    """بعد الحل (أو لحساب مكتمل قبل كده): يتأكد إن كل مراجعة/اختبار 100% (correct_count = عدد الأسئلة).
    أي واحد ناقص بيتعاد بالإجابات المصححة. مبيوقفش الحل لو إعادة فصل فشلت.
    بيرجّع (اللي اتصلّح, اللي فضل ناقص).
    report (اختياري): dict بيتملّى {chapter_id: {correct, total, before, state}} لكل مراجعة/اختبار اتقرأ،
    وstate = perfect (كان كامل) / fixed (اتعاد لـ 100%) / not_perfect (فضل ناقص).
    best (اختياري): {chapter_id: عنصر تقدم المنصة}. لو فيه best_correct_count بنقارن بيه (أحسن نتيجة، وهي اللي
    بتحدد الألماس) بدل نتيجة آخر محاولة؛ ولو مش موجود بنرجع لنتيجة آخر محاولة زي الأول."""
    fixed, remaining = [], []
    for chapter in chapters:
        _check(cancel)
        if chapter.get("test_type") not in ("review", "test") or chapter.get("id") is None:
            continue
        chapter_id = chapter["id"]
        title = str(chapter.get("name") or f"chapter-{chapter_id}")
        is_test = chapter.get("test_type") == "test"
        try:
            result = client.test_result(chapter_id) if is_test else client.review_result(chapter_id)
            if not isinstance(result, dict):
                continue                                  # ما اتحاولش أصلًا: مش شغل المرحلة دي
            info = (best or {}).get(str(chapter_id)) or {}
            declared = chapter.get("question_count")
            if isinstance(declared, int) and declared > 0:
                total = declared
            else:
                questions = client.test_questions(chapter_id)
                total = len(questions) if isinstance(questions, list) else 0
            best_count = info.get("best_correct_count")
            before = int(best_count) if isinstance(best_count, int) else int(result.get("correct_count") or 0)
            if report is not None:
                report[chapter_id] = {"correct": before, "total": total, "before": before,
                                      "state": "not_perfect", "diamond": info.get("diamond")}
            if total and before >= total:
                if report is not None:
                    report[chapter_id]["state"] = "perfect"
                continue                                  # 100% بالفعل
            _say(hook, "log", f"🎯 {title}: النتيجة مش كاملة ({before}/{total})، بعيدها لـ 100%")
            review = _solve_review(client, chapter, entries, hook, cancel, ctx)
            fixed.append(title)
            if report is not None:
                after = int((review or {}).get("correct_count") or total)
                report[chapter_id].update(correct=max(before, after), state="fixed")
                gained = (review or {}).get("diamond")
                if isinstance(gained, (int, float)) and not isinstance(gained, bool):
                    report[chapter_id]["diamond"] = gained
            if stats is not None and isinstance(review, dict):
                stats["questions"] += len(review.get("choice_results") or []) + len(review.get("description_results") or [])
                stats["activities"] += 1
        except (Stopped, LoginFailed):
            raise
        except (PlatformError, UnknownStructure, UnmatchedAnswer) as exc:
            _log.warning("perfect pass: %s still not perfect (%s)", title, type(exc).__name__)
            remaining.append(title)
    return fixed, remaining


def _progress_map(client, section_id):
    """{chapter_id: عنصر التقدم (best_correct_count, diamond, completed_at)} من المنصة، أو None لو القراءة فشلت."""
    try:
        progress = client.section_progress(section_id)
    except (PlatformError, UnknownStructure):
        return None
    if not isinstance(progress, list):
        return None
    return {
        str(item.get("chapter_id")): item
        for item in progress
        if isinstance(item, dict) and item.get("chapter_id") is not None
    }


def _course_diamond(client):
    """إجمالي الألماس في الكورس (رقم) أو None لو المنصة/العميل ما رجّعوش رقم. مابيوقفش الحل أبدًا."""
    getter = getattr(client, "course_diamond", None)
    if getter is None:
        return None
    try:
        value = getter()
    except (PlatformError, UnknownStructure):
        return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def run_job(
    username: str,
    password: str,
    entries,
    hook: Optional[Callable] = None,
    cancel: Optional[threading.Event] = None,
    headless: bool = True,
    report_consent: bool = False,
    course_id: Optional[str] = None,
    course: Optional[str] = None,
) -> dict:
    if course is None:   # البوت بيبعت course_id بس: نستنتج الدورة منه
        course = "javascript" if course_id and re.search(r"java_?script|(^|_)js(_|$)", course_id, re.I) else "python"
    js_token = _JS_MODE.set(course == "javascript")
    started_at = time.monotonic()
    if course == "python" and course_id in (None, COURSES["python"]["id"]):
        client = QureoClient(cancel)
    else:
        client = QureoClient(cancel, course, course_id)
    label = getattr(client, "course_label", COURSES[course]["label"])
    ctx = _Ctx(_safe_learned(js=(course == "javascript")))
    questions_done = 0
    activities_done = 0
    last_title = ""
    chapter_rows = []          # تقرير كل فصل (الاسم، دروس اتحلّت، مكتمل قبل كده، النتيجة) للرسالة النهائية
    result = {"status": "error", "error": "unknown"}
    try:
        _say(hook, "phase", "بيسجّل الدخول إلى Qureo")
        student = client.login(username, password)
        _say(hook, "login_ok", str(student.get("name") or ""))

        course = client.course()
        basic_section = course.get("basic_section") or {}
        section_id = basic_section.get("id")
        if section_id is None:
            raise UnknownStructure(f"Qureo {label} course has no basic section.")
        section = client.section(section_id)
        chapters = section.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            raise UnknownStructure(f"Qureo {label} course has no chapters.")
        chapters = sorted(
            (chapter for chapter in chapters if isinstance(chapter, dict)),
            key=lambda chapter: int(chapter.get("seq") or 0),
        )
        diamond_before = _course_diamond(client)
        progress = client.section_progress(section_id)
        progress_by_id = {
            str(item.get("chapter_id")): item
            for item in progress
            if isinstance(item, dict) and item.get("chapter_id") is not None
        }
        completed = sum(
            bool(progress_by_id.get(str(chapter.get("id")), {}).get("completed_at"))
            for chapter in chapters
        )
        initial_completed = completed
        _say(hook, "plan_todo", f"{label} Qureo|{completed}|{len(chapters)}")
        _say(hook, "plan_progress", f"{completed}|{len(chapters)}")

        for chapter in chapters:
            _check(cancel)
            chapter_id = chapter.get("id")
            if chapter_id is None:
                raise UnknownStructure("Qureo chapter is missing an id.")
            chapter_state = progress_by_id.get(str(chapter_id)) or {}
            last_title = str(chapter.get("name") or f"chapter-{chapter_id}")
            if chapter.get("test_type") not in (None, "review", "test"):
                raise UnknownStructure(
                    f"Qureo activity type is not documented for «{last_title}»."
                )
            row = {"id": chapter_id, "name": last_title, "lectures": 0, "was_done": False,
                   "type": chapter.get("test_type"), "correct": None, "total": None, "state": "unknown"}
            chapter_rows.append(row)
            if chapter_state.get("completed_at"):
                row["was_done"] = True
                continue

            try:
                access = client.chapter_access(chapter_id)
            except PlatformError as exc:
                if exc.status == 403 and isinstance(exc.payload, dict):
                    if exc.payload.get("not_fulfill_preconditions"):
                        raise UnknownStructure(
                            f"الفصل «{last_title}» ما زال مقفولًا حسب شروط المنصة."
                        ) from None
                    if exc.payload.get("not_contracted"):
                        raise UnknownStructure(
                            f"الفصل «{last_title}» غير متعاقد عليه."
                        ) from None
                raise
            # Also check flags in a 200 response
            if isinstance(access, dict):
                if access.get("not_fulfill_preconditions"):
                    raise UnknownStructure(
                        f"الفصل «{last_title}» ما زال مقفولًا حسب شروط المنصة."
                    )
                if access.get("not_contracted"):
                    raise UnknownStructure(
                        f"الفصل «{last_title}» غير متعاقد عليه."
                    )

            chapter_data, lecture_states = _load_chapter(client, chapter_id, cancel)
            lectures = chapter_data.get("lectures")
            if not isinstance(lectures, list):
                raise UnknownStructure(f"Lecture list missing for {last_title}.")
            completed_lectures = {
                str(item.get("lecture_id"))
                for item in lecture_states
                if isinstance(item, dict) and item.get("completed_at")
            }
            ordered = sorted(
                (item for item in lectures if isinstance(item, dict)),
                key=lambda item: int(item.get("seq") or 0),
            )
            todo = []
            for lecture in ordered:
                if lecture.get("id") is None:
                    raise UnknownStructure(f"Lecture id missing for {last_title}.")
                if str(lecture["id"]) not in completed_lectures:
                    todo.append(lecture)
            prefetched = _prefetch_lectures(client, [item["id"] for item in todo], cancel)
            for lecture in todo:
                lecture_id = lecture["id"]
                lecture_title = str(lecture.get("title") or last_title)
                _say(hook, "open", lecture_title)
                _solve_lecture(
                    client, lecture_id, lecture_title, entries, hook, cancel, ctx,
                    prefetched.get(lecture_id), confirm=False,
                )
                activities_done += 1
                _say(hook, "done", lecture_title)
            if todo:
                _confirm_lectures(client, chapter_id, [item["id"] for item in todo], last_title)
            row["lectures"] = len(todo)

            _say(hook, "open", last_title)
            review = _solve_review(client, chapter, entries, hook, cancel, ctx)
            questions_done += len(review.get("choice_results") or []) + len(
                review.get("description_results") or []
            )
            activities_done += 1
            _say(hook, "done", last_title)
            completed += 1
            _say(hook, "plan_progress", f"{completed}|{len(chapters)}")
            _say(hook, "chapter_done", f"{last_title}|{completed}|{len(chapters)}")

        fixed, not_perfect = [], []
        scores = {}
        if PERFECT_PASS:
            extra = {"questions": 0, "activities": 0}
            best_now = _progress_map(client, section_id) or progress_by_id   # أحسن نتيجة لكل فصل دلوقتي
            fixed, not_perfect = _perfect_pass(
                client, chapters, entries, hook, cancel, ctx, extra, report=scores, best=best_now,
            )
            questions_done += extra["questions"]
            activities_done += extra["activities"]
        final_map = _progress_map(client, section_id)
        diamond_after = _course_diamond(client)
        for row in chapter_rows:
            score = scores.get(row["id"])
            if score:
                row.update(correct=score["correct"], total=score["total"],
                           before=score["before"], state=score["state"], diamond=score.get("diamond"))
            item = (final_map or {}).get(str(row["id"]))
            if item is not None and isinstance(item.get("diamond"), (int, float)):
                row["diamond"] = item["diamond"]          # الألماس الفعلي بعد كل حاجة (أحدث من قراءة النتيجة)
            row["closed"] = None if final_map is None else bool(item and item.get("completed_at"))
        result = {
            "status": "finished",
            "last": last_title,
            "questions": questions_done,
            "activities": activities_done,
            "precompleted": bool(initial_completed >= len(chapters) and not fixed),
            "perfect_fixed": fixed,
            "not_perfect": not_perfect,
            "chapters": chapter_rows,
            "diamond_before": diamond_before,
            "diamond_after": diamond_after,
        }
    except Stopped:
        result = {"status": "stopped", "last": last_title}
    except LoginFailed:
        result = {"status": "login_failed", "last": last_title}
    except UnmatchedAnswer as exc:
        result = {
            "status": "tests_pending",
            "reason": "unmatched",
            "question": (exc.question or "")[:150],
            "test": last_title,
            "last": last_title,
        }
    except (PlatformError, UnknownStructure) as exc:
        result = {"status": "stuck", "error": str(exc)[:300], "last": last_title}
    except Exception as exc:  # noqa: BLE001
        _log.exception("Unexpected error in Qureo run_job")
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc} @ {_where(exc)}"[:300], "last": last_title}
    finally:
        timings = client.timing_report()
        requests_made = client.request_count()
        client.close()
        _JS_MODE.reset(js_token)

    result.setdefault("last", last_title)
    result.setdefault("questions", questions_done)
    result.setdefault("activities", activities_done)
    result["elapsed"] = round(time.monotonic() - started_at, 1)
    result["requests"] = requests_made
    result["timings"] = timings
    result["guessed"] = ctx.guessed
    result["retried"] = ctx.retried
    result["learned"] = ctx.learned_new
    return result
