/*
 capture_gems.js — أداة التقاط الجواهر (مراقبة فقط، مابتغيّرش أي طلب ولا بتجاوب بدالك).

 الاستخدام:
   1) افتح منصة Qureo في المتصفح (كروم/إيدج على الكمبيوتر) وسجّل دخول بحسابك.
   2) اضغط F12 ← تبويب Console ← الصق الملف كله واضغط Enter
      (لو كروم قال "allow pasting" اكتب allow pasting واضغط Enter وبعدين الصق تاني).
   3) هتظهر شريط صغير تحت على اليمين فيه زرّين. حلّ الاختبار عادي لحد ما تسلّم آخر اختبار
      وتظهر الجواهر.
   4) أول ما الجواهر تظهر قدامك اضغط «📍 ظهرت الجواهر»، وبعدها «💾 حفظ».
   5) هينزل ملف qureo_gems_capture_*.json ← ابعته لي (أو شغّل عليه:  python analyze_gems.py الملف).

 ملاحظات:
   • لازم تلصقه قبل ما تسلّم الاختبار الأخير (ويفضل قبل ما تفتح الفصل أصلًا). ماتعملش Refresh بعد اللصق:
     الـRefresh بيمسح الأداة وتلصقها تاني.
   • لو المنصة بتفتح الاختبار في iframe/تبويب تاني: اللصق بيتم في نفس الصفحة اللي بتحل فيها
     (في Console اختار من القايمة اللي فوق "top" الـframe بتاع المنصة لو في أكتر من واحد).
   • الباسورد والتوكن والكوكيز والإيميل والتليفون مابيتسجّلوش أصلًا (الهيدرز ما بتتقراش، والحقول الشخصية بتتحجب).
*/
(() => {
  if (window.__gems) { console.log('[gems] شغّالة بالفعل. __gems.save() لحفظ الملف'); return; }

  const MAX_ENTRIES = 800;
  const SECRET_KEY = /pass|token|secret|authorization|cookie|jwt|csrf|session|verifier|email|e_mail|phone|mobile|avatar|photo|picture|birth|address|username|user_name|first_?name|last_?name|full_?name/i;
  const PERSONAL_SIBLING = /^(email|phone|mobile|username|user_name)$/i;
  const GEM_KEY = /gem|diamond|jewel|crystal|reward|coin|\bxp\b|^xp|point|star|badge|trophy|achiev|currency|balance|wallet|bonus|mastery|prize|loot|جوهر|جواهر|ماس|نقاط|نجوم|مكاف/i;
  const GEM_TEXT = /جواهر|جوهر|ماس|💎|💠|\bgems?\b|diamonds?|crystals?|reward|مكافأ|مكافا|تهانينا|مبروك|congrat/i;
  const SKIP_URL = /\.(js|mjs|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|map|mp4|webm)(\?|$)|google-analytics|googletagmanager|sentry|hotjar|clarity\.ms|facebook|doubleclick/i;

  const t0 = Date.now();
  const log = { started_at: new Date().toISOString(), page: location.origin + location.pathname, entries: [], dom: [], marks: [], routes: [], storage_keys: {} };
  let seq = 0;
  const now = () => Date.now() - t0;

  // ───── تقليل الحجم + حجب البيانات الشخصية ─────
  function shrink(v, o, depth) {
    o = o || {}; depth = depth || 0;
    const maxArr = o.maxArr || 3, maxStr = o.maxStr || 80, maxDepth = o.maxDepth || 8;
    if (v === null || typeof v === 'number' || typeof v === 'boolean') return v;
    if (typeof v === 'string') return v.length > maxStr ? v.slice(0, maxStr) + '…' : v;
    if (depth > maxDepth) return '…';
    if (Array.isArray(v)) {
      const head = v.slice(0, maxArr).map(x => shrink(x, o, depth + 1));
      if (v.length > maxArr) head.push('…(+' + (v.length - maxArr) + ' عنصر)');
      return head;
    }
    if (typeof v === 'object') {
      const keys = Object.keys(v);
      const personalObj = keys.some(k => PERSONAL_SIBLING.test(k));
      const out = {};
      for (const k of keys) {
        out[k] = (SECRET_KEY.test(k) || (personalObj && /^name$/i.test(k))) ? '<محجوب>' : shrink(v[k], o, depth + 1);
      }
      return out;
    }
    return String(v).slice(0, maxStr);
  }

  // كل المفاتيح/القيم اللي شكلها جواهر/مكافأة (بنحتفظ بالمسار والقيمة)
  function findGemFields(v, path, out, depth) {
    path = path || ''; out = out || []; depth = depth || 0;
    if (out.length > 60 || depth > 10) return out;
    if (v && typeof v === 'object') {
      const arr = Array.isArray(v);
      (arr ? v.slice(0, 5) : Object.keys(v)).forEach((item, idx) => {
        const k = arr ? idx : item;
        const child = arr ? item : v[item];
        const p = path + (arr ? '[]' : (path ? '.' : '') + k);
        if (!arr && SECRET_KEY.test(String(k))) return;
        if (!arr && GEM_KEY.test(String(k))) out.push({ path: p, value: shrink(child, { maxArr: 10, maxStr: 200, maxDepth: 4 }) });
        else if (typeof child === 'string' && GEM_TEXT.test(child)) out.push({ path: p, value: child.slice(0, 200) });
        findGemFields(child, p, out, depth + 1);
      });
    }
    return out;
  }

  function cleanUrl(u) {
    try {
      const x = new URL(u, location.href);
      const q = [];
      x.searchParams.forEach((val, key) => q.push(key + '=' + (SECRET_KEY.test(key) ? '<محجوب>' : val.slice(0, 40))));
      return x.origin + x.pathname + (q.length ? '?' + q.join('&') : '');
    } catch (e) { return String(u).slice(0, 200); }
  }

  function parseJson(text) { try { return JSON.parse(text); } catch (e) { return undefined; } }

  function record(kind, method, url, status, reqBody, resText, ms) {
    if (SKIP_URL.test(url) || log.entries.length >= MAX_ENTRIES) return;
    const entry = { seq: ++seq, t: now(), kind, method: (method || 'GET').toUpperCase(), url: cleanUrl(url), status, ms };
    if (typeof reqBody === 'string' && reqBody) {
      const j = parseJson(reqBody);
      entry.request = j !== undefined ? shrink(j, { maxStr: 120 }) : '(نص ' + reqBody.length + ' حرف)';
    }
    if (typeof resText === 'string' && resText) {
      const j = parseJson(resText);
      if (j !== undefined) {
        const gems = findGemFields(j);
        entry.response = shrink(j, gems.length ? { maxArr: 10, maxStr: 200 } : { maxArr: 3, maxStr: 80 });
        if (gems.length) entry.gem_fields = gems;
      } else {
        entry.response_text = resText.slice(0, 200);
      }
    }
    log.entries.push(entry);
    if (entry.gem_fields) console.log('%c[gems] 💎 حقول جواهر محتملة في ' + entry.method + ' ' + entry.url, 'color:#d4a017', entry.gem_fields);
  }

  // ───── fetch ─────
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || String(input);
      const method = (init && init.method) || (input && input.method) || 'GET';
      const body = init && typeof init.body === 'string' ? init.body : '';
      const start = Date.now();
      const p = origFetch.apply(this, arguments);
      p.then(res => {
        try { res.clone().text().then(txt => record('fetch', method, url, res.status, body, txt, Date.now() - start)).catch(() => {}); } catch (e) {}
      }).catch(() => {});
      return p;
    };
  }

  // ───── XMLHttpRequest ─────
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__g = { method, url, start: 0 }; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) {
    const g = this.__g; if (g) g.start = Date.now();
    this.addEventListener('loadend', () => {
      if (!g) return;
      let txt = '';
      try { txt = (this.responseType === '' || this.responseType === 'text') ? this.responseText : (this.responseType === 'json' ? JSON.stringify(this.response) : ''); } catch (e) {}
      record('xhr', g.method, g.url, this.status, typeof body === 'string' ? body : '', txt, Date.now() - g.start);
    });
    return XS.apply(this, arguments);
  };

  // ───── WebSocket (لو المنصة بتبعت الجواهر لايف) ─────
  if (window.WebSocket) {
    const OW = window.WebSocket;
    const W = function (url, protocols) {
      const ws = protocols !== undefined ? new OW(url, protocols) : new OW(url);
      ws.addEventListener('message', ev => { if (typeof ev.data === 'string') record('ws', 'WS', url, 0, '', ev.data, 0); });
      return ws;
    };
    W.prototype = OW.prototype; ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(k => (W[k] = OW[k]));
    window.WebSocket = W;
  }

  // ───── تغيّر الصفحة (route) ─────
  function route(why) { log.routes.push({ t: now(), why, path: location.pathname + location.hash }); }
  ['pushState', 'replaceState'].forEach(fn => {
    const o = history[fn];
    history[fn] = function () { const r = o.apply(this, arguments); route(fn); return r; };
  });
  window.addEventListener('popstate', () => route('popstate'));
  window.addEventListener('hashchange', () => route('hashchange'));
  route('start');

  // ───── اللي ظاهر على الشاشة (جواهر / مبروك / مكافأة) ─────
  const seenText = new Set();
  function snapshotText() { return (document.body && document.body.innerText || '').slice(0, 3000); }
  function scanNode(node, why) {
    if (!node || log.dom.length >= 80) return;
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return;
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300);
    if (!text || !GEM_TEXT.test(text) || seenText.has(text)) return;
    seenText.add(text);
    const hit = { t: now(), why, text, tag: el.tagName, class: String(el.className || '').slice(0, 120), html: el.outerHTML.slice(0, 500), route: location.pathname + location.hash };
    if (log.dom.filter(d => d.page_text).length < 6) hit.page_text = snapshotText();
    log.dom.push(hit);
    console.log('%c[gems] 💎 ظهر على الشاشة: ' + text.slice(0, 100), 'color:#d4a017');
  }
  const mo = new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === 'characterData') scanNode(m.target, 'text');
      m.addedNodes && m.addedNodes.forEach(n => scanNode(n, 'added'));
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  scanNode(document.body, 'initial');

  // أسماء مفاتيح التخزين فقط (القيم ممكن تحتوي توكن فمابنقراهاش)
  try { log.storage_keys = { local: Object.keys(localStorage), session: Object.keys(sessionStorage) }; } catch (e) {}

  // ───── الحفظ ─────
  function save() {
    try { log.storage_keys = { local: Object.keys(localStorage), session: Object.keys(sessionStorage) }; } catch (e) {}
    log.saved_at = new Date().toISOString(); log.duration_ms = now();
    const blob = new Blob([JSON.stringify(log, null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'qureo_gems_capture_' + new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19) + '.json';
    document.body.appendChild(a); a.click(); a.remove();
    console.log('[gems] اتحفظ:', a.download, '— طلبات:', log.entries.length, '| ظهور على الشاشة:', log.dom.length, '| علامات:', log.marks.length);
  }
  function mark(note) {
    log.marks.push({ t: now(), note: note || 'ظهرت الجواهر', route: location.pathname + location.hash, page_text: snapshotText(), last_seq: seq });
    console.log('[gems] 📍 علّمت اللحظة دي (آخر طلب رقم ' + seq + ')');
  }

  // ───── شريط الأزرار ─────
  const bar = document.createElement('div');
  bar.setAttribute('style', 'position:fixed;bottom:12px;right:12px;z-index:2147483647;display:flex;gap:6px;font:13px system-ui;direction:rtl');
  const mk = (label, fn, bg) => {
    const b = document.createElement('button');
    b.textContent = label; b.onclick = fn;
    b.setAttribute('style', 'padding:8px 12px;border:0;border-radius:8px;color:#fff;cursor:pointer;background:' + bg);
    return b;
  };
  bar.appendChild(mk('📍 ظهرت الجواهر', () => mark(), '#b8860b'));
  bar.appendChild(mk('💾 حفظ', save, '#2e7d32'));
  document.body.appendChild(bar);

  window.__gems = { save, mark, log, stop() { window.fetch = origFetch; XMLHttpRequest.prototype.open = XO; XMLHttpRequest.prototype.send = XS; mo.disconnect(); bar.remove(); delete window.__gems; } };
  console.log('%c[gems] شغّالة ✅ — حلّ الاختبار عادي، وأول ما الجواهر تظهر اضغط «📍 ظهرت الجواهر» ثم «💾 حفظ».', 'color:#2e7d32;font-weight:bold');
})();
