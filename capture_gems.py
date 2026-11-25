"""
capture_gems.py — أداة واحدة لالتقاط الجواهر من منصة Qureo.

التشغيل:
    python capture_gems.py
فتح متصفح: سجّل دخول بحسابك وحلّ المراجعة/الاختبار عادي لحد ما الجواهر تظهر.
بعدين اضغط Ctrl+C في الطرفية (أو اقفل المتصفح) ← بيتكتب ملف  qureo_gems_<الوقت>.json  وبيتطبع ملخص.

مراقبة فقط: مابتجاوبش ولا بتعدّل أي طلب. الباسورد والتوكن والإيميل والتليفون مابيتسجّلوش
(الهيدرز والكوكيز مابتتقراش أصلًا، والحقول الشخصية بتتحجب).

أول مرة بس:
    pip install playwright
    (لو عندك Chrome أو Edge مثبّت مش محتاج تنزّل حاجة؛ وإلا:  python -m playwright install chromium)

خيارات اختيارية:
    --url   رابط البداية (الافتراضي بوابة Qureo)
    --out   اسم الملف الناتج
    --browser  chrome | msedge | chromium   (الافتراضي: يجرّب بالترتيب ده)
    --profile  فولدر يحفظ تسجيل دخولك عشان ماتسجّلش كل مرة (الافتراضي .gems_profile — ماتبعتوش لحد)
"""
import argparse
import json
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import analyze_gems as ag

START_URL = "https://me-portal.qureo.education"
MAX_ENTRIES = 3000
MAX_BODY_CHARS = 2_000_000

SKIP_URL = re.compile(
    r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|map|mp4|webm)(\?|$)|"
    r"google-analytics|googletagmanager|sentry|hotjar|clarity\.ms|facebook|doubleclick", re.I)
PERSONAL_SIBLING = re.compile(r"^(email|phone|mobile|username|user_name)$", re.I)
GEM_TEXT = re.compile(r"جواهر|جوهر|ماس|💎|💠|\bgems?\b|diamonds?|crystals?|reward|مكافأ|مكافا|تهانينا|مبروك|congrat", re.I)

# بيتحقن في كل صفحة/iframe: بيراقب الشاشة ويبعت للبايثون أي نص جواهر/مكافأة
DOM_JS = r"""
(() => {
  if (window.__gemsInjected) return; window.__gemsInjected = true;
  const GEM = /جواهر|جوهر|ماس|💎|💠|\bgems?\b|diamonds?|crystals?|reward|مكافأ|مكافا|تهانينا|مبروك|congrat/i;
  const seen = new Set();
  function scan(node, why) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    if (!el) return;
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300);
    if (!text || !GEM.test(text) || seen.has(text)) return;
    seen.add(text);
    let page_text = ''; try { page_text = (document.body.innerText || '').slice(0, 3000); } catch (e) {}
    try {
      window.__gemsDom({ why, text, tag: el.tagName, cls: String(el.className || '').slice(0, 120),
                         html: el.outerHTML.slice(0, 500), route: location.pathname + location.hash, page_text });
    } catch (e) {}
  }
  new MutationObserver(ms => { for (const m of ms) {
    if (m.type === 'characterData') scan(m.target, 'text');
    m.addedNodes && m.addedNodes.forEach(n => scan(n, 'added'));
  } }).observe(document, { childList: true, subtree: true, characterData: true });
  document.addEventListener('DOMContentLoaded', () => scan(document.body, 'initial'));
})();
"""


def shrink(value, max_arr=3, max_str=80, depth=0):
    """نحتفظ بكل أسماء الحقول ونقصّ القيم الطويلة والقوائم، ونحجب الحقول الشخصية."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str] + "…"
    if depth > 8:
        return "…"
    if isinstance(value, list):
        head = [shrink(v, max_arr, max_str, depth + 1) for v in value[:max_arr]]
        if len(value) > max_arr:
            head.append(f"…(+{len(value) - max_arr} عنصر)")
        return head
    if isinstance(value, dict):
        personal_obj = any(PERSONAL_SIBLING.search(str(k)) for k in value)
        out = {}
        for k, v in value.items():
            hidden = ag.SECRET_KEY.search(str(k)) or (personal_obj and str(k).lower() == "name")
            out[k] = "<محجوب>" if hidden else shrink(v, max_arr, max_str, depth + 1)
        return out
    return str(value)[:max_str]


def clean_url(url):
    """بنشيل قيم الكويري الحساسة ونحتفظ بالباقي."""
    base, _, query = (url or "").partition("?")
    if not query:
        return base
    parts = []
    for item in query.split("&"):
        key, _, val = item.partition("=")
        parts.append(f"{key}={'<محجوب>' if ag.SECRET_KEY.search(key) else val[:40]}")
    return base + "?" + "&".join(parts)


class Recorder:
    """بيجمّع الالتقاط في الذاكرة. مستقل عن المتصفح (عشان يتختبر)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._t0 = clock()
        self.entries, self.dom, self.routes = [], [], []
        self._seq = 0
        self._seen_text = set()

    def now_ms(self):
        return int((self._clock() - self._t0) * 1000)

    def http(self, method, url, status, request_text=None, response_text=None, kind="http", started_ms=None):
        if SKIP_URL.search(url or "") or len(self.entries) >= MAX_ENTRIES:
            return None
        self._seq += 1
        end = self.now_ms()
        entry = {"seq": self._seq, "t": end, "kind": kind, "method": (method or "GET").upper(),
                 "url": clean_url(url), "status": status}
        if started_ms is not None:
            entry["ms"] = max(0, end - started_ms)
        if request_text:
            try:
                entry["request"] = shrink(json.loads(request_text), max_str=120)
            except ValueError:
                entry["request"] = f"(نص {len(request_text)} حرف)"
        if response_text and len(response_text) <= MAX_BODY_CHARS:
            try:
                body = json.loads(response_text)
            except ValueError:
                entry["response_text"] = response_text[:200]
            else:
                gems = ag.gem_fields(body)
                if gems:
                    entry["response"] = shrink(body, max_arr=10, max_str=200)
                    entry["gem_fields"] = [{"path": g["path"], "value": shrink(g["value"], 10, 200)} for g in gems]
                else:
                    entry["response"] = shrink(body)
        self.entries.append(entry)
        return entry

    def screen(self, payload, page_url=""):
        text = (payload.get("text") or "").strip()
        if not text or text in self._seen_text or len(self.dom) >= 80:
            return None
        self._seen_text.add(text)
        hit = {"t": self.now_ms(), "why": payload.get("why"), "text": text, "tag": payload.get("tag"),
               "class": payload.get("cls", ""), "html": payload.get("html", ""),
               "route": payload.get("route") or page_url}
        if sum(1 for d in self.dom if d.get("page_text")) < 6:
            hit["page_text"] = payload.get("page_text", "")
        self.dom.append(hit)
        return hit

    def route(self, url, why="navigated"):
        self.routes.append({"t": self.now_ms(), "why": why, "path": url})

    def result(self):
        cap = {"started_at": datetime.now().isoformat(timespec="seconds"), "duration_ms": self.now_ms(),
               "entries": self.entries, "dom": self.dom, "marks": [], "routes": self.routes}
        cap["analysis"] = ag.analyze(cap)
        return cap


def attach(context, rec):
    """يربط الـRecorder بأحداث Playwright."""
    def on_response(response):
        try:
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            try:
                text = response.text()
            except Exception:  # noqa: BLE001  (redirect / body مش متاح)
                text = ""
            rec.http(request.method, response.url, response.status, request.post_data, text)
        except Exception as exc:  # noqa: BLE001
            print(f"[تحذير] مقدرتش أسجّل رد: {type(exc).__name__}", file=sys.stderr)

    def on_page(page):
        def on_ws(ws):
            ws.on("framereceived", lambda payload: rec.http(
                "WS", ws.url, 0, None, payload if isinstance(payload, str) else "", kind="ws"))
        page.on("websocket", on_ws)
        page.on("framenavigated", lambda frame: rec.route(frame.url) if frame == page.main_frame else None)

    context.on("response", on_response)
    context.on("page", on_page)
    context.expose_binding("__gemsDom", lambda source, payload: rec.screen(
        payload, getattr(source.get("page"), "url", "")))
    context.add_init_script(DOM_JS)
    for page in context.pages:
        on_page(page)


def launch(playwright, profile, browser):
    order = [browser] if browser else ["chrome", "msedge", "chromium"]
    errors = []
    for name in order:
        try:
            if name == "chromium":
                return playwright.chromium.launch_persistent_context(profile, headless=False, no_viewport=True)
            return playwright.chromium.launch_persistent_context(profile, channel=name, headless=False, no_viewport=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {str(exc).splitlines()[0][:100]}")
    raise RuntimeError("مقدرتش أفتح متصفح.\n  " + "\n  ".join(errors) +
                       "\nثبّت Chrome أو Edge، أو شغّل:  python -m playwright install chromium")


def save(rec, out_path):
    cap = rec.result()
    Path(out_path).write_text(json.dumps(cap, ensure_ascii=False, indent=1), encoding="utf-8")
    ag.show(cap["analysis"], cap)
    print(f"\n✅ اتكتب الملف: {out_path}  ({len(cap['entries'])} طلب، {len(cap['dom'])} ظهور على الشاشة)")
    print("ابعته لي.")
    return cap


def main(argv=None):
    parser = argparse.ArgumentParser(description="التقاط الجواهر من Qureo")
    parser.add_argument("--url", default=START_URL)
    parser.add_argument("--out", default=None)
    parser.add_argument("--browser", choices=["chrome", "msedge", "chromium"], default=None)
    parser.add_argument("--profile", default=str(Path(__file__).resolve().parent / ".gems_profile"))
    args = parser.parse_args(argv)
    out_path = args.out or f"qureo_gems_{datetime.now():%Y%m%d_%H%M%S}.json"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ناقص Playwright. نزّله بـ:  pip install playwright")
        return 1

    stop = {"flag": False}

    def on_sigint(_sig, _frame):
        if stop["flag"]:                       # Ctrl+C تاني: اخرج فورًا
            raise SystemExit(130)
        stop["flag"] = True
        print("\n⏹️ بوقّف وأكتب الملف…")

    signal.signal(signal.SIGINT, on_sigint)
    rec = Recorder()
    started = False
    try:
        with sync_playwright() as playwright:
            context = launch(playwright, args.profile, args.browser)
            started = True
            attach(context, rec)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(args.url)
            except Exception as exc:  # noqa: BLE001
                print(f"[تحذير] فتح الرابط: {type(exc).__name__}")
            print("✅ شغّال. سجّل دخول وحلّ عادي. لما تخلّص اضغط Ctrl+C هنا (أو اقفل المتصفح).")
            while not stop["flag"]:
                pages = context.pages
                if not pages:
                    break                      # المتصفح اتقفل
                try:
                    pages[-1].wait_for_timeout(300)   # بيسمح لأحداث الشبكة تتعالج
                except Exception:  # noqa: BLE001
                    time.sleep(0.3)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    except RuntimeError as exc:
        print(exc)
        return 1
    finally:
        if started:
            save(rec, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
