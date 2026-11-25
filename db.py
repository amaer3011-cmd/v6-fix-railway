"""قاعدة بيانات SQLite: الأكواد + عدّاد الصلاة + سجل المهام + إحصائيات المستخدم."""
import sqlite3
import threading
import time

_lock = threading.Lock()
_conn = None


def backup_file(src_path, dest_dir, keep=7):
    """نسخة احتياطية متسقة من قاعدة البيانات (SQLite backup API: آمنة مع WAL والبوت شغال).
    بتحتفظ بآخر `keep` نسخ. بترجّع مسار النسخة أو None لو الملف مش موجود."""
    import shutil
    from pathlib import Path
    src = Path(src_path)
    if not src.exists():
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = dest / f"bot-{stamp}.db"
    source = sqlite3.connect(str(src))
    try:
        out = sqlite3.connect(str(target))
        try:
            source.backup(out)
        finally:
            out.close()
    finally:
        source.close()
    for name in ("learned_answers.json", "learned_js_answers.json"):   # بيانات التعلّم جنب الداتابيز
        side = src.parent / name
        if side.exists():
            shutil.copy2(side, dest / f"{side.stem}-{stamp}.json")
    for pattern in ("bot-*.db", "learned_answers-*.json", "learned_js_answers-*.json"):
        for old in sorted(dest.glob(pattern))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return target


def init(path="bot.db"):
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    with _lock:
        _conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS users (
                tg_id      INTEGER PRIMARY KEY,
                tg_name    TEXT,
                username   TEXT,
                first_seen INTEGER,
                last_seen  INTEGER
            );
            CREATE TABLE IF NOT EXISTS platform_accounts (
                tg_id      INTEGER NOT NULL,
                platform   TEXT NOT NULL,
                username   TEXT NOT NULL,
                PRIMARY KEY (tg_id, platform)
            );
            CREATE TABLE IF NOT EXISTS counters (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id      INTEGER,
                started    INTEGER,
                finished   INTEGER,
                status     TEXT,
                questions  INTEGER DEFAULT 0,
                activities INTEGER DEFAULT 0,
                duration   INTEGER DEFAULT 0,
                note       TEXT
            );
            CREATE TABLE IF NOT EXISTS blocked_users (
                tg_id      INTEGER PRIMARY KEY,
                blocked_at INTEGER NOT NULL,
                reason     TEXT
            );
            CREATE TABLE IF NOT EXISTS entitlements (
                tg_id              INTEGER PRIMARY KEY,
                attempts_remaining INTEGER NOT NULL DEFAULT 1,
                vip_attempts_remaining INTEGER NOT NULL DEFAULT 0,
                unlimited_until    INTEGER NOT NULL DEFAULT 0,
                paid_attempts      INTEGER NOT NULL DEFAULT 0,
                ref_attempts       INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS free_trial_accounts (
                platform   TEXT NOT NULL,
                username   TEXT NOT NULL,
                tg_id      INTEGER NOT NULL,
                used_at    INTEGER NOT NULL,
                PRIMARY KEY (platform, username)
            );
            CREATE TABLE IF NOT EXISTS free_trial_claims (
                tg_id      INTEGER PRIMARY KEY,
                platform   TEXT NOT NULL,
                username   TEXT NOT NULL,
                claimed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activation_codes (
                code       TEXT PRIMARY KEY,
                code_type  TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'unused',
                created_at INTEGER NOT NULL,
                used_at    INTEGER,
                used_by    INTEGER,
                used_name  TEXT
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,
                inviter_id  INTEGER NOT NULL,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invite_rewards (
                tg_id INTEGER PRIMARY KEY,
                rewards_granted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS completed_accounts (
                username   TEXT NOT NULL,
                platform   TEXT NOT NULL,
                tg_id      INTEGER NOT NULL,
                completed_at INTEGER NOT NULL,
                PRIMARY KEY (platform, username)
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                chat_id   TEXT PRIMARY KEY,
                title     TEXT NOT NULL,
                link      TEXT NOT NULL,
                added_at  INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO counters(name, value) VALUES ('salawat', 0);
            INSERT OR IGNORE INTO counters(name, value) VALUES ('total_users', 0);
            INSERT OR IGNORE INTO counters(name, value) VALUES ('total_jobs', 0);
            INSERT OR IGNORE INTO counters(name, value) VALUES ('total_questions', 0);
            """
        )
        # Migration: أضف عمود duration لو مش موجود (ترقية من v5)
        try:
            _conn.execute("ALTER TABLE jobs ADD COLUMN duration INTEGER DEFAULT 0")
            _conn.commit()
        except Exception:
            pass
        try:   # ترقية: عدّاد المحاولات المشتراة بكود (جزء من attempts_remaining)
            _conn.execute("ALTER TABLE entitlements ADD COLUMN paid_attempts INTEGER NOT NULL DEFAULT 0")
            _conn.commit()
        except Exception:
            pass
        try:   # ترقية: محاولات مكافأة الدعوات (جزء من attempts_remaining، تشتغل على أي حساب بس مش أولوية)
            _conn.execute("ALTER TABLE entitlements ADD COLUMN ref_attempts INTEGER NOT NULL DEFAULT 0")
            _conn.commit()
        except Exception:
            pass
        # الحسابات المنجزة قبل كده بتتحسب إنها استهلكت محاولتها المجانية (نفس السلوك القديم)
        _conn.execute(
            "INSERT OR IGNORE INTO free_trial_accounts(platform, username, tg_id, used_at) "
            "SELECT CASE WHEN platform='javascript' THEN 'qureo' ELSE platform END, "
            "lower(username), tg_id, completed_at FROM completed_accounts "
            "ORDER BY completed_at ASC"
        )
        _conn.commit()
        _conn.execute(
            "INSERT OR IGNORE INTO platform_accounts(tg_id, platform, username) "
            "SELECT tg_id, 'financial', username FROM users "
            "WHERE username IS NOT NULL AND username != ''"
        )
        _conn.commit()


# ───────────────────────── Users ─────────────────────────

def touch_user(tg_id, tg_name="", username=""):
    now = int(time.time())
    with _lock:
        existing = _conn.execute(
            "SELECT tg_id FROM users WHERE tg_id=?", (tg_id,)
        ).fetchone()
        _conn.execute(
            "INSERT INTO users(tg_id, tg_name, username, first_seen, last_seen) VALUES (?,?,?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET tg_name=excluded.tg_name, "
            "username=excluded.username, last_seen=excluded.last_seen",
            (tg_id, tg_name, username or "", now, now),
        )
        if not existing:
            _conn.execute(
                "UPDATE counters SET value=value+1 WHERE name='total_users'"
            )
        _conn.execute(
            "INSERT OR IGNORE INTO entitlements(tg_id) VALUES (?)",
            (tg_id,),
        )
        _conn.commit()
        return not existing


def save_username(tg_id, username, platform="financial"):
    with _lock:
        _conn.execute(
            "INSERT INTO platform_accounts(tg_id, platform, username) VALUES (?,?,?) "
            "ON CONFLICT(tg_id, platform) DO UPDATE SET username=excluded.username",
            (tg_id, platform, username),
        )
        if platform == "financial":
            _conn.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
        _conn.commit()


def get_username(tg_id, platform="financial"):
    with _lock:
        row = _conn.execute(
            "SELECT username FROM platform_accounts WHERE tg_id=? AND platform=?",
            (tg_id, platform),
        ).fetchone()
        if not row and platform == "financial":
            row = _conn.execute(
                "SELECT username FROM users WHERE tg_id=?", (tg_id,)
            ).fetchone()
    return row[0] if row and row[0] else None


def get_completed_account_owner(username):
    """يرجع Telegram ID الذي أكمل هذا الكود على المنصة المنجزة."""
    username = (username or "").strip()
    if not username:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT tg_id, platform, completed_at "
            "FROM completed_accounts "
            "WHERE lower(username)=lower(?) "
            "ORDER BY completed_at DESC LIMIT 1",
            (username,),
        ).fetchone()
    if not row:
        return None
    return {"tg_id": row[0], "platform": row[1], "completed_at": row[2]}


def mark_account_completed(tg_id, username, platform):
    username = (username or "").strip()
    if not username:
        return
    now = int(time.time())
    with _lock:
        _conn.execute(
            "INSERT INTO completed_accounts(username, platform, tg_id, completed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(platform, username) DO UPDATE SET "
            "tg_id=excluded.tg_id, completed_at=excluded.completed_at",
            (username, platform, tg_id, now),
        )
        _conn.commit()


# ───────────────────────── Salawat ─────────────────────────

def add_salawat():
    with _lock:
        _conn.execute("UPDATE counters SET value=value+1 WHERE name='salawat'")
        value = _conn.execute(
            "SELECT value FROM counters WHERE name='salawat'"
        ).fetchone()[0]
        _conn.commit()
    return value


def get_salawat():
    with _lock:
        return _conn.execute(
            "SELECT value FROM counters WHERE name='salawat'"
        ).fetchone()[0]


def get_setting(name, default=""):
    with _lock:
        row = _conn.execute(
            "SELECT value FROM bot_settings WHERE key=?",
            (name,),
        ).fetchone()
    return row[0] if row else default


def set_setting(name, value):
    with _lock:
        _conn.execute(
            "INSERT INTO bot_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (name, str(value)),
        )
        _conn.commit()


def add_required_channel(chat_id, title, link):
    chat_id = str(chat_id).strip()
    title = str(title).strip()[:100]
    link = str(link).strip()[:500]
    if not chat_id:
        raise ValueError("chat_id مطلوب")
    with _lock:
        _conn.execute(
            "INSERT INTO required_channels(chat_id, title, link, added_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, link=excluded.link",
            (chat_id, title or chat_id, link, int(time.time())),
        )
        _conn.commit()


def remove_required_channel(chat_id):
    with _lock:
        _conn.execute("DELETE FROM required_channels WHERE chat_id=?", (str(chat_id),))
        _conn.commit()


def get_required_channels():
    with _lock:
        rows = _conn.execute(
            "SELECT chat_id, title, link, added_at "
            "FROM required_channels ORDER BY added_at DESC"
        ).fetchall()
    return [
        {"chat_id": r[0], "title": r[1], "link": r[2], "added_at": r[3]}
        for r in rows
    ]


# ───────────────────────── Jobs ─────────────────────────

def log_job(tg_id, started, status, questions, activities, note="", duration=0):
    with _lock:
        _conn.execute(
            "INSERT INTO jobs(tg_id, started, finished, status, questions, activities, duration, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                tg_id, int(started), int(time.time()),
                status, questions, activities or 0,
                int(duration), note[:300],
            ),
        )
        # حدّث العدادات الكلية
        _conn.execute(
            "UPDATE counters SET value=value+1 WHERE name='total_jobs'"
        )
        if questions:
            _conn.execute(
                "UPDATE counters SET value=value+? WHERE name='total_questions'",
                (questions,),
            )
        _conn.commit()


# ───────────────────────── إحصائيات الفرد ─────────────────────────

def get_user_stats(tg_id):
    """إحصائيات مستخدم واحد: عدد الجلسات، الأسئلة، الأنشطة، آخر جلسة، أطول جلسة."""
    with _lock:
        row = _conn.execute(
            """
            SELECT
                COUNT(*)                                    AS sessions,
                COALESCE(SUM(questions), 0)                 AS total_q,
                COALESCE(SUM(CASE WHEN activities IS NOT NULL
                              THEN activities ELSE 0 END), 0) AS total_act,
                MAX(finished)                               AS last_run,
                MAX(duration)                               AS longest_sec,
                COALESCE(SUM(duration), 0)                  AS total_sec,
                SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END) AS finished_count
            FROM jobs
            WHERE tg_id=?
            """,
            (tg_id,),
        ).fetchone()
        user_row = _conn.execute(
            "SELECT tg_name, first_seen FROM users WHERE tg_id=?", (tg_id,)
        ).fetchone()
    return {
        "sessions":      row[0] or 0,
        "total_q":       row[1] or 0,
        "total_act":     row[2] or 0,
        "last_run":      row[3],
        "longest_sec":   row[4] or 0,
        "total_sec":     row[5] or 0,
        "finished":      row[6] or 0,
        "name":          user_row[0] if user_row else "",
        "first_seen":    user_row[1] if user_row else None,
    }


# ───────────────────────── إحصائيات الكل (أدمن) ─────────────────────────

# حالات مش بتتحسب في نسبة النجاح: غلطة المستخدم (دخول خاطئ) أو إيقاف يدوي أو حساب منجز قبل كده
_RATE_EXCLUDED = ("login_failed", "stopped", "already_completed")


def _success_rate(conn, since=0):
    """نسبة نجاح البوت = المكتملة ÷ (العمليات اللي البوت نفّذها فعلًا). None لو مفيش عمليات."""
    marks = ",".join("?" * len(_RATE_EXCLUDED))
    row = conn.execute(
        f"SELECT SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END), COUNT(*) "
        f"FROM jobs WHERE started >= ? AND status NOT IN ({marks})",
        (since, *_RATE_EXCLUDED),
    ).fetchone()
    ok, total = (row[0] or 0), (row[1] or 0)
    return round(ok * 100 / total) if total else None


def get_global_stats():
    """إحصائيات عامة للبوت كله."""
    with _lock:
        totals = {
            r[0]: r[1]
            for r in _conn.execute("SELECT name, value FROM counters").fetchall()
        }
        local = time.localtime()
        day_start = int(time.mktime(time.struct_time((
            local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0,
            local.tm_wday, local.tm_yday, local.tm_isdst,
        ))))
        active_today = _conn.execute(
            "SELECT COUNT(DISTINCT tg_id) FROM jobs WHERE started >= ?",
            (day_start,),
        ).fetchone()[0]
        joined_today = _conn.execute(
            "SELECT COUNT(*) FROM users WHERE first_seen >= ?",
            (day_start,),
        ).fetchone()[0]
        today = _conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='login_failed' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status IN ('stuck','error','timeout','tests_pending') THEN 1 ELSE 0 END)
            FROM jobs WHERE started >= ?
            """,
            (day_start,),
        ).fetchone()
        recent = _conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(questions), 0),
                   SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status NOT IN ('finished', 'stopped') THEN 1 ELSE 0 END)
            FROM jobs WHERE started >= ?
            """,
            (int(time.time()) - 7 * 86400,),
        ).fetchone()
        rate_today = _success_rate(_conn, day_start)
        rate_week = _success_rate(_conn, int(time.time()) - 7 * 86400)
        rate_total = _success_rate(_conn, 0)
        top_row = _conn.execute(
            """
            SELECT tg_id, tg_name, SUM(questions) AS q
            FROM jobs JOIN users USING(tg_id)
            GROUP BY tg_id ORDER BY q DESC LIMIT 1
            """
        ).fetchone()
    return {
        "total_users":     totals.get("total_users", 0),
        "total_jobs":      totals.get("total_jobs", 0),
        "total_questions": totals.get("total_questions", 0),
        "salawat":         totals.get("salawat", 0),
        "active_today":    active_today,
        "joined_today":    joined_today,
        "today_jobs":      today[0] or 0,
        "today_success":   today[1] or 0,
        "today_login_failed": today[2] or 0,
        "today_bot_failures": today[3] or 0,
        "rate_today":      rate_today,
        "rate_week":       rate_week,
        "rate_total":      rate_total,
        "weekly_jobs":     recent[0] or 0,
        "weekly_questions": recent[1] or 0,
        "weekly_finished": recent[2] or 0,
        "weekly_issues":   recent[3] or 0,
        "top_user":        (top_row[1] or f"ID:{top_row[0]}", top_row[2]) if top_row else None,
    }


# ───────────────────────── الأدمن والمستخدمين ─────────────────────────

def get_all_user_ids():
    with _lock:
        rows = _conn.execute(
            "SELECT tg_id FROM users ORDER BY last_seen DESC"
        ).fetchall()
    return [row[0] for row in rows]


def get_recent_users(limit=10, offset=0):
    with _lock:
        rows = _conn.execute(
            """
            SELECT u.tg_id, u.tg_name, u.username,
                   u.first_seen, u.last_seen,
                   COUNT(j.id) AS sessions,
                   COALESCE(SUM(j.questions), 0) AS questions,
                   SUM(CASE WHEN j.status='finished' THEN 1 ELSE 0 END) AS finished
            FROM users u
            LEFT JOIN jobs j ON j.tg_id=u.tg_id
            GROUP BY u.tg_id
            ORDER BY u.last_seen DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [
        {
            "user_id": r[0], "name": r[1] or "", "username": r[2] or "",
            "first_seen": r[3], "last_seen": r[4], "sessions": r[5],
            "questions": r[6], "finished": r[7] or 0,
        }
        for r in rows
    ]


def search_users(query, limit=8):
    query = query.strip()
    if not query:
        return []
    with _lock:
        if query.isdecimal():
            rows = _conn.execute(
                """
                SELECT u.tg_id, u.tg_name, u.first_seen, u.last_seen,
                       COUNT(j.id), COALESCE(SUM(j.questions), 0)
                FROM users u LEFT JOIN jobs j ON j.tg_id=u.tg_id
                WHERE CAST(u.tg_id AS TEXT) LIKE ?
                GROUP BY u.tg_id ORDER BY u.last_seen DESC LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
        else:
            rows = _conn.execute(
                """
                SELECT u.tg_id, u.tg_name, u.first_seen, u.last_seen,
                       COUNT(j.id), COALESCE(SUM(j.questions), 0)
                FROM users u LEFT JOIN jobs j ON j.tg_id=u.tg_id
                WHERE u.tg_name LIKE ?
                GROUP BY u.tg_id ORDER BY u.last_seen DESC LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
    return [
        {
            "user_id": r[0], "name": r[1] or "", "first_seen": r[2],
            "last_seen": r[3], "sessions": r[4], "questions": r[5],
        }
        for r in rows
    ]


def get_recent_jobs(limit=10):
    with _lock:
        rows = _conn.execute(
            """
            SELECT j.tg_id, u.tg_name, j.status, j.questions, j.activities,
                   j.duration, j.finished, j.note
            FROM jobs j LEFT JOIN users u ON u.tg_id=j.tg_id
            ORDER BY j.finished DESC, j.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "user_id": r[0], "name": r[1] or "", "status": r[2] or "",
            "questions": r[3] or 0, "activities": r[4] or 0,
            "duration": r[5] or 0, "finished": r[6], "note": r[7] or "",
        }
        for r in rows
    ]


def get_user_count():
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user_detail(tg_id):
    stats = get_user_stats(tg_id)
    with _lock:
        accounts = _conn.execute(
            "SELECT platform, username FROM platform_accounts WHERE tg_id=?",
            (tg_id,),
        ).fetchall()
        blocked = _conn.execute(
            "SELECT blocked_at, reason FROM blocked_users WHERE tg_id=?",
            (tg_id,),
        ).fetchone()
    stats["accounts"] = {platform: username for platform, username in accounts}
    stats["blocked"] = blocked
    return stats


def set_user_blocked(tg_id, blocked, reason=""):
    with _lock:
        if blocked:
            _conn.execute(
                "INSERT INTO blocked_users(tg_id, blocked_at, reason) VALUES (?,?,?) "
                "ON CONFLICT(tg_id) DO UPDATE SET blocked_at=excluded.blocked_at, reason=excluded.reason",
                (tg_id, int(time.time()), reason[:300]),
            )
        else:
            _conn.execute("DELETE FROM blocked_users WHERE tg_id=?", (tg_id,))
        _conn.commit()


def is_user_blocked(tg_id):
    with _lock:
        return _conn.execute(
            "SELECT 1 FROM blocked_users WHERE tg_id=?", (tg_id,)
        ).fetchone() is not None


# ───────────────────────── المحاولات والأكواد والدعوات ─────────────────────────

CODE_TYPES = {
    "single": "محاولة واحدة",
    "vip": "VIP عادي — ١٠ محاولات",
    "vip_unlimited": "VIP مميز — أسبوع غير محدود",
}

VIP_PACK_SIZE = 10        # كود VIP عادي = ١٠ محاولات
UNLIMITED_DAYS = 7        # كود VIP مميز = أسبوع


def _vip_granted(tg_id, remaining):
    """إجمالي محاولات VIP اللي اتمنحت للمستخدم = عدد أكواد VIP المفعّلة × ١٠.
    بيتحسب من جدول الأكواد نفسه، فبيشتغل مع الاشتراكات القديمة من غير تعديل في قاعدة البيانات.
    (لازم يتنادى جوه _lock)."""
    packs = _conn.execute(
        "SELECT COUNT(*) FROM activation_codes "
        "WHERE used_by=? AND code_type='vip' AND status='used'",
        (tg_id,),
    ).fetchone()[0]
    return max(packs * VIP_PACK_SIZE, remaining)


def get_entitlement(tg_id):
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO entitlements(tg_id) VALUES (?)", (tg_id,))
        row = _conn.execute(
            "SELECT attempts_remaining, vip_attempts_remaining, unlimited_until, paid_attempts, ref_attempts "
            "FROM entitlements WHERE tg_id=?",
            (tg_id,),
        ).fetchone()
        _conn.commit()
    return {
        "attempts_remaining": row[0] if row else 1,
        "vip_attempts_remaining": row[1] if row else 0,
        "unlimited_until": row[2] if row else 0,
        "paid_attempts": row[3] if row else 0,
        "ref_attempts": row[4] if row else 0,
    }


def _split_attempts(attempts, paid, ref):
    """يقسّم attempts_remaining إلى (free, paid, ref). paid = مشتراة بكود، ref = مكافأة دعوات."""
    paid = max(0, min(paid, attempts))
    ref = max(0, min(ref, attempts - paid))
    return attempts - paid - ref, paid, ref


def has_priority(tg_id):
    """مين بيعدّي الطابور ويتحل له فورًا: VIP / غير محدود / معاه محاولة مشتراة بكود."""
    now = int(time.time())
    ent = get_entitlement(tg_id)
    if ent["unlimited_until"] > now or ent["vip_attempts_remaining"] > 0:
        return True
    _free, paid, _ref = _split_attempts(ent["attempts_remaining"], ent["paid_attempts"], ent["ref_attempts"])
    return paid > 0


def has_attempt(tg_id):
    now = int(time.time())
    ent = get_entitlement(tg_id)
    return (
        ent["unlimited_until"] > now
        or ent["attempts_remaining"] > 0
        or ent["vip_attempts_remaining"] > 0
    )


def _trial_key(platform, username):
    """مفتاح الحساب على المنصة (Qureo بايثون وجافاسكريبت نفس الحساب)."""
    family = "qureo" if platform in ("qureo", "javascript") else (platform or "financial")
    return family, (username or "").strip().lower()


def _free_verdict(tg_id, platform_key, user_key):
    """(ok, reason) للمحاولة المجانية على الحساب ده. لازم تتنادى والـ lock ماسوك."""
    if not user_key:
        return True, None
    claim = _conn.execute(
        "SELECT platform, username FROM free_trial_claims WHERE tg_id=?", (tg_id,)
    ).fetchone()
    if claim and (claim[0], claim[1]) != (platform_key, user_key):
        return False, "other_account"          # المجانية متقفلة على أول حساب دخله
    row = _conn.execute(
        "SELECT tg_id FROM free_trial_accounts WHERE platform=? AND username=?",
        (platform_key, user_key),
    ).fetchone()
    if row and row[0] != tg_id:
        return False, "account_used"           # الحساب ده خد مجانيته أكونت تاني
    return True, None


def check_account_access(tg_id, platform, username):
    """المحاولة المجانية بتتقفل على أول حساب منصة المستخدم يدخله (ومينفعش أكونت تاني ياخد نفس الحساب).
    أما المحاولات المشتراة بكود / VIP / الغير محدود فتشتغل على أي حساب.
    يرجّع {"ok": bool, "reason": no_attempts | account_used | other_account, "kind": free | paid | vip}."""
    now = int(time.time())
    ent = get_entitlement(tg_id)
    if ent["unlimited_until"] > now or ent["vip_attempts_remaining"] > 0:
        return {"ok": True, "kind": "vip"}
    attempts = ent["attempts_remaining"]
    if attempts <= 0:
        return {"ok": False, "reason": "no_attempts"}
    free, paid, ref = _split_attempts(attempts, ent["paid_attempts"], ent["ref_attempts"])
    platform_key, user_key = _trial_key(platform, username)
    with _lock:
        free_ok, reason = _free_verdict(tg_id, platform_key, user_key)
    if free > 0 and free_ok:
        return {"ok": True, "kind": "free"}
    if paid > 0:
        return {"ok": True, "kind": "paid"}
    if ref > 0:
        return {"ok": True, "kind": "ref"}
    return {"ok": False, "reason": reason or "account_used"}


def claim_free_account(tg_id, platform, username):
    """يحجز الحساب للمحاولة المجانية أول ما الحل يبدأ (ذرّي، بيقفل سباق أكونتين في نفس الوقت).
    يرجّع {"claimed": bool, "kind": ...}. VIP/غير محدود/مشترى بكود: مفيش حجز."""
    now = int(time.time())
    platform_key, user_key = _trial_key(platform, username)
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO entitlements(tg_id) VALUES (?)", (tg_id,))
        attempts, vip, unlimited, paid, ref = _conn.execute(
            "SELECT attempts_remaining, vip_attempts_remaining, unlimited_until, paid_attempts, ref_attempts "
            "FROM entitlements WHERE tg_id=?", (tg_id,),
        ).fetchone()
        if unlimited > now or vip > 0 or not user_key:
            _conn.commit()
            return {"claimed": False, "kind": "vip"}
        free, paid, ref = _split_attempts(attempts, paid, ref)
        ok, reason = _free_verdict(tg_id, platform_key, user_key)
        if free <= 0 or not ok:
            _conn.commit()
            return {"claimed": False, "kind": "paid" if (paid + ref) > 0 else "none", "reason": reason}
        _conn.execute(
            "INSERT OR IGNORE INTO free_trial_claims(tg_id, platform, username, claimed_at) VALUES (?,?,?,?)",
            (tg_id, platform_key, user_key, now),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO free_trial_accounts(platform, username, tg_id, used_at) VALUES (?,?,?,?)",
            (platform_key, user_key, tg_id, now),
        )
        _conn.commit()
    return {"claimed": True, "kind": "free"}


def release_free_claim(tg_id, platform, username):
    """يفك الحجز لو الدخول فشل (باسورد غلط) ولسه المحاولة المجانية ما اتستهلكتش."""
    platform_key, user_key = _trial_key(platform, username)
    with _lock:
        done = _conn.execute(
            "SELECT 1 FROM completed_accounts WHERE lower(username)=? AND tg_id=?",
            (user_key, tg_id),
        ).fetchone()
        if done:
            return
        _conn.execute(
            "DELETE FROM free_trial_claims WHERE tg_id=? AND platform=? AND username=?",
            (tg_id, platform_key, user_key),
        )
        _conn.execute(
            "DELETE FROM free_trial_accounts WHERE tg_id=? AND platform=? AND username=?",
            (tg_id, platform_key, user_key),
        )
        _conn.commit()


def consume_attempt(tg_id, platform=None, username=None):
    now = int(time.time())
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO entitlements(tg_id) VALUES (?)", (tg_id,))
        row = _conn.execute(
            "SELECT attempts_remaining, vip_attempts_remaining, unlimited_until, paid_attempts, ref_attempts "
            "FROM entitlements WHERE tg_id=?",
            (tg_id,),
        ).fetchone()
        attempts, vip_attempts, unlimited_until, paid, ref = row
        if unlimited_until > now:
            return {"consumed": True, "kind": "vip_unlimited"}
        if vip_attempts > 0:
            vip_attempts -= 1
            _conn.execute(
                "UPDATE entitlements SET vip_attempts_remaining=? WHERE tg_id=?",
                (vip_attempts, tg_id),
            )
            granted = _vip_granted(tg_id, vip_attempts)
            _conn.commit()
            return {
                "consumed": True, "kind": "vip", "remaining": vip_attempts,
                "granted": granted, "used": granted - vip_attempts,
            }
        if attempts > 0:
            free, paid, ref = _split_attempts(attempts, paid, ref)
            platform_key, user_key = _trial_key(platform, username)
            free_ok, _why = _free_verdict(tg_id, platform_key, user_key)
            if free > 0 and free_ok:
                kind = "single"                       # محاولة مجانية: الحساب ده بقى مرتبط بصاحبها
                if user_key:
                    _conn.execute(
                        "INSERT OR IGNORE INTO free_trial_accounts(platform, username, tg_id, used_at) "
                        "VALUES (?, ?, ?, ?)",
                        (platform_key, user_key, tg_id, now),
                    )
                    _conn.execute(
                        "INSERT OR IGNORE INTO free_trial_claims(tg_id, platform, username, claimed_at) "
                        "VALUES (?, ?, ?, ?)",
                        (tg_id, platform_key, user_key, now),
                    )
            elif paid > 0:
                kind = "single_paid"                  # محاولة مشتراة بكود: تشتغل على أي حساب
                paid -= 1
            elif ref > 0:
                kind = "single_ref"                   # مكافأة دعوات: تشتغل على أي حساب
                ref -= 1
            else:
                _conn.commit()
                return {"consumed": False, "kind": "account_used"}
            attempts -= 1
            _conn.execute(
                "UPDATE entitlements SET attempts_remaining=?, paid_attempts=?, ref_attempts=? WHERE tg_id=?",
                (attempts, paid, ref, tg_id),
            )
            _conn.commit()
            result = {"consumed": True, "kind": kind, "remaining": attempts}
            if kind == "single_paid":
                result["paid_remaining"] = paid
            return result
    _conn.commit()
    return {"consumed": False, "kind": "none"}


def seed_ready_codes(count=300):
    import secrets
    prefixes = {"single": "TRY", "vip": "VIP", "vip_unlimited": "MAX"}
    now = int(time.time())
    created = {}
    with _lock:
        for code_type in ("single", "vip", "vip_unlimited"):
            unused = _conn.execute(
                "SELECT COUNT(*) FROM activation_codes "
                "WHERE code_type=? AND status='unused'",
                (code_type,),
            ).fetchone()[0]
            missing = max(0, count - unused)
            created[code_type] = 0
            for _ in range(missing):
                while True:
                    code = f"{prefixes[code_type]}-{secrets.token_hex(4).upper()}"
                    exists = _conn.execute(
                        "SELECT 1 FROM activation_codes WHERE code=?",
                        (code,),
                    ).fetchone()
                    if not exists:
                        break
                _conn.execute(
                    "INSERT INTO activation_codes(code, code_type, status, created_at) "
                    "VALUES (?, ?, 'unused', ?)",
                    (code, code_type, now),
                )
                created[code_type] += 1
        _conn.commit()
    return created


def generate_code(code_type):
    if code_type not in CODE_TYPES:
        raise ValueError("نوع كود غير صالح")
    import secrets
    prefixes = {"single": "TRY", "vip": "VIP", "vip_unlimited": "MAX"}
    now = int(time.time())
    with _lock:
        while True:
            code = f"{prefixes[code_type]}-{secrets.token_hex(4).upper()}"
            exists = _conn.execute(
                "SELECT 1 FROM activation_codes WHERE code=?",
                (code,),
            ).fetchone()
            if not exists:
                break
        _conn.execute(
            "INSERT INTO activation_codes(code, code_type, status, created_at) "
            "VALUES (?, ?, 'unused', ?)",
            (code, code_type, now),
        )
        _conn.commit()
    return code


MAX_BULK_CODES = 50000
_CODE_PREFIXES = {"single": "TRY", "vip": "VIP", "vip_unlimited": "MAX"}


def generate_codes_bulk(code_type, count):
    """يولّد آلاف الأكواد في معاملة واحدة (executemany) — بيرجّع قائمة الأكواد الجديدة."""
    if code_type not in CODE_TYPES:
        raise ValueError("نوع كود غير صالح")
    import secrets
    count = max(1, min(int(count), MAX_BULK_CODES))
    prefix = _CODE_PREFIXES[code_type]
    now = int(time.time())
    created = []
    with _lock:
        while len(created) < count:
            batch = {f"{prefix}-{secrets.token_hex(4).upper()}" for _ in range(count - len(created))}
            batch -= set(created)
            existing = set()
            items = list(batch)
            for i in range(0, len(items), 500):
                chunk = items[i:i + 500]
                marks = ",".join("?" * len(chunk))
                existing.update(
                    r[0] for r in _conn.execute(
                        f"SELECT code FROM activation_codes WHERE code IN ({marks})", chunk
                    )
                )
            fresh = [c for c in items if c not in existing]
            _conn.executemany(
                "INSERT INTO activation_codes(code, code_type, status, created_at) "
                "VALUES (?, ?, 'unused', ?)",
                [(c, code_type, now) for c in fresh],
            )
            created.extend(fresh)
        _conn.commit()
    return created


def get_codes(code_type=None, limit=30):
    with _lock:
        if code_type:
            rows = _conn.execute(
                "SELECT code, code_type, status, created_at, used_at, used_by, used_name "
                "FROM activation_codes WHERE code_type=? "
                "ORDER BY created_at DESC LIMIT ?",
                (code_type, limit),
            ).fetchall()
        else:
            rows = _conn.execute(
                "SELECT code, code_type, status, created_at, used_at, used_by, used_name "
                "FROM activation_codes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [
        {
            "code": r[0], "code_type": r[1], "status": r[2],
            "created_at": r[3], "used_at": r[4], "used_by": r[5], "used_name": r[6],
        }
        for r in rows
    ]


def redeem_code(code, tg_id, name=""):
    code = code.strip().upper()
    now = int(time.time())
    with _lock:
        row = _conn.execute(
            "SELECT code_type, status FROM activation_codes WHERE code=?",
            (code,),
        ).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        code_type, status = row
        if status != "unused":
            return {"ok": False, "reason": "used"}

        _conn.execute("INSERT OR IGNORE INTO entitlements(tg_id) VALUES (?)", (tg_id,))
        if code_type == "single":
            _conn.execute(
                "UPDATE entitlements SET attempts_remaining=attempts_remaining+1, "
                "paid_attempts=paid_attempts+1 WHERE tg_id=?",
                (tg_id,),
            )
        elif code_type == "vip":
            _conn.execute(
                "UPDATE entitlements SET vip_attempts_remaining=vip_attempts_remaining+? WHERE tg_id=?",
                (VIP_PACK_SIZE, tg_id),
            )
        elif code_type == "vip_unlimited":
            current = _conn.execute(
                "SELECT unlimited_until FROM entitlements WHERE tg_id=?", (tg_id,)
            ).fetchone()[0] or 0
            _conn.execute(
                "UPDATE entitlements SET unlimited_until=? WHERE tg_id=?",
                (max(current, now) + UNLIMITED_DAYS * 86400, tg_id),   # كود تاني قبل ما الأسبوع يخلص = بيتضاف عليه
            )
        else:
            return {"ok": False, "reason": "invalid"}

        _conn.execute(
            "UPDATE activation_codes SET status='used', used_at=?, used_by=?, used_name=? "
            "WHERE code=?",
            (now, tg_id, name[:100], code),
        )
        _conn.commit()
    return {"ok": True, "code_type": code_type}


def _vip_row(tg_id, name, packs, weeklies, remaining, unlimited_until, last_at, now):
    granted = max(packs * VIP_PACK_SIZE, remaining)
    unlimited_active = unlimited_until > now
    return {
        "tg_id": tg_id,
        "name": name or "",
        "packs": packs,                       # عدد أكواد الـ١٠ محاولات المفعّلة
        "weeklies": weeklies,                 # عدد أكواد الأسبوع المفعّلة
        "granted": granted,
        "used": granted - remaining if packs or remaining else 0,
        "remaining": remaining,
        "unlimited_until": unlimited_until if unlimited_active else 0,
        "unlimited_expired_at": 0 if unlimited_active or not weeklies else unlimited_until,
        "unlimited_active": unlimited_active,
        "active": remaining > 0 or unlimited_active,
        "last_at": last_at or 0,
    }


_VIP_SQL = (
    "SELECT c.used_by, "
    "  COALESCE(NULLIF(u.tg_name, ''), MAX(c.used_name), ''), "
    "  SUM(CASE WHEN c.code_type='vip' THEN 1 ELSE 0 END), "
    "  SUM(CASE WHEN c.code_type='vip_unlimited' THEN 1 ELSE 0 END), "
    "  COALESCE(e.vip_attempts_remaining, 0), COALESCE(e.unlimited_until, 0), "
    "  MAX(c.used_at) "
    "FROM activation_codes c "
    "LEFT JOIN entitlements e ON e.tg_id = c.used_by "
    "LEFT JOIN users u ON u.tg_id = c.used_by "
    "WHERE c.status='used' AND c.code_type IN ('vip', 'vip_unlimited') AND c.used_by IS NOT NULL "
)


def get_vip_status(tg_id):
    """حالة اشتراك VIP لمستخدم واحد (أو None لو عمره ما فعّل كود VIP)."""
    now = int(time.time())
    with _lock:
        row = _conn.execute(_VIP_SQL + "AND c.used_by=? GROUP BY c.used_by", (tg_id,)).fetchone()
    if not row:
        return None
    return _vip_row(*row, now)


def get_vip_subscribers(status="all"):
    """كل مشتركي VIP: status = all | active (لسه معاه محاولات/أسبوع شغّال) | done (خلّص اشتراكه).
    النشطين الأول، وبعدهم الأحدث تفعيلًا."""
    now = int(time.time())
    with _lock:
        rows = _conn.execute(_VIP_SQL + "GROUP BY c.used_by").fetchall()
    subs = [_vip_row(*r, now) for r in rows]
    if status == "active":
        subs = [s for s in subs if s["active"]]
    elif status == "done":
        subs = [s for s in subs if not s["active"]]
    subs.sort(key=lambda s: (not s["active"], -s["last_at"]))
    return subs


def get_vip_summary():
    subs = get_vip_subscribers("all")
    return {
        "total": len(subs),
        "active": sum(1 for s in subs if s["active"]),
        "done": sum(1 for s in subs if not s["active"]),
        "weekly_active": sum(1 for s in subs if s["unlimited_active"]),
        "granted": sum(s["granted"] for s in subs),
        "used": sum(s["used"] for s in subs),
        "remaining": sum(s["remaining"] for s in subs),
    }


REF_PER_REWARD = 5      # كل ٥ دعوات = محاولة
REF_MAX_REWARDS = 2     # أقصى عدد محاولات مجانية بتتمنح من الدعوات (٥ = محاولة، ١٠ = محاولتين)


def get_referral_progress(tg_id):
    """حالة مكافآت الدعوات لمستخدم: عدد المدعوين، المحاولات المستلمة، والتقدم للجاية."""
    with _lock:
        count = _conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (tg_id,)
        ).fetchone()[0]
        row = _conn.execute(
            "SELECT rewards_granted FROM invite_rewards WHERE tg_id=?", (tg_id,)
        ).fetchone()
    granted = row[0] if row else 0
    capped = granted >= REF_MAX_REWARDS
    progress = 0 if capped else count % REF_PER_REWARD
    return {
        "count": count, "granted": granted, "max": REF_MAX_REWARDS, "capped": capped,
        "progress": progress, "remaining": 0 if capped else REF_PER_REWARD - progress,
    }


def register_referral(inviter_id, referred_id):
    if inviter_id == referred_id:
        return {"ok": True, "new_rewards": 0, "counted": False}
    now = int(time.time())
    with _lock:
        exists = _conn.execute(
            "SELECT 1 FROM referrals WHERE referred_id=?",
            (referred_id,),
        ).fetchone()
        if exists:
            return {"ok": True, "new_rewards": 0, "counted": False}
        _conn.execute(
            "INSERT INTO referrals(referred_id, inviter_id, created_at) VALUES (?, ?, ?)",
            (referred_id, inviter_id, now),
        )
        count = _conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE inviter_id=?",
            (inviter_id,),
        ).fetchone()[0]
        row = _conn.execute(
            "SELECT rewards_granted FROM invite_rewards WHERE tg_id=?",
            (inviter_id,),
        ).fetchone()
        granted = row[0] if row else 0
        should_grant = min(count // REF_PER_REWARD, REF_MAX_REWARDS)   # سقف المحاولات المجانية من الدعوات
        new_rewards = max(0, should_grant - granted)
        if new_rewards:
            _conn.execute("INSERT OR IGNORE INTO entitlements(tg_id) VALUES (?)", (inviter_id,))
            _conn.execute(
                "INSERT INTO invite_rewards(tg_id, rewards_granted) VALUES (?, ?) "
                "ON CONFLICT(tg_id) DO UPDATE SET rewards_granted=?",
                (inviter_id, should_grant, should_grant),
            )
            _conn.execute(
                "UPDATE entitlements SET attempts_remaining=attempts_remaining+?, ref_attempts=ref_attempts+? WHERE tg_id=?",
                (new_rewards, new_rewards, inviter_id),
            )
        _conn.commit()
    # count = إجمالي المدعوين، progress = التقدم ناحية المحاولة الجاية (0..4)، remaining = فاضل كام
    total_granted = max(granted, should_grant)
    capped = total_granted >= REF_MAX_REWARDS
    progress = 0 if capped else count % REF_PER_REWARD
    return {"ok": True, "new_rewards": new_rewards, "counted": True, "count": count,
            "granted": total_granted, "max": REF_MAX_REWARDS, "capped": capped,
            "progress": progress, "remaining": 0 if capped else REF_PER_REWARD - progress}
