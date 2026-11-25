"""
fake_platform.py — منصة وهمية واقعية للاختبار (httpx.MockTransport)، بتتصرف حسب SPRIX_API_Documentation.md:
  - دخول بكوكي، وجلسة ممكن تنتهي (401) في نص الشغل
  - فتح تسلسلي صارم: نشاط واحد مفتوح بعد اللي قبله
  - درس: إجابة غلط ما بتتسجّلش في cleared_block_ids، و correct_option_id = null (زي العينة الموثّقة)،
    و /complete بيرفض لو فيه أسئلة ما اتجاوبتش صح
  - اختبار: start → PUT answer → submit، النجاح عند 80%، والتصحيح بيكشف الخيار الصحيح لكل سؤال
  - أعطال: 503 متقطعة، حظر Cloudflare، تأخر ظهور الفتح في المنهج
"""
import copy
import json
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "blocks70.json").read_text(encoding="utf-8"))
FIXTURE = FIXTURE.get("data", FIXTURE)


def _json(data, status=200):
    return httpx.Response(status, json={"success": True, "data": data})


def _err(status, message):
    return httpx.Response(status, json={"success": False, "error": {"message": message}})


class FakePlatform:
    def __init__(self, bank_truth, *, wrong_in_bank=(), reveal_correct=False, expire_after=None,
                 flaky_every=None, lag_unlock=False, cloudflare=False, items=None, pass_score=80,
                 lock_after_wrong=False):
        """
        bank_truth : callable(question_text, option_texts) -> index الصحيح حسب البنك (من resolve_answer)
        wrong_in_bank : نصوص أسئلة البنك فيها غلط (الصحيح الحقيقي في المنصة = خيار تاني)
        """
        self.bank_truth = bank_truth
        self.wrong_in_bank = set(wrong_in_bank)
        self.reveal_correct = reveal_correct
        self.expire_after = expire_after
        self.flaky_every = flaky_every
        self.lag_unlock = lag_unlock
        self.cloudflare = cloudflare
        self.pass_score = pass_score
        self.lock_after_wrong = lock_after_wrong

        self.token = None
        self.logins = 0
        self.auth_requests = 0
        self.request_count = 0
        self.log = []                        # (method, path)
        self._lag_hits = 0

        # ── المحتوى ──
        self.chapters = {}                   # chapter_id -> blocks
        self.questions = {}                  # question_id -> {text, options[(id,text)], correct_id}
        self.cleared = {}                    # chapter_id -> set(block ids)
        self.wrong_blocks = {}               # chapter_id -> set(block ids) أجاب غلط (للقفل)
        self.tests = {}                      # test_id -> {"attempts": [...], "qids": [...]}
        self.items = items or [("chapter", 70), ("review", 5), ("chapter", 71), ("test", 9)]
        self.completed = {}
        self._build_content()

    # ── بناء المحتوى من الدرس الحقيقي ──
    def _truth_index(self, text, option_texts):
        idx = self.bank_truth(text, option_texts)
        if text in self.wrong_in_bank:
            idx = (idx + 1) % len(option_texts)
        return idx

    def _register_question(self, qid, question, options):
        texts = [re.sub(r"<[^>]+>", " ", o["text"]) for o in options]
        import html as h
        texts = [" ".join(h.unescape(t).split()) for t in texts]
        qtext = " ".join(h.unescape(re.sub(r"<[^>]+>", " ", question["question_text"])).split())
        idx = self._truth_index(qtext, texts)
        self.questions[qid] = {"text": qtext, "options": options, "correct_id": options[idx]["id"], "raw": question}

    def _clone_chapter(self, chapter_id, offset):
        data = copy.deepcopy(FIXTURE)
        blocks = []
        for b in data["blocks"]:
            b["id"] += offset
            b["chapter_id"] = chapter_id
            if b.get("type") == "question":
                b["question_id"] += offset
                b["question"]["id"] += offset
                for o in b["options"]:
                    o["id"] += offset
                    o["question_id"] += offset
                self._register_question(b["question_id"], b["question"], b["options"])
            blocks.append(b)
        self.chapters[chapter_id] = blocks
        self.cleared[chapter_id] = set()
        self.wrong_blocks[chapter_id] = set()

    def _build_content(self):
        first_chapter = True
        offset = 0
        for kind, iid in self.items:
            if kind == "chapter":
                self._clone_chapter(iid, offset)
                offset += 100000
            elif kind == "test":
                qids = []
                # 10 سؤال: الأسئلة الحقيقية الخمسة مرتين بـ ids مختلفة
                src = [b for b in FIXTURE["blocks"] if b.get("type") == "question"]
                for rep in range(2):
                    for b in src:
                        b = copy.deepcopy(b)
                        shift = offset + rep * 1000
                        qid = b["question_id"] + shift
                        for o in b["options"]:
                            o["id"] += shift
                            o["question_id"] = qid
                        b["question"]["id"] = qid
                        self._register_question(qid, b["question"], b["options"])
                        qids.append(qid)
                self.tests[iid] = {"attempts": [], "qids": qids}
                offset += 100000
            self.completed[(kind, iid)] = False

    # ── حالة المنهج ──
    def _unlocked(self, position):
        return all(self.completed[(k, i)] for k, i in self.items[:position])

    def _mark_completed(self, key):
        if not self.completed[key]:
            self.completed[key] = True
            if self.lag_unlock:
                self._lag_hits = 1

    def _curriculum(self):
        lagging = self._lag_hits > 0
        if lagging:
            self._lag_hits -= 1
        items = []
        for pos, (kind, iid) in enumerate(self.items):
            unlocked = self._unlocked(pos) and not (lagging and not self.completed[(kind, iid)])
            items.append({"id": iid, "unit_id": 1, "section_id": 1, "item_order": pos + 1, "item_type": kind,
                          "title": f"{kind}-{iid}", "is_hidden": False,
                          "is_completed": self.completed[(kind, iid)],
                          "is_unlocked": unlocked})
        done = sum(1 for v in self.completed.values() if v)
        return [{"id": 1, "unit_order": 1, "title": "الوحدة", "is_hidden": False, "is_accessible": True,
                 "total_items": len(items), "completed_items": done,
                 "sections": [{"id": 1, "unit_id": 1, "name": "s", "sort_order": 1, "items": copy.deepcopy(items)}],
                 "items": copy.deepcopy(items)}]

    # ── نقطة الدخول للـ transport ──
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.request_count += 1
        if self.cloudflare:
            return httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>",
                                  headers={"content-type": "text/html", "server": "cloudflare", "cf-ray": "abc"})
        if not path.startswith("/api/v1"):
            return httpx.Response(200, text="<html>home</html>", headers={"content-type": "text/html"})
        path = path[len("/api/v1"):]
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}

        if path == "/auth/login" and method == "POST":
            if body.get("password") == "bad":
                return _err(401, "invalid credentials")
            self.logins += 1
            self.token = f"T{self.logins}"
            self.auth_requests = 0
            r = _json({"name": "طالب", "id": 1})
            r.headers["set-cookie"] = f"learning_token={self.token}; Path=/; HttpOnly"
            return r

        # باقي الطلبات تحتاج جلسة سارية
        if f"learning_token={self.token}" not in request.headers.get("cookie", "") or self.token is None:
            return _err(401, "unauthorized")
        self.auth_requests += 1
        if self.expire_after and self.auth_requests > self.expire_after:
            self.token = None
            return _err(401, "token expired")
        if self.flaky_every and self.request_count % self.flaky_every == 0:
            return httpx.Response(503, json={"success": False, "error": {"message": "unavailable"}})
        self.log.append((method, path))

        m = re.fullmatch(r"/subjects/(\d+)/curriculum", path)
        if m and method == "GET":
            return _json(self._curriculum())
        if re.fullmatch(r"/subjects/\d+/achievements/unlock", path):
            return _json({})

        m = re.fullmatch(r"/chapters/(\d+)/blocks", path)
        if m and method == "GET":
            cid = int(m.group(1))
            return _json({"chapter": {"id": cid, "is_completed": self.completed.get(("chapter", cid), False),
                                      "cleared_block_ids": sorted(self.cleared[cid])},
                          "blocks": copy.deepcopy(self.chapters[cid])})

        m = re.fullmatch(r"/chapters/(\d+)/answer", path)
        if m and method == "POST":
            cid = int(m.group(1))
            block = next((b for b in self.chapters[cid] if b["id"] == body.get("block_id")), None)
            if block is None:
                return _err(404, "block not found")
            if self.lock_after_wrong and block["id"] in self.wrong_blocks[cid]:
                return _err(409, "already answered")
            q = self.questions[block["question_id"]]
            ok = body.get("selected_option_id") == q["correct_id"]
            if ok:
                self.cleared[cid].add(block["id"])
            else:
                self.wrong_blocks[cid].add(block["id"])
            return _json({"is_correct": ok, "explanation": None,
                          "correct_option_id": q["correct_id"] if (self.reveal_correct and not ok) else None,
                          "correct_text_answer": None})

        m = re.fullmatch(r"/chapters/(\d+)/complete", path)
        if m and method == "POST":
            cid = int(m.group(1))
            needed = {b["id"] for b in self.chapters[cid] if b.get("type") == "question"}
            ok = needed <= self.cleared[cid]
            if ok:
                self._mark_completed(("chapter", cid))
            return _json({"completed": ok})

        m = re.fullmatch(r"/reviews/(\d+)/items", path)
        if m and method == "GET":
            return _json({"review": {"id": int(m.group(1))}, "items": []})
        m = re.fullmatch(r"/reviews/(\d+)/complete", path)
        if m and method == "POST":
            self._mark_completed(("review", int(m.group(1))))
            return _json({"completed": True})

        m = re.fullmatch(r"/tests/(\d+)/start", path)
        if m and method == "POST":
            tid = int(m.group(1))
            t = self.tests[tid]
            attempt = {"n": len(t["attempts"]) + 1, "answers": {}, "tq": {}}
            t["attempts"].append(attempt)
            qids = t["qids"]
            responses, test_questions, questions, options = [], [], {}, {}
            for i, qid in enumerate(qids):
                tq = 1000 * attempt["n"] + i
                attempt["tq"][tq] = qid
                responses.append({"id": tq + 7, "test_question_id": tq, "status": "unanswered", "selected_option_id": None})
                test_questions.append({"id": tq, "test_id": tid, "question_id": qid, "sort_order": i + 1, "weight": 1})
                q = self.questions[qid]
                questions[str(qid)] = {"id": qid, "type": "mcq", "question_text": q["raw"]["question_text"]}
                options[str(qid)] = [{k: v for k, v in o.items()} for o in q["options"]]
            return _json({"response": {"id": 5000 + attempt["n"], "attempt_number": attempt["n"], "status": "in_progress"},
                          "question_responses": responses, "questions": questions, "options": options,
                          "test_questions": test_questions})

        m = re.fullmatch(r"/tests/(\d+)/questions/(\d+)/answer", path)
        if m and method == "PUT":
            tid, tq = int(m.group(1)), int(m.group(2))
            attempt = self.tests[tid]["attempts"][-1]
            if tq not in attempt["tq"]:
                return _err(404, "unknown question")
            attempt["answers"][tq] = body.get("selected_option_id")
            return _json({"saved": True})

        m = re.fullmatch(r"/tests/(\d+)/submit", path)
        if m and method == "POST":
            tid = int(m.group(1))
            attempt = self.tests[tid]["attempts"][-1]
            qrs, opts, questions, correct = [], {}, {}, 0
            for tq, qid in attempt["tq"].items():
                q = self.questions[qid]
                ok = attempt["answers"].get(tq) == q["correct_id"]
                correct += ok
                qrs.append({"test_question_id": tq, "is_correct": ok, "selected_option_id": attempt["answers"].get(tq)})
                opts[str(qid)] = [dict(o, **({"is_correct": True} if o["id"] == q["correct_id"] else {})) for o in q["options"]]
                questions[str(qid)] = {"id": qid, "question_text": q["raw"]["question_text"], "explanation": "<ul><li>…</li></ul>"}
            score = round(correct * 100 / len(attempt["tq"]))
            passed = score >= self.pass_score
            if passed:
                self._mark_completed(("test", tid))
            return _json({"response": {"id": 5000 + attempt["n"], "passed": passed, "total_score": score,
                                       "correct_count": 0, "total_count": 0},
                          "passing_score": self.pass_score, "question_responses": qrs,
                          "questions": questions, "options": opts})

        return _err(404, f"no route {method} {path}")
