"""
analyze_gems.py — بيحلّل ملف التقاط الجواهر ويقولك الجواهر جاية من أنهي طلب.

الاستخدام:
    python analyze_gems.py qureo_gems_capture_XXXX.json
    python analyze_gems.py الملف.har          ← بديل: Network ← Save all as HAR (بيتحجب منه الهيدرز والكوكيز)

بيطبع:
  1) اللحظة اللي الجواهر ظهرت فيها (من العلامة اللي ضغطتها أو من الشاشة).
  2) الطلبات اللي حصلت قبلها مباشرة (آخر إجابة/تسليم + رد المنصة).
  3) الحقول اللي اسمها شبه جواهر/مكافأة ومكانها.
  4) أي رقم اتغيّر بين ردّين لنفس الـ endpoint (غالبًا هو عدّاد الجواهر).
ويكتب gems_findings.json جنب الملف (تقدر تبعته لي).
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SECRET_KEY = re.compile(
    r"pass|token|secret|authorization|cookie|jwt|csrf|session|verifier|email|mail|phone|mobile|"
    r"avatar|photo|picture|birth|address|username|user_name|first_?name|last_?name|full_?name", re.I)
GEM_KEY = re.compile(
    r"gem|diamond|jewel|crystal|reward|coin|(^|\.)xp|point|star|badge|trophy|achiev|currency|"
    r"balance|wallet|bonus|mastery|prize|loot|جوهر|جواهر|ماس|نقاط|نجوم|مكاف", re.I)
STRICT_GEM_TEXT = re.compile(r"جواهر|جوهر|💎|\bgems?\b|diamonds?", re.I)
WINDOW_MS = 20000      # الطلبات اللي قبل لحظة ظهور الجواهر بحد أقصى كده
ID_PART = re.compile(r"/\d+(?=/|$)")


def endpoint_key(method, url):
    """نفس الـ endpoint بغض النظر عن رقم الفصل/السؤال."""
    path = re.sub(r"\?.*$", "", re.sub(r"^https?://[^/]+", "", url or ""))
    return f"{(method or 'GET').upper()} {ID_PART.sub('/{id}', path)}"


def flatten(obj, prefix=""):
    """كل الأوراق الرقمية: {مسار: قيمة}. القوائم بالمسار [] بالترتيب."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if SECRET_KEY.search(str(k)):
                continue
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:10]):
            out.update(flatten(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix] = obj
    return out


def gem_fields(obj, prefix="", out=None, depth=0):
    out = [] if out is None else out
    if depth > 10 or len(out) > 80:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if SECRET_KEY.search(str(k)):
                continue
            path = f"{prefix}.{k}" if prefix else str(k)
            if GEM_KEY.search(str(k)):
                out.append({"path": path, "value": v})
            gem_fields(v, path, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:5]:
            gem_fields(v, prefix + "[]", out, depth + 1)
    return out


def load_har(data):
    """HAR → نفس شكل ملف الالتقاط. الهيدرز والكوكيز مابنقراهمش."""
    entries = []
    base = None
    for i, e in enumerate(data.get("log", {}).get("entries", []), 1):
        req, res = e.get("request", {}), e.get("response", {})
        url = req.get("url", "")
        if re.search(r"\.(js|css|png|jpe?g|gif|svg|webp|ico|woff2?|map)(\?|$)", url):
            continue
        text = (res.get("content") or {}).get("text") or ""
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        post = (req.get("postData") or {}).get("text") or ""
        try:
            sent = json.loads(post) if post else None
        except ValueError:
            sent = None
        t = e.get("startedDateTime", "")
        entries.append({"seq": i, "t": t, "method": req.get("method"), "url": url.split("?")[0],
                        "status": res.get("status"), "request": sent, "response": body})
    # الزمن النسبي بالمللي ثانية
    try:
        from datetime import datetime
        stamps = [datetime.fromisoformat(x["t"].replace("Z", "+00:00")) for x in entries]
        base = min(stamps)
        for x, s in zip(entries, stamps):
            x["t"] = int((s - base).total_seconds() * 1000)
    except (ValueError, TypeError):
        for x in entries:
            x["t"] = x["seq"] * 1000
    return {"entries": entries, "dom": [], "marks": [], "routes": []}


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "log" in data and "entries" in data["log"]:
        return load_har(data)
    return data


def analyze(cap):
    entries = cap.get("entries") or []
    findings = {"anchor": None, "before_anchor": [], "gem_fields": {}, "changed_numbers": []}

    # 1) لحظة ظهور الجواهر: أول علامة، وإلا أول ظهور على الشاشة، وإلا أول طلب فيه حقل جواهر
    anchor_t, anchor_label = None, None
    if cap.get("marks"):
        m = cap["marks"][0]
        anchor_t, anchor_label = m["t"], "علامة «ظهرت الجواهر»"
    elif cap.get("dom"):
        # الجواهر بتظهر في آخر الفصل: نفضّل آخر نص فيه كلمة جواهر صريحة، وإلا آخر نص (مبروك/مكافأة…)
        strict = [d for d in cap["dom"] if STRICT_GEM_TEXT.search(d.get("text", ""))]
        d = (strict or cap["dom"])[-1]
        anchor_t, anchor_label = d["t"], f"ظهر على الشاشة: {d['text'][:60]}"
    else:
        for e in entries:
            if e.get("gem_fields") or gem_fields(e.get("response")):
                anchor_t, anchor_label = e["t"], "أول طلب فيه حقل شبه جواهر"
                break
    findings["anchor"] = {"t": anchor_t, "label": anchor_label}

    # 2) الطلبات قبلها مباشرة
    if anchor_t is not None:
        for e in entries:
            if anchor_t - WINDOW_MS <= e.get("t", 0) <= anchor_t + 2000:
                findings["before_anchor"].append({
                    "seq": e.get("seq"), "t": e.get("t"), "call": endpoint_key(e.get("method"), e.get("url")),
                    "status": e.get("status"), "request": e.get("request"), "response": e.get("response"),
                })

    # 3) حقول الجواهر المحتملة لكل endpoint
    by_ep = defaultdict(list)
    for e in entries:
        fields = e.get("gem_fields") or gem_fields(e.get("response"))
        if fields:
            ep = endpoint_key(e.get("method"), e.get("url"))
            for f in fields:
                findings["gem_fields"].setdefault(ep, {}).setdefault(f["path"], []).append(f["value"])
        by_ep[endpoint_key(e.get("method"), e.get("url"))].append(e)

    # 4) رقم اتغيّر بين ردّين لنفس الـ endpoint
    for ep, items in by_ep.items():
        if len(items) < 2:
            continue
        flats = [(e, flatten(e.get("response"))) for e in items if isinstance(e.get("response"), (dict, list))]
        paths = set().union(*[f.keys() for _, f in flats]) if flats else set()
        for p in sorted(paths):
            vals = [(e.get("seq"), f[p]) for e, f in flats if p in f]
            if len({v for _, v in vals}) > 1:
                findings["changed_numbers"].append({"endpoint": ep, "path": p, "values_by_seq": vals[:12]})
    return findings


def show(findings, cap):
    a = findings["anchor"]
    print("═" * 60)
    print(f"طلبات: {len(cap.get('entries') or [])} | ظهور على الشاشة: {len(cap.get('dom') or [])} | علامات: {len(cap.get('marks') or [])}")
    if a["t"] is None:
        print("⚠️ ما لقيتش لحظة ظهور للجواهر (ولا علامة ولا نص على الشاشة ولا حقل). يمكن اتلصقت الأداة متأخر.")
    else:
        print(f"📍 لحظة الجواهر: {a['label']} (عند {a['t'] / 1000:.1f}ث من بداية الالتقاط)")
    for d in (cap.get("dom") or [])[:5]:
        print(f"   🖥️ على الشاشة: «{d['text'][:80]}»  [{d.get('tag') or ''}.{(d.get('class') or '')[:40]}]")
    print("\n── الطلبات قبل اللحظة دي مباشرة ──")
    for e in findings["before_anchor"][-8:]:
        print(f"  #{e['seq']:>3} {e['call']} → {e['status']}")
    print("\n── حقول شبه جواهر/مكافأة ──")
    if not findings["gem_fields"]:
        print("  (مفيش أي رد فيه حقل بالأسماء دي: يعني الجواهر ممكن تتحسب في الفرونت أو في طلب ماتلقطش)")
    for ep, fields in findings["gem_fields"].items():
        print(f"  {ep}")
        for path, values in fields.items():
            print(f"     • {path} = {json.dumps(values[-1], ensure_ascii=False)[:90]}  (x{len(values)})")
    print("\n── أرقام اتغيّرت بين ردّين لنفس الطلب (مرشّحة تكون عدّاد الجواهر) ──")
    if not findings["changed_numbers"]:
        print("  (مفيش)")
    for c in findings["changed_numbers"][:25]:
        print(f"  {c['endpoint']}  {c['path']}: {c['values_by_seq']}")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cap = load(argv[1])
    findings = analyze(cap)
    show(findings, cap)
    out = Path(argv[1]).with_name("gems_findings.json")
    out.write_text(json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ اتكتب: {out}  (ابعته لي)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
