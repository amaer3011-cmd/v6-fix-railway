"""بوت تيليجرام — حل منصة الثقافة المالية (v6: شريط تقدم محسّن + إحصائيات)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
import html
import io
import logging
import os
import random
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

load_dotenv()

import api_engine as core
import matching
import db
import qureo_engine
import subscription_texts as st
from verses import VERSES

BASE         = Path(__file__).resolve().parent

# مسارات البيانات النسبية (زي DB_PATH=bot.db) بتتثبّت على فولدر البوت، مش على المكان اللي شغّلت منه الأمر.
# غير كده لو شغّلت البوت من فولدر تاني كان هيفتح قاعدة فاضية جديدة وتحس إن المستخدمين اختفوا.
for _key in ("DB_PATH", "LEARNED_FILE", "UNMATCHED_FILE"):
    _val = (os.getenv(_key) or "").strip()
    if _val and not Path(_val).is_absolute():
        os.environ[_key] = str(BASE / _val)
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
DEV_USERNAME = os.getenv("DEV_USERNAME", "A7med19_7").lstrip("@")


def _int_setting(name, default, minimum):
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}.")
    return value


MAX_JOBS        = _int_setting("MAX_JOBS",        1,   1)
MAX_QUEUE       = _int_setting("MAX_QUEUE",       10,  0)
JOB_TIMEOUT_MIN = _int_setting("JOB_TIMEOUT_MIN", 240, 1)
PRIORITY_JOBS   = _int_setting("PRIORITY_JOBS",   10,  1)   # حسابات المشتركين اللي بتتحل في نفس الوقت (من غير طابور)
PRICE_SINGLE    = _int_setting("PRICE_SINGLE",    50,  0)
PRICE_PACK10    = _int_setting("PRICE_PACK10",    120, 0)
PRICE_WEEKLY    = _int_setting("PRICE_WEEKLY",    250, 0)
OWNER_CHAT_ID   = _int_setting("OWNER_CHAT_ID",   0,   0)
ANSWERS_FILE    = BASE / os.getenv("ANSWERS_FILE", "answers.txt")
QUREO_ANSWERS_FILE = BASE / os.getenv("QUREO_ANSWERS_FILE", "qureo_answers.txt")
JAVASCRIPT_ANSWERS_FILE = BASE / os.getenv("JAVASCRIPT_ANSWERS_FILE", "javascript_answers.md")
JAVASCRIPT_COURSE_ID = os.getenv("JAVASCRIPT_COURSE_ID", "javascript_ar")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("finbot")

_AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


# ═══════════════════════════════════════════════════════
#  أدوات تنسيق الأرقام والوقت
# ═══════════════════════════════════════════════════════

def ar_num(n):
    return f"{n:,}".replace(",", "٬").translate(_AR_DIGITS)

def ar_num_two_digits(n):
    return ar_num(n).rjust(2, "٠")

def ar_percent(done, total):
    if total <= 0:
        return "٠٪"
    pct = max(0, min(100, int(done * 100 / total)))
    return ar_num(pct) + "٪"

def progress_bar(done, total, width=10):
    if width <= 0:
        return ""
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)

def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rem  = divmod(seconds, 3600)
    minutes, s  = divmod(rem, 60)
    if hours:
        return f"{ar_num(hours)}س {ar_num_two_digits(minutes)}د {ar_num_two_digits(s)}ث"
    if minutes:
        return f"{ar_num(minutes)}د {ar_num_two_digits(s)}ث"
    return f"{ar_num(seconds)}ث"

def fmt_ts(ts):
    """Unix timestamp → تاريخ قابل للقراءة."""
    if not ts:
        return "—"
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y/%m/%d %H:%M")


# ═══════════════════════════════════════════════════════
#  رسائل الملخص
# ═══════════════════════════════════════════════════════

def job_summary(questions, activities, elapsed, platform_progress=None):
    progress_line = ""
    if (
        isinstance(platform_progress, tuple)
        and len(platform_progress) == 2
        and platform_progress[1] > 0
    ):
        done, total = platform_progress
        done = max(0, min(done, total))
        bar  = progress_bar(done, total, 12)
        progress_line = (
            f"📊 آخر تقدم مؤكد: <code>{bar}</code> <b>{ar_percent(done, total)}</b> "
            f"({ar_num(done)}/{ar_num(total)})\n"
        )
    if activities is None:
        activity_line = "📚 العناصر المكتملة: غير مؤكدة\n"
    else:
        activity_line = f"📚 العناصر المكتملة: <b>{ar_num(activities)}</b>\n"
    return (
        "<b>ملخص المهمة</b>\n"
        + progress_line
        + f"📝 أسئلة أُجيبت: <b>{ar_num(questions)}</b>\n"
        + activity_line
        + f"⏱️ إجمالي الوقت: <b>{format_duration(elapsed)}</b>"
    )


def perfect_note(result):
    """سطر للمستخدم عن مرحلة «100% في كل اختبار»: اللي اتصلّح واللي فضل ناقص."""
    fixed = result.get("perfect_fixed") or []
    left = result.get("not_perfect") or []
    lines = []
    if fixed:
        lines.append(f"🎯 اتعادت <b>{ar_num(len(fixed))}</b> مراجعة/اختبار لحد ما بقوا 100%.")
    if left:
        names = "، ".join(html.escape(str(n)) for n in left[:5])
        lines.append(f"⚠️ لسه مش 100%: <b>{names}</b>\nالمنصة ما سمحتش بإعادتهم أو فيهم سؤال مش معروف.")
    return ("\n\n" + "\n".join(lines)) if lines else ""


CHAPTERS_MAX_ROWS = 25
CHAPTERS_MAX_CHARS = 1800


def chapters_report(result):
    """قائمة الفصول اللي اشتغل عليها البوت في Qureo: دروس اتحلّت + نتيجة المراجعة/الاختبار + هل الفصل اتقفل.
    فاضية لو النتيجة مفيهاش فصول (منصة الثقافة المالية)."""
    rows = result.get("chapters") or []
    if not rows:
        return ""
    lines = []
    for row in rows[:CHAPTERS_MAX_ROWS]:
        name = html.escape(str(row.get("name") or "—")[:45])
        state = row.get("state")
        correct, total = row.get("correct"), row.get("total")
        parts = []
        if row.get("lectures"):
            parts.append(f"{ar_num(row['lectures'])} درس")
        if state == "fixed" and total:
            parts.append(f"أُعيد {ar_num(row.get('before') or 0)}/{ar_num(total)} ← {ar_num(correct)}/{ar_num(total)}")
        elif correct is not None and total:
            parts.append(f"{ar_num(correct)}/{ar_num(total)}")
        if state == "fixed":
            icon = "🎯"
        elif state == "not_perfect":
            icon = "⚠️"
        elif row.get("was_done"):
            icon = "⏭️"
        else:
            icon = "✅"
        diamond = row.get("diamond")
        if isinstance(diamond, (int, float)) and not isinstance(diamond, bool):
            parts.append(f"💎{ar_num(int(diamond))}")
        if row.get("closed") is False:
            parts.append("لسه مش مقفول")
        tail = f" — {' • '.join(parts)}" if parts else ""
        lines.append(f"{icon} {name}{tail}")
    hidden = len(rows) - CHAPTERS_MAX_ROWS
    if hidden > 0:
        lines.append(f"… و{ar_num(hidden)} فصل آخر")
    body = "\n".join(lines)
    if len(body) > CHAPTERS_MAX_CHARS:
        body = body[:CHAPTERS_MAX_CHARS].rsplit("\n", 1)[0] + "\n…"
    legend = "<i>✅ اتحل • 🎯 اتعاد لـ 100% • ⏭️ مكتمل قبل كده • ⚠️ لسه ناقص • 💎 الألماس</i>"
    return f"\n\n📚 <b>الفصول ({ar_num(len(rows))}):</b>\n{body}\n{legend}{diamonds_line(result)}"


def diamonds_line(result):
    """إجمالي الألماس في الكورس قبل/بعد الحل (من المنصة نفسها). فاضي لو المنصة ما رجّعتوش."""
    before, after = result.get("diamond_before"), result.get("diamond_after")
    if not isinstance(after, (int, float)) or isinstance(after, bool):
        return ""
    after = int(after)
    if isinstance(before, (int, float)) and not isinstance(before, bool) and int(before) != after:
        gained = after - int(before)
        sign = "+" if gained > 0 else "−"
        return f"\n\n💎 <b>الألماس:</b> {ar_num(int(before))} ← <b>{ar_num(after)}</b> ({sign}{ar_num(abs(gained))})"
    return f"\n\n💎 <b>إجمالي الألماس:</b> {ar_num(after)}"


def completion_message(questions, activities, elapsed, last, platform_progress=None):
    return (
        "🎉🎉🎉 <b>مبروووووووك تم انجاز حل المنصة كامل يا غالي</b> 🎉🎉🎉\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>مافيش أنشطة مفتوحة متاحة حاليًا.</i>\n\n"
        f"{job_summary(questions, activities, elapsed, platform_progress)}\n"
        f"\n📍 <b>آخر نشاط:</b> {html.escape(last or '—')}\n\n"
        "<i>قد تفتح المنصة أنشطة إضافية لاحقًا.</i>"
    )


# ═══════════════════════════════════════════════════════
#  منطق الطابور
# ═══════════════════════════════════════════════════════

def queue_full(app):
    """الطابور بتاع المستخدمين العاديين بس. المشتركين مبيدخلوش الطابور."""
    return app.bot_data.get("normal_active", 0) >= MAX_JOBS + MAX_QUEUE


def queue_closed(app):
    """الأدمن قفل الطابور: المستخدمين العاديين مايقدروش يبدأوا حل جديد."""
    return bool(app.bot_data.get("queue_closed", False))


def is_vip_user(uid):
    """مين يتحل له فورًا من غير طابور: المالك / VIP / غير محدود / معاه محاولة مشتراة بكود."""
    if uid == OWNER_CHAT_ID:
        return True
    return db.has_priority(uid)


def job_groups(bot_data):
    """(شغّالة, منتظرة): uids الحلول المسجّلة مقسّمة حسب حالتها.
    الحل بيتسجّل في cancels أول ما يتطلب (عشان زرار الإيقاف يشتغل وهو في الطابور)،
    فعدد cancels لوحده بيجمع الشغّال والمنتظر مع بعض؛ job_states هي اللي بتفرّق بينهم."""
    states = bot_data.get("job_states", {})
    running, waiting = [], []
    for uid in bot_data.get("cancels", {}):
        (running if states.get(uid) == "running" else waiting).append(uid)
    return running, waiting


async def acquire_job_slot(app, semaphore, cancel, vip_priority=False, poll_interval=0.25):
    vip_waiting = app.bot_data.setdefault("vip_waiting", set())
    while not cancel.is_set():
        if not vip_priority and vip_waiting:
            await asyncio.sleep(poll_interval)
            continue
        try:
            acquired = await asyncio.wait_for(semaphore.acquire(), timeout=poll_interval)
        except asyncio.TimeoutError:
            continue
        if cancel.is_set():
            semaphore.release()
            return False
        return acquired
    return False


QUEUE_FULL_TEXT = "🚦 البوت مشغول دلوقتي والطابور ممتلئ. جرّب تاني بعد شوية 🌿"
QUEUE_CLOSED_TEXT = (
    "🔒 <b>الطابور مقفول دلوقتي.</b>\n"
    "مش بنستقبل طلبات حل جديدة حاليًا، جرّب تاني بعد شوية.\n\n"
    "⚡ <i>أصحاب الاشتراكات بيتحل لهم فورًا من غير طابور.</i>"
)


def subscription_text(header="💎 <b>اشتراكات البوت</b>"):
    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"1️⃣ <b>محاولة واحدة</b> — <b>{ar_num(PRICE_SINGLE)} ج</b>\n"
        f"2️⃣ <b>١٠ محاولات</b> — <b>{ar_num(PRICE_PACK10)} ج</b>\n"
        f"3️⃣ <b>غير محدود أسبوع كامل</b> — <b>{ar_num(PRICE_WEEKLY)} ج</b>\n\n"
        "✨ <b>مميزات المشتركين:</b>\n"
        "⚡ حلّك بيبدأ <b>فورًا</b> من غير ما تقف في الطابور\n"
        "🔓 تحل أي حساب على أي منصة (مش متقفل على حساب واحد)\n\n"
        "🛒 للشراء كلّم المطوّر وهيبعتلك كود التفعيل، وبعدها اضغط «تفعيل كود اشتراك».\n"
        f"🎁 أو ادعُ أصدقاءك: كل {ar_num(db.REF_PER_REWARD)} أشخاص = محاولة مجانية "
        f"(بحد أقصى {ar_num(db.REF_MAX_REWARDS)})."
    )


def subscription_kb():
    return InlineKeyboardMarkup([
        [btn("🎟️ تفعيل كود اشتراك", "success", callback_data="redeem_start")],
        [btn("💬 اشترك الآن — تواصل مع المطوّر", url=f"https://t.me/{DEV_USERNAME}")],
        [btn("👥 اكسب محاولات بالدعوات", "primary", callback_data="invite")],
        [btn("🏠 القائمة الرئيسية", "primary", callback_data="menu")],
    ])


# ═══════════════════════════════════════════════════════
#  أزرار
# ═══════════════════════════════════════════════════════

BUTTON_STYLES = ("primary", "success", "danger")      # القيم الوحيدة اللي تيليجرام بيقبلها في style


def btn(text, style=None, **kw):
    if style in BUTTON_STYLES:     # أي قيمة تانية (زي "url") بتخلّي تيليجرام يرفض الرسالة كلها
        try:
            return InlineKeyboardButton(text, style=style, **kw)
        except TypeError:
            pass
    return InlineKeyboardButton(text, **kw)


def main_menu(total):
    return InlineKeyboardMarkup([
        [btn("🚀 الثقافة المالية", "success", callback_data="solve_financial")],
        [btn("🐍 Python على Qureo", "success", callback_data="solve_qureo")],
        [btn("🟨 JavaScript على Qureo", "success", callback_data="solve_javascript")],
        [
            btn("📖 طريقة الاستخدام", "primary", callback_data="help"),
            btn("🔒 الخصوصية", "primary", callback_data="privacy"),
        ],
        [
            btn("📈 إحصائياتي", "primary", callback_data="mystats"),
            btn(f"🤍 صلِّ على النبي ﷺ · {ar_num(total)}", "primary", callback_data="salat"),
        ],
        [btn("👥 دعوتي — اكسب محاولة", "primary", callback_data="invite")],
        [btn("💎 الاشتراكات والأسعار", "success", callback_data="plans")],
        [btn("🎟️ تفعيل كود اشتراك", "success", callback_data="redeem_start")],
        [btn("💬 تواصل مع المطوّر", url=f"https://t.me/{DEV_USERNAME}")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup([[btn("❌ إلغاء", "danger", callback_data="cancel_flow")]])

def stop_kb():
    return InlineKeyboardMarkup([[btn("⛔ إيقاف الحل", "danger", callback_data="stop")]])

def after_kb():
    return InlineKeyboardMarkup([
        [
            btn("🔁 الثقافة المالية", "success", callback_data="solve_financial"),
            btn("🐍 Qureo Python", "success", callback_data="solve_qureo"),
        ],
        [btn("🟨 Qureo JavaScript", "success", callback_data="solve_javascript")],
        [btn("📈 إحصائياتي",        "primary", callback_data="mystats")],
        [btn("🏠 القائمة الرئيسية", "primary", callback_data="menu")],
    ])

def invite_link(bot_username, inviter_id):
    return f"https://t.me/{bot_username.lstrip('@')}?start=ref_{inviter_id}"


# ═══════════════════════════════════════════════════════
#  رسائل الترحيب
# ═══════════════════════════════════════════════════════

async def _notify_new_member(ctx, user):
    """إشعار الأدمن بدخول عضو جديد."""
    if not OWNER_CHAT_ID:
        return
    username = f"@{user.username}" if user.username else "بدون يوزر"
    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    try:
        await ctx.application.bot.send_message(
            OWNER_CHAT_ID,
            "👋 <b>عضو جديد دخل للبوت</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 الاسم: {mention}\n"
            f"🔗 اليوزر: {html.escape(username)}\n"
            f"🆔 Telegram ID: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        log.warning("new member notification failed (%s)", type(exc).__name__)


async def _apply_referral(ctx, user, ref):
    """يسجّل الدعوة (ref_<id>) ويبلّغ صاحب الدعوة."""
    ref = (ref or "").strip()
    if not ref.lower().startswith("ref_"):
        return
    try:
        inviter_id = int(ref[4:])
    except ValueError:
        inviter_id = None
    if inviter_id:
        reward = db.register_referral(inviter_id, user.id)
        if reward.get("counted"):
            who = html.escape(user.first_name or user.full_name or "شخص جديد")
            cap = ar_num(reward["max"])
            if reward["new_rewards"]:
                extra = (
                    f"\n🏁 وصلت للحد الأقصى ({cap} محاولات مجانية من الدعوات)."
                    if reward["capped"] else
                    f"\nكمّل ادعي: فاضل لك {ar_num(reward['max'] - reward['granted'])} محاولة بحد أقصى."
                )
                text = (
                    f"🎉 <b>تم دخول شخص جديد للبوت بواسطتك!</b>\n"
                    f"👤 {who}\n\n"
                    f"🎁 <b>مبروك!</b> اخدت "
                    f"{ar_num(reward['new_rewards'])} محاولة مجانية تحل بيها أي منصة وأي حساب. 🚀"
                    f"{extra}"
                )
            elif reward["capped"]:
                text = (
                    f"🎉 <b>تم دخول شخص جديد للبوت بواسطتك!</b>\n"
                    f"👤 {who}\n\n"
                    f"✅ اخدت الحد الأقصى من المحاولات المجانية بالدعوات ({cap}).\n"
                    f"💎 لو عايز محاولات أكتر شوف الاشتراكات: /plans"
                )
            else:
                done, left = reward["progress"], reward["remaining"]
                text = (
                    f"🎉 <b>تم دخول شخص جديد للبوت بواسطتك!</b>\n"
                    f"👤 {who}\n\n"
                    f"📊 التقدم: <b>{ar_num(done)} / ٥</b>\n"
                    f"🔥 فاضل <b>{ar_num(left)}</b> "
                    f"{'شخص واحد' if left == 1 else 'أشخاص'} وتاخد محاولة مجانية!"
                )
            try:
                await ctx.application.bot.send_message(
                    inviter_id, text, parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass


JOINED_STATUSES = ("member", "administrator", "creator")
SUB_CACHE_SECONDS = _int_setting("SUB_CACHE_SECONDS", 300, 0)   # بعد ما المستخدم يثبت اشتراكه ما نسألش تيليجرام عنه تاني المدة دي
_sub_ok = {}          # uid -> وقت انتهاء الكاش (monotonic)
_gate_warned = {}     # chat_id -> آخر مرة بلّغنا الأدمن إن القناة فيها مشكلة إعداد
GATE_WARN_EVERY = 6 * 3600
_CONFIG_ERROR_RE = re.compile(
    r"chat not found|member list is inaccessible|not enough rights|admin(istrator)? (rights|required)|"
    r"chat_admin_required|channel_private|not a member|was kicked|bot is not|forbidden",
    re.I,
)


def gate_active(uid):
    """هل الاشتراك الإجباري مطلوب من المستخدم ده دلوقتي؟ (المالك مستثنى)"""
    return (
        uid != OWNER_CHAT_ID
        and db.get_setting("required_channels_enabled", "1") == "1"
        and bool(db.get_required_channels())
    )


def reset_sub_cache():
    _sub_ok.clear()


def _is_joined(member):
    status = getattr(member, "status", "")
    return status in JOINED_STATUSES or (status == "restricted" and bool(getattr(member, "is_member", False)))


def _is_channel_config_error(exc):
    """خطأ في إعداد القناة نفسها (البوت مش مشرف/اتشال/القناة مش موجودة) — مش ذنب المستخدم."""
    return isinstance(exc, Forbidden) or (isinstance(exc, BadRequest) and bool(_CONFIG_ERROR_RE.search(str(exc))))


def _normalize_link(link):
    link = (link or "").strip()
    if link.startswith("@"):
        link = "https://t.me/" + link[1:]
    elif re.match(r"^(t\.me|telegram\.me)/", link, re.I):
        link = "https://" + link
    return link if re.match(r"^(https?|tg)://\S+$", link, re.I) else ""


async def _channel_url(app, channel):
    """رابط الانضمام للقناة. لو المخزّن مش صالح بنجيبه من تيليجرام (يوزر القناة أو رابط الدعوة)."""
    link = _normalize_link(channel.get("link"))
    if link:
        return link
    try:
        chat = await app.bot.get_chat(channel["chat_id"])
    except TelegramError:
        return ""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}"
    return _normalize_link(getattr(chat, "invite_link", ""))


async def _warn_owner_channel_problem(app, channel, exc):
    log.warning("required channel %s can't be verified (%s: %s)", channel["chat_id"], type(exc).__name__, exc)
    if not OWNER_CHAT_ID:
        return
    now = time.monotonic()
    if now - _gate_warned.get(channel["chat_id"], -GATE_WARN_EVERY) < GATE_WARN_EVERY:
        return
    _gate_warned[channel["chat_id"]] = now
    try:
        await app.bot.send_message(
            OWNER_CHAT_ID,
            "⚠️ <b>اشتراك القنوات الإجباري</b>\n"
            f"البوت مش قادر يتحقق من القناة <b>{html.escape(channel['title'])}</b> "
            f"(<code>{html.escape(str(channel['chat_id']))}</code>).\n"
            "اتأكد إن البوت <b>مشرف</b> في القناة. القناة دي اتخطّت مؤقتًا عشان المستخدمين ما يتعلّقوش.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass


async def _missing_channels(app, uid, channels):
    missing = []
    for channel in channels:
        try:
            member = await app.bot.get_chat_member(channel["chat_id"], uid)
        except TelegramError as exc:
            if _is_channel_config_error(exc):
                await _warn_owner_channel_problem(app, channel, exc)     # الإعداد غلط: ما نقفلش البوت على الكل
            else:
                missing.append(channel)                                   # شبكة/مهلة: يضغط «تحقق» تاني
            continue
        if not _is_joined(member):
            missing.append(channel)
    return missing


async def _send_gate_message(chat, message, markup, fallback_links):
    """يبعت رسالة الاشتراك. لو تيليجرام رفضها (HTML غلط، رابط زرار مش صالح…) بنجرّب صيغ أبسط بدل ما المستخدم ما يشوفش حاجة."""
    attempts = [
        dict(text=message, parse_mode=ParseMode.HTML if "<" in message else None, reply_markup=markup),
        dict(text=message, parse_mode=None, reply_markup=markup),
        dict(
            text=message + ("\n\n" + "\n".join(fallback_links) if fallback_links else ""),
            parse_mode=None,
            reply_markup=InlineKeyboardMarkup([[btn("✅ تحقق من الاشتراك", callback_data="check_channels")]]),
        ),
    ]
    for kwargs in attempts:
        try:
            await chat.send_message(**kwargs)
            return True
        except BadRequest as exc:
            log.warning("subscription message rejected by Telegram (%s); trying a simpler form", exc)
        except TelegramError as exc:
            log.warning("subscription message not delivered (%s)", type(exc).__name__)
            return False
    return False


async def send_channel_gate(app, chat, uid):
    """يتحقق من اشتراك المستخدم (بدون كاش). True = مشترك. False = اتبعتله رسالة الاشتراك."""
    missing = await _missing_channels(app, uid, db.get_required_channels())
    if not missing:
        if SUB_CACHE_SECONDS:
            if len(_sub_ok) > 5000:
                now = time.monotonic()
                for key in [k for k, v in _sub_ok.items() if v <= now]:
                    _sub_ok.pop(key, None)
            _sub_ok[uid] = time.monotonic() + SUB_CACHE_SECONDS
        return True
    _sub_ok.pop(uid, None)
    rows, fallback_links = [], []
    for channel in missing:
        url = await _channel_url(app, channel)
        title = (channel["title"] or str(channel["chat_id"]))[:60]
        if url:
            rows.append([btn(title, url=url)])
            fallback_links.append(f"• {title}: {url}")
        else:
            fallback_links.append(f"• {title}")
    rows.append([btn("✅ تحقق من الاشتراك", "success", callback_data="check_channels")])
    message = db.get_setting(
        "required_join_message",
        "لازم تشترك في القنوات المطلوبة عشان تستخدم البوت. اشترك، بعدين اضغط «تحقق من الاشتراك».",
    )
    await _send_gate_message(chat, message, InlineKeyboardMarkup(rows), fallback_links)
    return False


async def ensure_subscribed(app, chat, uid):
    """True لو مفيش اشتراك مطلوب أو المستخدم مشترك (بكاش قصير). غير كده بيبعتله رسالة الاشتراك."""
    if not gate_active(uid):
        return True
    if _sub_ok.get(uid, 0) > time.monotonic():
        return True
    return await send_channel_gate(app, chat, uid)


async def _gate_blocks(update, ctx):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type != "private":
        return False
    query = update.callback_query
    if update.message is None and query is None:
        return False
    if query is not None and query.data == "check_channels":
        return False                       # زرار «تحقق» بيعمل التحقق بنفسه
    if not gate_active(user.id) or _sub_ok.get(user.id, 0) > time.monotonic():
        return False
    is_new = db.touch_user(user.id, user.full_name, user.username or "")
    if update.message is not None and (update.message.text or "").startswith("/start"):
        parts = update.message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().lower().startswith("ref_"):
            ctx.user_data["pending_ref"] = parts[1].strip()      # الدعوة تتسجّل بعد ما يشترك
    if await send_channel_gate(ctx.application, chat, user.id):
        return False
    if is_new:
        await _notify_new_member(ctx, user)
    if query is not None:
        try:
            await query.answer("اشترك في القنوات الأول 🔒")
        except TelegramError:
            pass
    return True


async def on_gate_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """بوابة الاشتراك الإجباري: بتشتغل قبل أي معالج تاني (رسالة أو زرار)، فالمستخدم ما يستخدمش البوت قبل ما يشترك."""
    try:
        blocked = await _gate_blocks(update, ctx)
    except Exception:  # noqa: BLE001 — عطل في البوابة ما يوقفش البوت كله
        log.exception("subscription gate failed; letting the update through")
        return
    if blocked:
        raise ApplicationHandlerStop


async def on_check_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    if await send_channel_gate(ctx.application, q.message.chat, user.id):
        await q.message.reply_text("تم التحقق من اشتراكك ✅")
        await _apply_referral(ctx, user, ctx.user_data.pop("pending_ref", None))
        await send_welcome(q.message.chat, user.first_name or "يا غالي")


async def send_welcome(chat, name):
    v       = random.choice(VERSES)
    caption = (
        "✨ <b>FinBot | مساعدك الدراسي</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"أهلاً بيك يا <b>{html.escape(name)}</b> 👋\n\n"
        f"<blockquote>{html.escape(v['shatr1'])}\n{html.escape(v['shatr2'])}\n"
        f"<i>{html.escape(v['poet'])}</i></blockquote>\n\n"
        "🎓 <b>وفّر وقتك وركّز في تعلّمك.</b>\n"
        "اختار المنصة من الأزرار تحت، والبوت يتابع التقدّم معاك.\n\n"
        "🤍 صلِّ على النبي ﷺ"
    )
    markup = main_menu(db.get_salawat())
    await chat.send_message(caption, parse_mode=ParseMode.HTML, reply_markup=markup)


# ═══════════════════════════════════════════════════════
#  معالجات الأوامر الأساسية
# ═══════════════════════════════════════════════════════

async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = db.touch_user(user.id, user.full_name, user.username or "")
    pending_ref = ctx.user_data.get("pending_ref")      # دعوة اتحفظت وقت ما كان لسه ما اشتركش
    ctx.user_data.clear()
    if not await ensure_subscribed(ctx.application, update.effective_chat, user.id):
        if pending_ref:
            ctx.user_data["pending_ref"] = pending_ref
        return
    if is_new:
        await _notify_new_member(ctx, user)
    args = getattr(ctx, "args", None) or []
    await _apply_referral(ctx, user, args[0] if args else pending_ref)
    await send_welcome(update.effective_chat, user.first_name or "يا غالي")


async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member
    if not member or not member.new_chat_member:
        return
    old_status = member.old_chat_member.status
    new_status = member.new_chat_member.status
    if old_status in ("member", "administrator", "creator") or new_status not in ("member", "administrator", "creator"):
        return
    user = member.new_chat_member.user
    db.touch_user(user.id, user.full_name, user.username or "")
    if not OWNER_CHAT_ID:
        return
    username = f"@{user.username}" if user.username else "بدون يوزر"
    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    try:
        await ctx.application.bot.send_message(
            OWNER_CHAT_ID,
            "👋 <b>عضو جديد دخل للبوت</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 الاسم: {mention}\n"
            f"🔗 اليوزر: {html.escape(username)}\n"
            f"🆔 Telegram ID: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        log.warning("new member notification failed (%s)", type(exc).__name__)


async def on_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("تمام، اتلغى. اكتب /start عشان ترجع للقائمة.")


HELP_TEXT = (
    "📖 <b>طريقة الاستخدام</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "١. اختار المنصة من القائمة الرئيسية.\n"
    "٢. ابعت كود الدخول؛ هيتحفظ عشان تستخدمه بعدين.\n"
    "٣. ابعت كلمة المرور، ورسالتك هتتمسح بعد استلامها.\n"
    "٤. تابع التقدّم، وتقدر توقف المهمة في أي وقت.\n\n"
    "💡 <i>الأنشطة المكتملة مسبقًا بيتخطّاها البوت تلقائيًا.</i>\n\n"
    "⌨️ <b>أوامر سريعة</b>\n"
    "<code>/start</code> القائمة الرئيسية\n"
    "<code>/mystats</code> إحصائياتك\n"
    "<code>/cancel</code> إلغاء الخطوة الحالية"
)

PRIVACY_TEXT = (
    "🔒 <b>الأمان والخصوصية</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "✅ <b>كود الدخول:</b> يُحفظ لتسهيل الاستخدام.\n"
    "✅ <b>كلمة المرور:</b> لا تُحفظ في قاعدة البيانات، وتُستخدم لتسجيل الدخول فقط.\n"
    "🧹 تُحذف رسالة كلمة المرور بعد استلامها.\n\n"
    "⚠️ تمر كلمة المرور عبر خادم البوت أثناء تسجيل الدخول؛ لا تستخدم الخدمة "
    "إلا إذا كنت تثق بمشغّلها."
)


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_html(HELP_TEXT, reply_markup=return_menu_kb())

async def on_privacy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_html(PRIVACY_TEXT, reply_markup=return_menu_kb())

async def on_help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP_TEXT, reply_markup=return_menu_kb())


async def _announce_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE, code, code_type):
    """رسالة الفرحة للطالب بعد تفعيل كود (مميزات الاشتراك) + إشعار المالك."""
    user = update.effective_user
    uid = user.id
    label = db.CODE_TYPES[code_type]
    ent = db.get_entitlement(uid)
    await update.message.reply_html(
        st.redeem_success_text(code_type, ent, fmt_ts),
        reply_markup=after_kb(),
    )
    if OWNER_CHAT_ID:
        username = f"@{user.username}" if user.username else "بدون يوزر"
        mention = f'<a href="tg://user?id={uid}">{html.escape(user.full_name)}</a>'
        try:
            await ctx.application.bot.send_message(
                OWNER_CHAT_ID,
                "🎟️ <b>تم تفعيل كود اشتراك</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 المستخدم: {mention}\n"
                f"🔗 اليوزر: {html.escape(username)}\n"
                f"🆔 Telegram ID: <code>{uid}</code>\n"
                f"📦 النوع: <b>{html.escape(label)}</b>\n"
                f"🔑 الكود: <code>{html.escape(code.strip().upper())}</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc:
            log.warning("code activation notify failed (%s)", type(exc).__name__)


async def on_redeem_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = " ".join(ctx.args).strip()
    if not code:
        await update.message.reply_html(
            "استخدم الأمر هكذا:\n<code>/redeem VIP-XXXXXXXX</code>",
        )
        return
    uid = update.effective_user.id
    result = db.redeem_code(code, uid, update.effective_user.full_name)
    if not result["ok"]:
        await update.message.reply_text("الكود مستخدم أو غير صالح.")
        return
    await _announce_redeem(update, ctx, code, result["code_type"])


async def _answer_once(q, *args, **kwargs):
    """q.answer بدون ما يوقع المعالج لو الزرار اتردّ عليه قبل كده (تيليجرام بيرفض الرد التاني)."""
    try:
        await q.answer(*args, **kwargs)
    except TelegramError:
        pass


async def on_admin_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await _answer_once(q)
    channels = db.get_required_channels()
    enabled = db.get_setting("required_channels_enabled", "1") == "1"
    message = db.get_setting(
        "required_join_message",
        "لازم تشترك في القنوات المطلوبة عشان تستخدم البوت. اشترك، بعدين اضغط «تحقق من الاشتراك».",
    )
    lines = ["📢 <b>اشتراك القنوات الإجباري</b>\n"]
    lines.append("الحالة: " + ("<b>مفعّل</b>" if enabled else "<b>موقّف</b>"))
    if channels:
        lines.append("")
        for channel in channels:
            lines.append(
                f"• {html.escape(channel['title'])}\n"
                f"  <code>{html.escape(channel['chat_id'])}</code>"
            )
    else:
        lines.append("\nمفيش قنوات مطلوبة حالياً.")
    lines.append("\n<b>رسالة المستخدم:</b>\n" + html.escape(message))
    kb_rows = [
        [btn("➕ إضافة قناة", "success", callback_data="admin_channel_add"),
         btn("✏️ تعديل الرسالة", "primary", callback_data="admin_channel_message")],
        [btn("🟢 تفعيل الاشتراك" if not enabled else "🔴 إيقاف الاشتراك",
             "success" if not enabled else "danger",
             callback_data="admin_channel_toggle")],
    ]
    for channel in channels:
        kb_rows.append([
            btn(
                "🗑️ " + channel["title"][:24],
                "danger",
                callback_data="admin_channel_remove_" + channel["chat_id"].replace(" ", "_"),
            )
        ])
    kb_rows.append([btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")])
    kb = InlineKeyboardMarkup(kb_rows)
    await q.message.reply_html("\n".join(lines), reply_markup=kb)


async def on_admin_channel_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    enabled = db.get_setting("required_channels_enabled", "1") == "1"
    db.set_setting("required_channels_enabled", "0" if enabled else "1")
    reset_sub_cache()
    await q.answer("تم إيقاف الاشتراك الإجباري" if enabled else "تم تفعيل الاشتراك الإجباري")
    q.data = "admin_channels"
    await on_admin_channels(update, ctx)


async def on_admin_channel_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    ctx.user_data["state"] = "await_channel_add"
    await q.message.reply_text(
        "أرسل يوزر القناة فقط، بهذا الشكل:\n"
        "<code>@channel_username</code>\n\n"
        "البوت لازم يكون مشرفًا في القناة."
    )


async def on_admin_channel_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    ctx.user_data["state"] = "await_channel_message"
    await q.message.reply_text("أرسل نص رسالة الاشتراك الجديدة للمستخدمين.")


async def on_admin_channel_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    chat_id = q.data[len("admin_channel_remove_"):].strip()
    db.remove_required_channel(chat_id)
    reset_sub_cache()
    await _answer_once(q, "تم حذف القناة")
    q.data = "admin_channels"
    await on_admin_channels(update, ctx)


def return_menu_kb():
    return InlineKeyboardMarkup(
        [[btn("🏠 القائمة الرئيسية", "primary", callback_data="menu")]]
    )

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    error = ctx.error
    if isinstance(error, BadRequest) and "query is too old" in str(error).casefold():
        log.info("ignored expired Telegram callback query")
        return
    log.error("unhandled error", exc_info=error)

async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await send_welcome(q.message.chat, q.from_user.first_name or "يا غالي")


# ═══════════════════════════════════════════════════════
#  📈 الإحصائيات
# ═══════════════════════════════════════════════════════

def _build_mystats_text(uid):
    s = db.get_user_stats(uid)
    ent = db.get_entitlement(uid)
    if uid == OWNER_CHAT_ID:
        entitlement_line = "\n\n👑 <b>اشتراكك:</b> مالك البوت — محاولات غير محدودة"
    elif ent["unlimited_until"] > int(time.time()):
        entitlement_line = f"\n\n👑 <b>اشتراك VIP مميز:</b> حتى {fmt_ts(ent['unlimited_until'])}"
    else:
        entitlement_line = (
            f"\n\n🎟️ <b>محاولات عادية:</b> {ar_num(ent['attempts_remaining'])}"
            f"\n💎 <b>محاولات VIP عادي:</b> {ar_num(ent['vip_attempts_remaining'])}"
        )
        vip = db.get_vip_status(uid)
        if vip and vip["packs"]:
            entitlement_line += (
                f" <i>(استخدمت {ar_num(vip['used'])} من {ar_num(vip['granted'])})</i>"
            )
    if s["sessions"] == 0:
        return (
            "📈 <b>ملخّص إنجازك</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "لسه مفيش مهام مسجلة.\n"
            "اختار منصتك وابدأ أول خطوة! 🚀"
            + entitlement_line
        )

    success_rate = (
        int(s["finished"] * 100 / s["sessions"]) if s["sessions"] else 0
    )
    bar_sessions = min(s["sessions"], 10)
    sessions_bar = "🟩" * bar_sessions + "⬜" * (10 - bar_sessions)

    return (
        "📈 <b>ملخّص إنجازك</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>{html.escape(s['name'] or 'مجهول')}</b>\n"
        f"📅 أول جلسة: <i>{fmt_ts(s['first_seen'])}</i>\n"
        f"🕐 آخر جلسة: <i>{fmt_ts(s['last_run'])}</i>\n\n"
        "📚 <b>نشاطك</b>\n"
        f"🔁 الجلسات: <b>{ar_num(s['sessions'])}</b>\n"
        f"{sessions_bar}\n"
        f"✅ مكتملة: <b>{ar_num(s['finished'])}</b>  "
        f"({ar_num(success_rate)}٪)\n\n"
        f"📝 إجابات: <b>{ar_num(s['total_q'])}</b>  •  "
        f"✅ أنشطة: <b>{ar_num(s['total_act'])}</b>\n\n"
        "⏱️ <b>الوقت</b>\n"
        f"الإجمالي: <b>{format_duration(s['total_sec'])}</b>\n"
        f"أطول جلسة: <b>{format_duration(s['longest_sec'])}</b>\n\n"
        "🤍 <i>كل خطوة صغيرة تقرّبك من هدفك.</i>"
        + entitlement_line
    )


async def on_mystats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = _build_mystats_text(uid)
    kb   = InlineKeyboardMarkup([
        [btn("🚀 الثقافة المالية", "success", callback_data="solve_financial")],
        [btn("🐍 Qureo Python", "success", callback_data="solve_qureo")],
        [btn("🟨 Qureo JavaScript", "success", callback_data="solve_javascript")],
        [btn("🏠 القائمة",   "primary", callback_data="menu")],
    ])
    await update.message.reply_html(text, reply_markup=kb)


async def on_mystats_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    text = _build_mystats_text(uid)
    kb   = InlineKeyboardMarkup([
        [btn("🚀 الثقافة المالية", "success", callback_data="solve_financial")],
        [btn("🐍 Qureo Python", "success", callback_data="solve_qureo")],
        [btn("🟨 Qureo JavaScript", "success", callback_data="solve_javascript")],
        [btn("🏠 القائمة",   "primary", callback_data="menu")],
    ])
    await q.message.reply_html(text, reply_markup=kb)


async def on_stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """أوامر أدمن فقط — /stats."""
    if not _is_admin(update.effective_user.id):
        return
    await _send_admin_dashboard(update.message, ctx)


_GEN_ALIASES = {
    "single": "single", "try": "single", "1": "single",
    "vip": "vip", "10": "vip",
    "unlimited": "vip_unlimited", "max": "vip_unlimited", "vip_unlimited": "vip_unlimited",
}


async def on_gencodes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/gencodes <single|vip|unlimited> <العدد>  — توليد سريع بكميات كبيرة (لحد ٥٠٬٠٠٠ كود)."""
    if not _is_admin(update.effective_user.id):
        return
    args = ctx.args or []
    code_type = _GEN_ALIASES.get((args[0] if args else "").lower())
    if not code_type or len(args) < 2 or not args[1].isdigit():
        await update.message.reply_html(
            "🎟️ <b>توليد الأكواد بكميات</b>\n"
            "<code>/gencodes single 500</code> — محاولة واحدة\n"
            "<code>/gencodes vip 100</code> — VIP عادي (١٠ محاولات)\n"
            "<code>/gencodes unlimited 20</code> — VIP مميز (أسبوع)\n"
            f"الحد الأقصى {ar_num(db.MAX_BULK_CODES)} كود في الأمر الواحد."
        )
        return
    count = max(1, min(int(args[1]), db.MAX_BULK_CODES))
    started = time.monotonic()
    codes = await asyncio.to_thread(db.generate_codes_bulk, code_type, count)
    took = time.monotonic() - started
    label = db.CODE_TYPES[code_type]
    if len(codes) <= 20:
        body = "\n".join(f"<code>{html.escape(c)}</code>" for c in codes)
        await update.message.reply_html(f"✅ <b>{html.escape(label)}</b> — {ar_num(len(codes))} كود\n{body}")
        return
    payload = "\n".join(codes).encode("utf-8")
    await update.message.reply_document(
        document=payload,
        filename=f"codes_{code_type}_{len(codes)}.txt",
        caption=(
            f"✅ تم توليد {ar_num(len(codes))} كود ({label}) في {took:.1f} ثانية.\n"
            "كل سطر كود واحد."
        ),
    )


async def on_maintenance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """أمر أدمن لتبديل وضع الصيانة: /maintenance"""
    if not _is_admin(update.effective_user.id):
        return
    app = ctx.application
    app.bot_data["maintenance"] = not app.bot_data.get("maintenance", False)
    db.set_setting("maintenance", "1" if app.bot_data["maintenance"] else "0")
    await update.message.reply_html(
        "🟡 <b>تم تفعيل وضع الصيانة.</b>\nالمستخدمون لن يستطيعوا بدء حل جديد."
        if app.bot_data["maintenance"]
        else "🟢 <b>تم إيقاف وضع الصيانة.</b>\nالبوت يعمل بشكل طبيعي.",
    )


def _is_admin(user_id):
    return bool(OWNER_CHAT_ID) and user_id == OWNER_CHAT_ID


def _admin_dashboard_kb():
    return InlineKeyboardMarkup([
        [
            btn("👥 المستخدمين", "primary", callback_data="admin_users"),
            btn("🔴 الجارية", "danger", callback_data="admin_active"),
        ],
        [
            btn("🎟️ إدارة الأكواد", "success", callback_data="admin_codes"),
            btn("💎 مشتركو VIP", "success", callback_data="admin_vips"),
        ],
        [btn("📢 اشتراك القنوات الإجباري", "danger", callback_data="admin_channels")],
        [
            btn("📢 بث الرسالة", "primary", callback_data="admin_broadcast_start"),
            btn("🔄 تحديث", "success", callback_data="admin_stats"),
        ],
        [
            btn("🔎 بحث عن مستخدم", "primary", callback_data="admin_search"),
            btn("🧾 آخر المهام", "primary", callback_data="admin_jobs"),
        ],
        [btn("📥 تصدير المستخدمين", "primary", callback_data="admin_export")],
        [btn("🩺 حالة النظام", "primary", callback_data="admin_health")],
    ])


async def on_redeem_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["state"] = "await_redeem"
    await q.message.reply_html(
        "🎟️ <b>تفعيل كود اشتراك</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ابعت الكود كاملًا من غير مسافات.\n"
        "<code>مثال: VIP-XXXXXXXX</code>",
        reply_markup=cancel_kb(),
    )


async def on_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    me = await ctx.application.bot.get_me()
    if not me.username:
        log.error("cannot create invite link: bot has no username")
        await q.message.reply_text("مش قادر أجهز رابط الدعوة دلوقتي. حاول تاني بعد شوية.")
        return
    link = invite_link(me.username, uid)
    prog = db.get_referral_progress(uid)
    if prog["capped"]:
        status_line = (
            f"✅ اخدت الحد الأقصى: <b>{ar_num(prog['granted'])}</b> محاولات مجانية من الدعوات.\n"
            "💎 لو عايز أكتر شوف الاشتراكات: /plans"
        )
    else:
        status_line = (
            f"📊 دعوتك لحد دلوقتي: <b>{ar_num(prog['count'])}</b> • "
            f"فاضل <b>{ar_num(prog['remaining'])}</b> وتاخد محاولة"
        )
    await q.message.reply_html(
        "👥 <b>نظام الدعوات</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "شارك الرابط ده مع أصحابك. لما يضغطوا عليه هيفتح البوت مباشرة، "
        "وبعدها يضغطوا «ابدأ» لتسجيل الدعوة:\n"
        f'<a href="{html.escape(link, quote=True)}">{html.escape(link)}</a>\n\n'
        "🎁 <b>٥ دعوات = محاولة مجانية</b>\n"
        "🎁 <b>١٠ دعوات = محاولتين</b> (وده الحد الأقصى للمحاولات المجانية من الدعوات)\n\n"
        f"{status_line}",
        reply_markup=InlineKeyboardMarkup([
            [btn("🚀 فتح البوت برابط الدعوة", url=link)],
            [btn("🏠 القائمة الرئيسية", "primary", callback_data="menu")],
        ]),
    )


async def on_admin_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    kb = InlineKeyboardMarkup([
        [btn("🎟️ محاولة واحدة — ٤٠ ج", "success", callback_data="admin_codes_single")],
        [btn("💎 VIP ١٠ محاولات — ٨٠ ج", "primary", callback_data="admin_codes_vip")],
        [btn("👑 VIP مميز أسبوع — ١٧٠ ج", "danger", callback_data="admin_codes_unlimited")],
        [btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")],
    ])
    await q.message.reply_html(
        "🎟️ <b>إدارة الأكواد المخصصة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "اختار قسم الكود عشان تولّد كود أو تعرض الأكواد غير المفعلة والمفعلة.",
        reply_markup=kb,
    )


async def on_admin_codes_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    code_type = q.data.rsplit("_", 1)[1]
    if code_type == "unlimited":
        code_type = "vip_unlimited"
    label = db.CODE_TYPES.get(code_type, code_type)
    rows = db.get_codes(code_type, 20)
    unused = [r for r in rows if r["status"] == "unused"]
    used = [r for r in rows if r["status"] == "used"]
    lines = [
        f"🎟️ <b>{html.escape(label)}</b>",
        f"متاح: <b>{ar_num(len(unused))}</b>  •  مُستخدم: <b>{ar_num(len(used))}</b>\n",
    ]
    for r in unused[:10]:
        lines.append(f"🟢 <code>{html.escape(r['code'])}</code>")
    for r in used[:10]:
        name = html.escape(r["used_name"] or "مجهول")
        lines.append(f"🔴 <code>{html.escape(r['code'])}</code> — {name} (<code>{r['used_by']}</code>)")
    kb = InlineKeyboardMarkup([
        [btn("➕ توليد كود جديد", "success", callback_data=f"admin_code_generate_{code_type}")],
        [btn("⬅️ أقسام الأكواد", "primary", callback_data="admin_codes")],
    ])
    await q.message.reply_html("\n".join(lines), reply_markup=kb)


async def on_admin_code_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    code_type = q.data[len("admin_code_generate_"):].strip()
    if code_type == "unlimited":
        code_type = "vip_unlimited"
    code = db.generate_code(code_type)
    await q.answer("تم توليد الكود")
    await q.message.reply_html(
        f"✅ <b>تم توليد كود {html.escape(db.CODE_TYPES[code_type])}</b>\n"
        f"<code>{html.escape(code)}</code>",
        reply_markup=InlineKeyboardMarkup([
            [btn("📋 عرض القسم", "primary", callback_data="admin_codes_unlimited" if code_type == "vip_unlimited" else f"admin_codes_{code_type}")],
            [btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")],
        ]),
    )


def _fmt_rate(rate):
    return "—" if rate is None else f"{ar_num(rate)}٪"


async def _send_admin_dashboard(target, ctx):
    g   = db.get_global_stats()
    top = f"🥇 أكثر مستخدم: <b>{html.escape(g['top_user'][0])}</b> ({ar_num(g['top_user'][1])} سؤال)" if g["top_user"] else ""
    running_uids, waiting_uids = job_groups(ctx.application.bot_data)
    running, waiting = len(running_uids), len(waiting_uids)
    maintenance = ctx.application.bot_data.get("maintenance", False)
    text = (
        "╭────── <b>لوحة تحكم البوت</b> ──────╮\n"
        f"│ {'🟡 وضع الصيانة' if maintenance else '🟢 البوت يعمل'}  •  "
        f"{'🔒 الطابور مقفول' if queue_closed(ctx.application) else '🔓 الطابور مفتوح'}\n"
        "╰──────────────────────────────╯\n\n"
        f"👥 إجمالي المستخدمين: <b>{ar_num(g['total_users'])}</b>\n"
        f"🆕 انضموا النهارده: <b>{ar_num(g['joined_today'])}</b>\n"
        f"🔥 نشطون اليوم:       <b>{ar_num(g['active_today'])}</b>\n"
        f"🔁 إجمالي الجلسات:    <b>{ar_num(g['total_jobs'])}</b>\n"
        f"📝 إجمالي الأسئلة:    <b>{ar_num(g['total_questions'])}</b>\n"
        f"🤍 الصلوات على النبي: <b>{ar_num(g['salawat'])}</b>\n\n"
        f"📊 عمليات النهارده: <b>{ar_num(g['today_jobs'])}</b>\n"
        f"✅ ناجحة: <b>{ar_num(g['today_success'])}</b>  "
        f"❌ دخول خاطئ: <b>{ar_num(g['today_login_failed'])}</b>\n"
        f"⚠️ تعثرات البوت فقط: <b>{ar_num(g['today_bot_failures'])}</b>\n"
        f"🎯 نسبة النجاح اليوم: <b>{_fmt_rate(g['rate_today'])}</b>\n"
        f"📈 الأسبوع: <b>{_fmt_rate(g['rate_week'])}</b>  •  الإجمالي: <b>{_fmt_rate(g['rate_total'])}</b>\n"
        "<i>(النسبة من العمليات اللي البوت نفّذها فعلًا، من غير الدخول الخاطئ والإيقاف اليدوي)</i>\n\n"
        f"📅 آخر ٧ أيام: <b>{ar_num(g['weekly_jobs'])}</b> مهمة، "
        f"{ar_num(g['weekly_questions'])} سؤال\n"
        f"✅ مكتملة: <b>{ar_num(g['weekly_finished'])}</b>  •  "
        f"⚠️ تعثّرت: <b>{ar_num(g['weekly_issues'])}</b>\n\n"
        f"⚙️ شغّالة الآن: <b>{ar_num(running)}</b>  •  انتظار: <b>{ar_num(waiting)}</b>\n"
        f"{top or '🥇 لسه مفيش مستخدمين نشطين'}"
    )
    await target.reply_html(text, reply_markup=_admin_dashboard_kb())


VIP_PAGE_SIZE = 6


def _vip_page(status, page):
    """نص ولوحة أزرار صفحة من مشتركي VIP (الكل / النشطين / المنتهين)."""
    summary = db.get_vip_summary()
    subs = db.get_vip_subscribers(status)
    pages = max(1, -(-len(subs) // VIP_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = subs[page * VIP_PAGE_SIZE:(page + 1) * VIP_PAGE_SIZE]
    lines = [st.vip_summary_text(summary, status), ""]
    kb_rows = []
    if not chunk:
        lines.append("مفيش مشتركين في القسم ده.")
    for i, sub in enumerate(chunk, page * VIP_PAGE_SIZE + 1):
        lines.append(st.vip_row_text(i, sub, fmt_ts))
        lines.append("")
        kb_rows.append([btn(f"{i}. {(sub['name'] or 'مجهول')[:26]}", "primary", callback_data=f"admin_user_{sub['tg_id']}")])
    if pages > 1:
        lines.append(f"صفحة {ar_num(page + 1)} من {ar_num(pages)}")
    nav = []
    if page > 0:
        nav.append(btn("⬅️ السابق", "primary", callback_data=f"admin_vips_{status}_{page - 1}"))
    if page + 1 < pages:
        nav.append(btn("التالي ➡️", "primary", callback_data=f"admin_vips_{status}_{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([
        btn(("• " if status == key else "") + title, "success" if status == key else "primary",
            callback_data=f"admin_vips_{key}_0")
        for key, title in st.FILTER_TITLES.items()
    ])
    kb_rows.append([btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(kb_rows)


async def on_admin_vips(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """مشتركو VIP: كل واحد استخدم كام وفاضله كام ونوع اشتراكه — أدمن فقط."""
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    status, page = "all", 0
    match = re.match(r"^admin_vips_(all|active|done)_(\d+)$", q.data)
    if match:
        status, page = match.group(1), int(match.group(2))
    text, kb = _vip_page(status, page)
    if match:      # تنقّل/فلتر: نعدّل نفس الرسالة بدل ما نكرّر الرسائل
        try:
            await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except BadRequest:
            pass
    await q.message.reply_html(text, reply_markup=kb)


async def on_vips_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/vips [active|done] — قائمة مشتركي VIP (أدمن فقط)."""
    if not _is_admin(update.effective_user.id):
        return
    arg = (ctx.args[0].lower() if ctx.args else "all")
    status = {"active": "active", "done": "done", "all": "all"}.get(arg, "all")
    text, kb = _vip_page(status, 0)
    await update.message.reply_html(text, reply_markup=kb)


async def on_admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """قائمة آخر المستخدمين مع صفحات — أدمن فقط."""
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    page = 0
    if q.data.startswith("admin_users_page_"):
        try:
            page = max(0, int(q.data.rsplit("_", 1)[1]))
        except ValueError:
            page = 0
    limit = 8
    rows = db.get_recent_users(limit, page * limit)
    if not rows:
        await q.message.reply_text("مفيش مستخدمين في الصفحة دي.")
        return
    lines = [f"👥 <b>المستخدمين</b> — صفحة {ar_num(page + 1)}\n"]
    kb_rows = []
    for i, r in enumerate(rows, 1):
        name = html.escape(r.get("name") or "مجهول")
        uid  = r.get("user_id", "?")
        sess = r.get("sessions", 0)
        lines.append(f"{ar_num(i)}. {name} — <code>{uid}</code> — {ar_num(sess)} جلسة")
        kb_rows.append([btn(name[:30], "primary", callback_data=f"admin_user_{uid}")])
    nav = []
    if page:
        nav.append(btn("⬅️ السابق", "primary", callback_data=f"admin_users_page_{page - 1}"))
    if db.get_user_count() > (page + 1) * limit:
        nav.append(btn("التالي ➡️", "primary", callback_data=f"admin_users_page_{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")])
    await q.message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))


async def on_admin_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    try:
        uid = int(q.data.rsplit("_", 1)[1])
    except ValueError:
        return
    await _send_admin_user_detail(q.message, uid)


async def _send_admin_user_detail(message, uid):
    s = db.get_user_detail(uid)
    if not s["sessions"] and not s["name"]:
        await message.reply_text("المستخدم ده مش موجود.")
        return
    blocked = bool(s["blocked"])
    accounts = s["accounts"]
    text = (
        "👤 <b>تفاصيل المستخدم</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(s['name'] or 'مجهول')}</b>\n"
        f"🆔 <code>{uid}</code>\n"
        f"📅 انضم: <i>{fmt_ts(s['first_seen'])}</i>\n"
        f"🕐 آخر ظهور: <i>{fmt_ts(s['last_run'])}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔁 الجلسات: <b>{ar_num(s['sessions'])}</b>\n"
        f"✅ المكتملة: <b>{ar_num(s['finished'])}</b>\n"
        f"📝 الأسئلة: <b>{ar_num(s['total_q'])}</b>\n"
        f"📚 الأنشطة: <b>{ar_num(s['total_act'])}</b>\n"
        f"⏱️ وقت الحل: <b>{format_duration(s['total_sec'])}</b>\n"
    )
    if accounts:
        text += "\n🔑 <b>أكواد المنصات:</b>\n"
        for platform, account in accounts.items():
            text += f"• {html.escape(platform)}: <code>{html.escape(account)}</code>\n"
    vip = db.get_vip_status(uid)
    if vip:
        text += "\n💎 <b>الاشتراك:</b>\n" + "\n".join(st.vip_status_lines(vip, fmt_ts)) + "\n"
    text += "\n🚫 الحالة: " + ("<b>محظور</b>" if blocked else "<b>نشط</b>")
    kb = InlineKeyboardMarkup([
        [btn("🔓 إلغاء الحظر" if blocked else "🔒 حظر المستخدم",
             "danger" if not blocked else "success",
             callback_data=f"admin_block_{uid}")],
        [btn("⬅️ رجوع للمستخدمين", "primary", callback_data="admin_users")],
    ])
    await message.reply_html(text, reply_markup=kb)


async def on_admin_toggle_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    try:
        uid = int(q.data.rsplit("_", 1)[1])
    except ValueError:
        await q.answer()
        return
    blocked = db.is_user_blocked(uid)
    db.set_user_blocked(uid, not blocked)
    if not blocked:
        cancel = ctx.application.bot_data.get("cancels", {}).get(uid)
        if cancel:
            cancel.set()
    await q.answer("تم إلغاء الحظر" if blocked else "تم حظر المستخدم")
    q.data = f"admin_user_{uid}"
    await _send_admin_user_detail(q.message, uid)


async def on_admin_search_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    ctx.user_data["state"] = "await_admin_search"
    await q.message.reply_text("ابعت Telegram ID أو جزء من اسم المستخدم للبحث. /cancel للإلغاء.")


async def on_user_lookup_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("الاستخدام: /user <Telegram ID أو الاسم>")
        return
    await _show_admin_user_results(update.message, query)


async def _show_admin_user_results(message, query):
    rows = db.search_users(query)
    if not rows:
        await message.reply_text("ملقتش مستخدم مطابق.")
        return
    lines = ["🔎 <b>نتائج البحث:</b>"]
    buttons = []
    for row in rows:
        name = html.escape(row["name"] or "مستخدم")
        lines.append(
            f"• {name} — <code>{row['user_id']}</code> — "
            f"{ar_num(row['sessions'])} جلسة — {ar_num(row['questions'])} سؤال"
        )
        buttons.append([btn(name[:30], "primary", callback_data=f"admin_user_{row['user_id']}")])
    await message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_admin_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    rows = db.get_recent_jobs(10)
    if not rows:
        await q.message.reply_text("مفيش مهام مسجلة لحد دلوقتي.")
        return
    status_icons = {
        "finished": "✅", "stopped": "⛔", "timeout": "⌛",
        "login_failed": "🔐", "stuck": "🧩", "error": "⚠️",
        "tests_pending": "📝",
    }
    lines = ["🧾 <b>آخر ١٠ مهام</b>\n"]
    for row in rows:
        name = html.escape(row["name"] or "مستخدم")
        status = row["status"]
        lines.append(
            f"{status_icons.get(status, '❔')} {name} (<code>{row['user_id']}</code>) — "
            f"{html.escape(status)} — {ar_num(row['questions'])} سؤال — "
            f"{fmt_ts(row['finished'])}"
        )
        if row["note"]:
            lines.append(f"   <i>{html.escape(row['note'][:120])}</i>")
    await q.message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [[btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")]]
        ),
    )


async def on_admin_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer("جاري تجهيز الملف…")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["telegram_id", "name", "first_seen", "last_seen", "sessions", "questions"])
    offset = 0
    page_size = 500
    while True:
        rows = db.get_recent_users(page_size, offset)
        if not rows:
            break
        for row in rows:
            writer.writerow([
                row["user_id"], row["name"], row["first_seen"], row["last_seen"],
                row["sessions"], row["questions"],
            ])
        offset += len(rows)
        if len(rows) < page_size:
            break
    document = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    document.name = "bot-users.csv"
    await q.message.reply_document(
        document=document,
        caption=f"📥 تصدير المستخدمين: {ar_num(db.get_user_count())} مستخدم",
    )


async def on_admin_active(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """الحلول الجارية الآن — أدمن فقط."""
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    cancels = ctx.application.bot_data.get("cancels", {})
    if not cancels:
        await q.message.reply_text("🟢 مفيش حلول جارية دلوقتي.")
        return
    running_uids, waiting_uids = job_groups(ctx.application.bot_data)
    lines = [
        f"🔴 <b>حلول مسجّلة: {ar_num(len(cancels))}</b>  "
        f"(⚙️ شغّالة {ar_num(len(running_uids))} • ⏳ منتظرة {ar_num(len(waiting_uids))})\n"
    ]
    for title, uids in (("⚙️ <b>شغّالة الآن</b>", running_uids), ("⏳ <b>في الطابور</b>", waiting_uids)):
        if not uids:
            continue
        lines.append(f"{title} ({ar_num(len(uids))}):")
        for uid in uids:
            s = db.get_user_stats(uid)
            name = html.escape(s.get("name") or "مجهول")
            lines.append(f"• {name} — <code>{uid}</code>")
        lines.append("")
    kb = InlineKeyboardMarkup(
        [[btn(f"⛔ إيقاف مهمة {str(uid)[:15]}", "danger", callback_data=f"admin_stop_{uid}")]
         for uid in running_uids + waiting_uids]
        + [[btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")]]
    )
    await q.message.reply_html("\n".join(lines), reply_markup=kb)


async def on_admin_stop_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    try:
        uid = int(q.data.rsplit("_", 1)[1])
    except ValueError:
        await q.answer("معرّف غير صالح", show_alert=True)
        return
    cancel = ctx.application.bot_data.get("cancels", {}).get(uid)
    if cancel is None:
        await q.answer("المهمة انتهت بالفعل", show_alert=True)
        return
    cancel.set()
    await q.answer("تم طلب إيقاف المهمة")


async def on_admin_stats_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer("تم تحديث الإحصائيات")
    await _send_admin_dashboard(q.message, ctx)


async def on_admin_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    await _send_admin_health(q.message, ctx)


async def _send_admin_health(message, ctx):
    app = ctx.application
    running_uids, waiting_uids = job_groups(app.bot_data)
    maintenance = app.bot_data.get("maintenance", False)
    closed = queue_closed(app)
    text = (
        "🩺 <b>حالة النظام</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 وضع التشغيل: <b>{'صيانة' if maintenance else 'عادي'}</b>\n"
        f"🚦 الطابور: <b>{'🔒 مقفول' if closed else '🔓 مفتوح'}</b>\n"
        f"🔴 وظائف شغّالة: <b>{ar_num(len(running_uids))}</b>\n"
        f"⏳ طلبات منتظرة: <b>{ar_num(len(waiting_uids))}</b>\n"
        f"⚙️ الحد المتزامن: <b>{ar_num(MAX_JOBS)}</b>\n"
        f"📦 سعة الطابور: <b>{ar_num(MAX_QUEUE)}</b>\n"
        f"⏱️ مهلة المهمة: <b>{ar_num(JOB_TIMEOUT_MIN)} دقيقة</b>\n"
        f"💾 قاعدة البيانات: <code>{html.escape(os.getenv('DB_PATH', str(BASE / 'bot.db')))}</code>"
    )
    kb = InlineKeyboardMarkup([
        [btn("🔓 فتح الطابور" if closed else "🔒 قفل الطابور",
             "success" if closed else "danger",
             callback_data="admin_queue_toggle")],
        [btn("🟡 تشغيل وضع الصيانة" if not maintenance else "🟢 إيقاف وضع الصيانة",
             "danger" if not maintenance else "success",
             callback_data="admin_maintenance_toggle")],
        [btn("🔄 تحديث", "primary", callback_data="admin_health"),
         btn("🏠 لوحة الأدمن", "success", callback_data="admin_stats")],
    ])
    await message.reply_html(text, reply_markup=kb)


def _set_queue_closed(app, closed):
    app.bot_data["queue_closed"] = bool(closed)
    db.set_setting("queue_closed", "1" if closed else "0")


async def on_admin_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    app = ctx.application
    _set_queue_closed(app, not queue_closed(app))
    await q.answer("🔒 الطابور اتقفل" if queue_closed(app) else "🔓 الطابور اتفتح")
    await _send_admin_health(q.message, ctx)


async def on_queue_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """أمر أدمن: /queue  (تبديل)  أو  /queue close  أو  /queue open"""
    if not _is_admin(update.effective_user.id):
        return
    app = ctx.application
    arg = (ctx.args[0].lower() if getattr(ctx, "args", None) else "")
    if arg in ("close", "off", "قفل"):
        _set_queue_closed(app, True)
    elif arg in ("open", "on", "فتح"):
        _set_queue_closed(app, False)
    else:
        _set_queue_closed(app, not queue_closed(app))
    await update.effective_message.reply_html(
        "🔒 <b>الطابور اتقفل.</b> المستخدمين العاديين مش هيقدروا يبدأوا حل جديد "
        "(الحلول الشغّالة هتكمّل، والمشتركين بيتحل لهم فورًا)."
        if queue_closed(app) else
        "🔓 <b>الطابور اتفتح.</b> البوت بيستقبل طلبات حل جديدة."
    )


async def on_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_html(subscription_text(), reply_markup=subscription_kb())


async def on_plans_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(subscription_text(), reply_markup=subscription_kb())


async def on_admin_maintenance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    app = ctx.application
    app.bot_data["maintenance"] = not app.bot_data.get("maintenance", False)
    db.set_setting("maintenance", "1" if app.bot_data["maintenance"] else "0")
    await q.answer("تم تفعيل وضع الصيانة" if app.bot_data["maintenance"] else "تم إيقاف وضع الصيانة")
    q.data = "admin_health"
    await _send_admin_health(q.message, ctx)


async def on_admin_broadcast_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """بداية البث — أدمن فقط."""
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    await q.answer()
    ctx.user_data["state"] = "await_broadcast"
    await q.message.reply_text(
        "📢 ابعت الرسالة اللي عايز تبثها لكل المستخدمين.\n"
        "ابعت /cancel لو غيّرت رأيك."
    )


async def on_broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/broadcast نص — أدمن فقط."""
    if not _is_admin(update.effective_user.id):
        return
    text = (update.message.text or "").partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("ابعت النص بعد /broadcast")
        return
    await _stage_broadcast(update.message, ctx, text)


async def _stage_broadcast(reply_target, ctx, text):
    text = text.strip()
    if not text:
        await reply_target.reply_text("نص الرسالة فارغ.")
        return
    if len(text) > 4000:
        await reply_target.reply_text("الحد الأقصى للرسالة ٤٠٠٠ حرف. قصّر النص وحاول تاني.")
        return
    nonce = secrets.token_hex(6)
    ctx.user_data["broadcast_draft"] = {"text": text, "nonce": nonce}
    count = db.get_user_count()
    preview = text if len(text) <= 1500 else text[:1500] + "…"
    await reply_target.reply_text(
        f"👁️ معاينة البث إلى {ar_num(count)} مستخدم:\n\n{preview}\n\nتأكيد الإرسال؟",
        reply_markup=InlineKeyboardMarkup([
            [btn("✅ تأكيد البث", "success", callback_data=f"admin_broadcast_confirm_{nonce}"),
             btn("❌ إلغاء", "danger", callback_data=f"admin_broadcast_cancel_{nonce}")]
        ]),
    )


async def on_admin_broadcast_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    draft = ctx.user_data.get("broadcast_draft")
    nonce = q.data.rsplit("_", 1)[-1]
    if not draft or draft["nonce"] != nonce:
        await q.answer("انتهت صلاحية المعاينة. ابدأ بثًا جديدًا.", show_alert=True)
        return
    ctx.user_data.pop("broadcast_draft", None)
    await q.answer("بدأ الإرسال")
    await q.message.reply_text("📢 جاري إرسال الرسالة…")
    await _do_broadcast(q.message, ctx, draft["text"])


async def on_admin_broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user.id):
        await q.answer("مش أدمن!", show_alert=True)
        return
    draft = ctx.user_data.get("broadcast_draft")
    nonce = q.data.rsplit("_", 1)[-1]
    if draft and draft["nonce"] == nonce:
        ctx.user_data.pop("broadcast_draft", None)
    ctx.user_data.pop("state", None)
    await q.answer("تم إلغاء البث")
    await q.message.reply_text("تم إلغاء البث، لم يتم إرسال أي رسالة.")


async def _do_broadcast(reply_target, ctx, text: str):
    """يبعت رسالة لكل المستخدمين."""
    all_users = db.get_all_user_ids()
    sent = failed = 0
    for uid in all_users:
        try:
            await ctx.application.bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramError:
            failed += 1
    await reply_target.reply_text(
        f"📢 تم الإرسال!\n✅ نجح: {ar_num(sent)}\n❌ فشل: {ar_num(failed)}"
    )


# ═══════════════════════════════════════════════════════
#  الصلاة على النبي ﷺ
# ═══════════════════════════════════════════════════════

async def on_salat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    now = time.monotonic()
    if now - ctx.user_data.get("last_salat", 0) < 0.35:
        await q.answer()
        return
    ctx.user_data["last_salat"] = now
    total = db.add_salawat()
    await q.answer("اللهم صلِّ وسلِّم وبارك على سيدنا محمد ﷺ")
    try:
        await q.edit_message_reply_markup(main_menu(total))
    except (BadRequest, RetryAfter):
        pass


# ═══════════════════════════════════════════════════════
#  تدفق الدخول
# ═══════════════════════════════════════════════════════

async def on_solve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    if not await ensure_subscribed(ctx.application, q.message.chat, uid):
        return
    if db.is_user_blocked(uid):
        await q.message.reply_text("🚫 تم حظر حسابك من استخدام البوت.")
        return
    if ctx.application.bot_data.get("maintenance", False) and uid != OWNER_CHAT_ID:
        await q.message.reply_text("🟡 البوت في وضع الصيانة دلوقتي. جرّب تاني بعد شوية.")
        return
    db.touch_user(uid, q.from_user.full_name, q.from_user.username or "")
    if uid in ctx.application.bot_data["cancels"]:
        await q.message.reply_text("⏳ في حل شغّال لحسابك دلوقتي. استنى لما يخلص أو اضغط «إيقاف».")
        return
    if uid != OWNER_CHAT_ID:
        if not db.has_attempt(uid):
            await q.message.reply_html(
                subscription_text("🚫 <b>خلصت محاولاتك.</b>"),
                reply_markup=subscription_kb(),
            )
            return
    if not is_vip_user(uid):
        if queue_closed(ctx.application):
            await q.message.reply_html(QUEUE_CLOSED_TEXT, reply_markup=subscription_kb())
            return
    if queue_full(ctx.application) and not is_vip_user(uid):
        await q.message.reply_text(QUEUE_FULL_TEXT)
        return
    if q.data == "solve_qureo":
        ctx.user_data["platform"] = "qureo"
    elif q.data == "solve_javascript":
        ctx.user_data["platform"] = "javascript"
    else:
        ctx.user_data["platform"] = "financial"
    # نبدأ مباشرة بدون موافقة على التقرير
    await continue_solve_flow(q.message, ctx)


async def continue_solve_flow(message, ctx):
    uid   = message.chat.id
    platform = ctx.user_data.get("platform", "financial")
    saved = db.get_username(uid, platform)
    if saved:
        kb = InlineKeyboardMarkup([
            [btn("✅ استخدم الكود ده", "success", callback_data="use_saved")],
            [btn("✏️ كود تاني",        "primary", callback_data="new_user")],
        ])
        await message.reply_html(
            f"عندي كود محفوظ لحسابك:\n<code>{html.escape(saved)}</code>\n\nتستخدمه؟",
            reply_markup=kb,
        )
    else:
        await ask_username(message, ctx)


async def ask_username(message, ctx):
    ctx.user_data["state"] = "await_user"
    await message.reply_html(
        "🔐 <b>الخطوة ١ من ٢ | كود الدخول</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ابعت كود الدخول الخاص بحسابك على المنصة.\n"
        "<i>هتحفظه عشان ما تكتبوش في المرة الجاية.</i>",
        reply_markup=cancel_kb(),
    )


async def ask_password(message, ctx, username):
    ctx.user_data["state"]    = "await_pass"
    ctx.user_data["username"] = username
    await message.reply_html(
        "🔑 <b>الخطوة ٢ من ٢ | كلمة المرور</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الحساب: <code>{html.escape(username)}</code>\n\n"
        "ابعت كلمة المرور في الرسالة التالية.\n"
        "<i>هتتمسح الرسالة بعد استلامها، ومش بتتخزن.</i>",
        reply_markup=cancel_kb(),
    )


async def on_new_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await ask_username(q.message, ctx)


ACCOUNT_USED_TEXT = (
    "🚫 <b>الحساب ده استخدم المحاولة المجانية قبل كده</b> من حساب تيليجرام تاني.\n"
    "المحاولة المجانية بتتحسب مرة واحدة لكل حساب على المنصة.\n\n"
    "🎟️ لو عايز تحله فعّل كود اشتراك واحل بيه عادي."
)


OTHER_ACCOUNT_TEXT = (
    "🚫 <b>المحاولة المجانية متقفلة على أول حساب منصة دخلته.</b>\n"
    "مينفعش تحل حساب تاني بالمحاولة المجانية.\n\n"
    "🎟️ لو عايز تحل أي حساب تاني فعّل كود اشتراك واحل بيه عادي."
)


async def _reject_if_account_used(target, uid, platform, username, ctx):
    """يرفض الحل لو الحساب ده خد محاولته المجانية من أكونت تاني ومعاكش محاولة مشتراة بكود."""
    if uid == OWNER_CHAT_ID:
        return False
    verdict = db.check_account_access(uid, platform, username)
    if verdict["ok"] or verdict.get("reason") not in ("account_used", "other_account"):
        return False
    ctx.user_data.pop("state", None)
    send = getattr(target, "reply_text", None) or target.send_message
    await send(
        OTHER_ACCOUNT_TEXT if verdict.get("reason") == "other_account" else ACCOUNT_USED_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [btn("🎟️ تفعيل كود اشتراك", "success", callback_data="redeem_start")],
            [btn("💬 تواصل مع المطوّر", url=f"https://t.me/{DEV_USERNAME}")],
        ]),
    )
    return True


async def on_use_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    await q.answer()
    platform = ctx.user_data.get("platform", "financial")
    saved = db.get_username(q.from_user.id, platform)
    if not saved:
        await ask_username(q.message, ctx)
        return
    if await _reject_if_account_used(q.message, q.from_user.id, platform, saved, ctx):
        return
    await ask_password(q.message, ctx, saved)


async def on_cancel_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ctx.user_data.pop("state",          None)
    ctx.user_data.pop("username",       None)
    ctx.user_data.pop("report_consent", None)
    ctx.user_data.pop("platform", None)
    await q.answer("اتلغى")
    try:
        await q.message.delete()
    except TelegramError:
        pass


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("state")
    text  = (update.message.text or "").strip()
    uid   = update.effective_user.id
    if state == "await_channel_add":
        if not _is_admin(uid):
            ctx.user_data.pop("state", None)
            return
        ctx.user_data.pop("state", None)
        username = text.strip()
        if username.startswith("@"):
            username = username[1:]
        if (
            not username
            or not username.replace("_", "").isalnum()
            or username.isdigit()
            or "/" in text
            or ":" in text
        ):
            await update.message.reply_text(
                "أرسل يوزر القناة فقط، مثل: <code>@channel_username</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        raw_chat_id = f"@{username}"
        try:
            chat = await ctx.application.bot.get_chat(raw_chat_id)
            if getattr(chat, "type", "") not in ("channel", "supergroup"):
                await update.message.reply_text("اليوزر هذا ليس لقناة تيليجرام.")
                return

            me = await ctx.application.bot.get_me()
            bot_member = await ctx.application.bot.get_chat_member(chat.id, me.id)
            if bot_member.status not in ("administrator", "creator"):
                await update.message.reply_text(
                    "البوت مش مشرف في القناة. اجعله أدمن ثم أرسل اليوزر تاني."
                )
                return

            chat_id = str(chat.id)
            title = chat.title or raw_chat_id
            link = f"https://t.me/{username}"
            db.add_required_channel(chat_id, title, link)
            reset_sub_cache()
            await update.message.reply_html(
                f"✅ تم إضافة القناة:\n<b>{html.escape(title)}</b>\n"
                f"Chat ID: <code>{html.escape(chat_id)}</code>",
            )
        except TelegramError as exc:
            await update.message.reply_text(
                f"تعذر إضافة القناة. تأكد أن البوت عضو فيها وأن المعرّف صحيح.\n{type(exc).__name__}"
            )

    elif state == "await_channel_message":
        if not _is_admin(uid):
            ctx.user_data.pop("state", None)
            return
        ctx.user_data.pop("state", None)
        if len(text) > 4000:
            await update.message.reply_text("الحد الأقصى للرسالة ٤٠٠٠ حرف.")
            return
        db.set_setting("required_join_message", text)
        await update.message.reply_text("تم تعديل رسالة الاشتراك الإجباري.")

    elif state == "await_redeem":
        ctx.user_data.pop("state", None)
        result = db.redeem_code(text, uid, update.effective_user.full_name)
        if not result["ok"]:
            message = "الكود مستخدم أو غير صالح." if result["reason"] == "used" else "الكود غير موجود أو مكتوب غلط."
            await update.message.reply_text(message)
            return
        await _announce_redeem(update, ctx, text, result["code_type"])

    elif state == "await_broadcast":
        if not _is_admin(uid):
            ctx.user_data.pop("state", None)
            return
        ctx.user_data.pop("state", None)
        await _stage_broadcast(update.message, ctx, text)

    elif state == "await_admin_search":
        if not _is_admin(uid):
            ctx.user_data.pop("state", None)
            return
        ctx.user_data.pop("state", None)
        await _show_admin_user_results(update.message, text)

    elif state == "await_user":
        if not (2 <= len(text) <= 64) or any(c.isspace() for c in text):
            await update.message.reply_text("الكود شكله مش مظبوط (من غير مسافات). ابعته تاني.")
            return
        if await _reject_if_account_used(
            update.message, uid, ctx.user_data.get("platform", "financial"), text, ctx
        ):
            return
        db.save_username(uid, text, ctx.user_data.get("platform", "financial"))
        await ask_password(update.message, ctx, text)

    elif state == "await_pass":
        password = update.message.text or ""
        try:
            await update.message.delete()
        except TelegramError:
            pass
        platform = ctx.user_data.pop("platform", "financial")
        username = ctx.user_data.pop("username", None) or db.get_username(uid, platform)
        ctx.user_data.pop("state", None)
        if not username or not password.strip():
            await update.effective_chat.send_message("حصلت مشكلة، ابدأ من جديد بـ /start")
            return
        if await _reject_if_account_used(update.effective_chat, uid, platform, username, ctx):
            password = None
            return
        if db.is_user_blocked(uid):
            password = None
            await update.effective_chat.send_message("🚫 تم حظر حسابك من استخدام البوت.")
            return
        if ctx.application.bot_data.get("maintenance", False) and uid != OWNER_CHAT_ID:
            password = None
            await update.effective_chat.send_message("🟡 البوت دخل وضع الصيانة. جرّب تاني بعد شوية.")
            return
        if uid != OWNER_CHAT_ID:
            claim = db.claim_free_account(uid, platform, username)   # حجز الحساب للمجانية قبل بدء الحل
            if not claim["claimed"] and claim.get("reason"):
                if await _reject_if_account_used(update.effective_chat, uid, platform, username, ctx):
                    password = None
                    return
        ctx.application.create_task(
            run_student_job(
                ctx.application,
                update.effective_chat.id,
                uid,
                username,
                password,
                platform,
            ),
            update=update,
        )
        password = None

    else:
        await update.message.reply_text("اكتب /start عشان تفتح القائمة 🌿")


# ═══════════════════════════════════════════════════════
#  🎯 شريط التقدم المحسّن
# ═══════════════════════════════════════════════════════

# أيقونات المراحل — تتغير مع تقدم العمل
_PHASE_ICONS = {
    "بيسجّل":    "🔑",
    "يسجّل":     "🔑",
    "login":     "🔑",
    "الدخول":    "🔑",
    "بيفحص":     "🔍",
    "يفحص":      "🔍",
    "خطة":       "📋",
    "منهج":      "📋",
    "بيراجع":    "🔎",
    "بيحل":      "⚡",
    "اختبار":    "🧪",
    "يحدّث":     "🔄",
    "انتظار":    "⏳",
    "طابور":     "🚦",
}

def _phase_icon(phase: str) -> str:
    for key, icon in _PHASE_ICONS.items():
        if key in phase:
            return icon
    return "⚙️"

# ساعات متحركة
_CLOCKS = "🕛🕐🕑🕒🕓🕔🕕🕖🕗🕘🕙🕚"


class Progress:
    def __init__(self, bot, chat_id, message_id, username=""):
        self.bot        = bot
        self.chat_id    = chat_id
        self.message_id = message_id
        self.username   = username  # كود المستخدم لعرضه في التقدم

        self.lines               = deque(maxlen=5)   # آخر 5 نشاطات
        self.completed           = set()
        self.transitions         = set()
        self.plan_units          = {}
        self.platform_progress   = None
        self.initial_platform_done = None
        self.questions           = 0
        self.activities          = None
        self.header              = "🚀 <b>جاري الحل</b>"
        self.phase               = "بيجهّز كل شيء"
        self.current_activity    = ""
        self.quiz_position       = None
        self.show_stop           = True
        self.dirty               = True
        self.t0                  = time.monotonic()
        self.clock_started       = False     # العدّاد بيبدأ أول ما الحل يبدأ فعلًا (مش وقت الانتظار في الطابور)
        self.queue_wait          = 0.0
        self.last_edit_at        = 0.0
        self.last_rendered       = None
        self.retry_not_before    = 0.0
        self.edit_failures       = 0
        # لتتبع الفصول المكتملة بالترتيب
        self.chapter_log         = []  # list of chapter names completed

    # ─── intake ───

    def _record_plan_unit(self, text):
        parts = (text or "").rsplit("|", 2)
        if len(parts) != 3:
            return
        name, done, total = parts
        try:
            done, total = int(done), int(total)
        except ValueError:
            return
        normalized = core.normalize_text(name)
        if normalized and total > 0:
            self.plan_units[normalized] = (name, done, total)

    def handle(self, kind, text):
        raw = text or ""
        e   = html.escape(raw)

        if kind == "q_ok":
            self.questions += 1
            self.dirty = True
            return
        elif kind == "phase":
            self.phase = raw
            self.dirty = True
            return
        elif kind == "login_ok":
            self.phase = "✅ تم الدخول — بيراجع المنهج"
            self.dirty = True
            return
        elif kind == "open":
            self.current_activity = raw
            self.phase            = "بيحل النشاط الحالي"
            self.quiz_position    = None
            self.dirty = True
            return
        elif kind == "quiz_solving":
            self.current_activity = raw
            self.phase            = "بيحل الاختبار"
            self.quiz_position    = None
            self.dirty = True
            return
        elif kind == "quiz_progress":
            parts = raw.split("|", 1)
            if len(parts) == 2:
                try:
                    self.quiz_position = (int(parts[0]), int(parts[1]))
                except ValueError:
                    self.quiz_position = None
            self.phase = "بيحل الاختبار"
            self.dirty = True
            return
        elif kind == "log":
            self.phase = raw
            self.dirty = True
            return
        elif kind == "plan_todo":
            self._record_plan_unit(raw)
            self.dirty = True
            return
        elif kind == "plan_progress":
            parts = raw.split("|", 1)
            if len(parts) == 2:
                try:
                    done, total = (int(v) for v in parts)
                except ValueError:
                    return
                if 0 <= done <= total and total > 0:
                    if self.initial_platform_done is None:
                        self.initial_platform_done = done
                    self.platform_progress = (done, total)
                    self.activities = max(0, done - self.initial_platform_done)
                    self.dirty = True
            return
        elif kind in ("done", "moved_on"):
            normalized = core.normalize_text(text or "")
            if not normalized or normalized in self.transitions:
                return
            self.transitions.add(normalized)
            self.lines.append(f"➡️ تم: <b>{e}</b>")
            if core.normalize_text(self.current_activity) == normalized:
                self.current_activity = ""
                self.quiz_position    = None
                self.phase = "بيراجع التقدم"
            elif not self.current_activity:
                self.phase = "بيراجع التقدم"
        elif kind == "quiz_done":
            normalized = core.normalize_text(text or "")
            if not normalized or normalized in self.completed:
                return
            self.completed.add(normalized)
            self.lines.append(f"✅ اجتاز الاختبار: <b>{e}</b>")
            if core.normalize_text(self.current_activity) == normalized:
                self.current_activity = ""
                self.quiz_position    = None
                self.phase = "بيراجع التقدم"
            elif not self.current_activity:
                self.phase = "بيراجع التقدم"
        elif kind == "chapter_done":
            # يسجّل الفصل في chapter_log لعرضه في آخر النشاطات
            parts = (text or "").split("|", 2)
            ch_name = parts[0] if parts else text
            if ch_name and ch_name not in self.chapter_log:
                self.chapter_log.append(ch_name)
        elif kind == "group_done":
            normalized = core.normalize_text(text or "")
            if not normalized or normalized in self.completed:
                return
            self.completed.add(normalized)
            self.lines.append(f"✅ اكتملت الوحدة: <b>{e}</b>")
        elif kind in ("plan_done", "group_skipped"):
            if kind == "plan_done":
                self._record_plan_unit(raw)
                name = e.split("|")[0]
            else:
                name = e
            normalized = core.normalize_text(text or "")
            if not normalized or normalized in self.completed:
                return
            self.completed.add(normalized)
            self.lines.append(f"⏭️ مكتملة مسبقًا: <b>{name}</b>")
        else:
            return
        self.dirty = True

    # ─── render ───

    def _eta_line(self, elapsed):
        done, total = self.overall_items()
        if (
            self.initial_platform_done is None
            or total <= 0
            or done <= self.initial_platform_done
            or done >= total
            or elapsed < 30
        ):
            return ""
        rate = (done - self.initial_platform_done) / elapsed
        if rate <= 0:
            return ""
        remaining = (total - done) / rate
        estimate  = round(remaining, -1) or remaining
        return (
            f"⏳ المتبقي تقريبًا: <b>~{format_duration(estimate)}</b> "
            "<i>(تقريبي)</i>\n"
        )

    def overall_items(self):
        return self.platform_progress if self.platform_progress is not None else (0, 0)

    def start_clock(self):
        """أول ما الحل يبدأ فعلًا (بعد الطابور): نصفّر العدّاد ونسجّل كام استنى."""
        now = time.monotonic()
        self.queue_wait = max(0.0, now - self.t0)
        self.t0 = now
        self.clock_started = True
        self.dirty = True

    def elapsed(self):
        return (time.monotonic() - self.t0) if self.clock_started else 0.0

    def render(self):
        elapsed = self.elapsed()

        # ─── السطر الأول: الكود ───
        code_line = f"<b>كود:</b> <code>{html.escape(self.username)}</code>\n" if self.username else ""

        # ─── شريط التقدم ───
        if self.platform_progress is None:
            bar_line = "التقدم: ⏳ <i>بيحضّر الخطة...</i>\n"
        else:
            done, total = self.platform_progress
            pct = max(0, min(100, int(done * 100 / total))) if total > 0 else 0
            bar  = progress_bar(done, total, 18)
            bar_line = (
                f"📊 التقدم: <code>{bar}</code> <b>{ar_num(pct)}٪</b>"
                f"  <i>({ar_num(done)}/{ar_num(total)})</i>\n"
            )

        # ─── العنصر الحالي ───
        item_lines = []
        if self.platform_progress is not None:
            done, total = self.platform_progress
            item_lines.append(
                f"📚 الفصول المكتملة: <b>{ar_num(done)} / {ar_num(total)}</b>"
            )
        if self.current_activity:
            item_lines.append(
                f"📍 النشاط الحالي: {html.escape(self.current_activity[:60])}"
            )
        item_line = "\n".join(item_lines) + ("\n" if item_lines else "")

        # ─── الفصول المكتملة (نفس الرسالة بتتحدّث) ───
        chapters_section = ""
        if self.chapter_log:
            shown = self.chapter_log[-8:]
            hidden = len(self.chapter_log) - len(shown)
            rows = [f"✅ {html.escape(ch[:45])}" for ch in shown]
            if hidden > 0:
                rows.insert(0, f"<i>… و{ar_num(hidden)} فصل قبلهم</i>")
            chapters_section = "\n📘 <b>آخر الفصول المنجزة</b>\n" + "\n".join(rows) + "\n"

        # ─── آخر النشاطات الأخرى (اختبارات/وحدات) ───
        others = [line for line in self.lines if "اكتمل" not in line and "اجتاز" not in line]
        recent_section = ""
        if others:
            recent_section = "\n🧾 <b>آخر التحديثات</b>\n" + "\n".join(f"• {o}" for o in others[-3:]) + "\n"

        return (
            f"{self.header}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            + code_line
            + f"{_phase_icon(self.phase)} <b>{html.escape(self.phase)}</b>\n"
            + bar_line
            + item_line
            + self._eta_line(elapsed)
            + (
                f"⏱️ الوقت المنقضي: <b>{format_duration(elapsed)}</b>\n"
                if self.clock_started else
                "⏱️ الوقت: <i>هيبدأ أول ما الحل يبدأ</i>\n"
            )
            + chapters_section
            + recent_section
        )

    async def flush(self, force=False):
        now = time.monotonic()
        if not force and not self.dirty:
            return
        if now < self.retry_not_before:
            return
        if not force and self.last_edit_at and now - self.last_edit_at < 1.5:
            return
        rendered = self.render()
        if rendered == self.last_rendered:
            self.dirty = False
            return
        try:
            await self.bot.edit_message_text(
                rendered,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=stop_kb() if self.show_stop else None,
            )
            self.dirty        = False
            self.last_rendered = rendered
            self.last_edit_at  = time.monotonic()
            self.retry_not_before = 0.0
            self.edit_failures = 0
        except RetryAfter as exc:
            self.dirty = True
            self.retry_not_before = time.monotonic() + exc.retry_after
            log.warning("progress edit rate-limited; retrying after %.1fs", exc.retry_after)
        except BadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                self.dirty        = False
                self.last_rendered = rendered
                self.last_edit_at  = time.monotonic()
            else:
                self.dirty = False
                log.warning("progress edit rejected: %s", exc)
        except TelegramError as exc:
            self.dirty = True
            self.edit_failures += 1
            delay = min(30, 2 ** min(self.edit_failures - 1, 5))
            self.retry_not_before = time.monotonic() + delay
            log.warning("progress edit failed; retrying in %ss: %s", delay, exc)


async def ticker(progress, stop):
    last_clock = progress.t0
    while not stop.is_set():
        if time.monotonic() - last_clock >= 10:
            progress.dirty = True
            last_clock = time.monotonic()
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        if progress.dirty:
            await progress.flush()


# ═══════════════════════════════════════════════════════
#  تشخيص للأدمن
# ═══════════════════════════════════════════════════════

async def send_owner_diagnostic(app, report):
    if not OWNER_CHAT_ID:
        return
    details = (
        f"🚨 تقرير عطل #{report['id']}\n"
        f"الحالة:    {report['status']}\n"
        f"الوقت:     {report['timestamp']}\n"
        f"الموقع:    {report['page']}\n"
        f"النشاط:    {report['activity']}\n"
        f"التشخيص:   {report['error']}"
    )[:4000]
    try:
        await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=details)
    except TelegramError as exc:
        log.warning("owner diagnostic message failed (%s)", type(exc).__name__)


def should_notify_admin_completion(status):
    """إشعار "إتمام الحل" للأدمن بيتبعت بس لما المنصة اتنجزت فعلًا (status == finished).

    الدخول الغلط، التوقف، الإيقاف اليدوي، انتهاء المهلة، الأخطاء، الاختبارات المعلّقة، والحساب
    المنجز مسبقًا (already_completed) كلها مش إنجاز، فمفيهاش إشعار للأدمن."""
    return status == "finished"


_ACCESS_ERROR_RE = re.compile(
    r"course access|no access to this (subject|course)|not contracted", re.I
)


def is_access_error(error):
    """الحساب دخل صح بس مالوش صلاحية على المادة/الكورس (403) — ده مش خطأ مؤقت ومش بيتحل بإعادة المحاولة."""
    return bool(error) and bool(_ACCESS_ERROR_RE.search(str(error)))


def subject_note(platform, result):
    """سطر «📖 المادة» لحسابات الثقافة المالية اللي اتحلّت على مادة غير الأساسية (طلاب اللغات)؛ غير كده فاضي."""
    if platform not in (None, "", "financial") or not isinstance(result, dict):
        return ""
    subject_id = result.get("subject_id")
    if subject_id is None or subject_id == core.SUBJECT_IDS[0]:
        return ""
    label = str(result.get("subject_label") or "").strip()
    name = f"{html.escape(label)} " if label else ""
    return f"📖 <b>المادة:</b> {name}(<code>{subject_id}</code>)\n"


async def notify_admin_completion(app, uid, username, platform, status, questions, activities, elapsed, last, queue_wait=0.0, subject_line=""):
    """إشعار الأدمن بإنجاز منصة فعلًا. أي حالة تانية مفيهاش إشعار."""
    if not OWNER_CHAT_ID or not should_notify_admin_completion(status):
        return
    user_info = db.get_user_stats(uid)
    name = html.escape(user_info.get("name") or "مجهول")
    if platform == "qureo":
        platform_ar = "🐍 Qureo Python"
    elif platform == "javascript":
        platform_ar = "🟨 Qureo JavaScript"
    else:
        platform_ar = "💰 الثقافة المالية"
    status_icons = {
        "finished":  "✅ اكتمل بنجاح",
        "stuck":     "🧩 توقف في منتصف الطريق",
        "stopped":   "⛔ أوقفه المستخدم",
        "timeout":   "⌛ انتهت المهلة",
        "login_failed": "❌ فشل الدخول",
        "error":     "⚠️ خطأ غير متوقع",
    }
    status_text = status_icons.get(status, f"❓ {status}")
    text = (
        "🔔 <b>إشعار إتمام حل</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>المستخدم:</b> {name}\n"
        f"🆔 <b>Telegram ID:</b> <code>{uid}</code>\n"
        f"🔑 <b>كود المنصة:</b> <code>{html.escape(str(username))}</code>\n"
        f"📚 <b>المنصة:</b> {platform_ar}\n"
        f"{subject_line}"
        f"📊 <b>الحالة:</b> {status_text}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 أسئلة أُجيبت: <b>{ar_num(questions or 0)}</b>\n"
        f"📚 أنشطة أُكملت: <b>{ar_num(activities or 0)}</b>\n"
        f"⏱️ وقت الحل: <b>{format_duration(elapsed)}</b>\n"
        + (f"🕒 انتظار في الطابور: <b>{format_duration(queue_wait)}</b>\n" if queue_wait >= 5 else "")
        + f"📍 آخر نشاط: <b>{html.escape(last or '—')}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 إجمالي جلسات المستخدم: <b>{ar_num(user_info.get('sessions', 0))}</b>"
    )
    try:
        await app.bot.send_message(OWNER_CHAT_ID, text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.warning("admin completion notify failed (%s)", type(exc).__name__)


# ═══════════════════════════════════════════════════════
#  تشغيل الحل
# ═══════════════════════════════════════════════════════

async def run_student_job(
    app, chat_id, uid, username, password, platform="financial"
):
    data = app.bot_data
    if uid in data["cancels"]:
        return
    vip_priority = is_vip_user(uid)
    if not vip_priority and queue_closed(app):
        password = None
        await app.bot.send_message(
            chat_id, QUEUE_CLOSED_TEXT, parse_mode=ParseMode.HTML, reply_markup=subscription_kb()
        )
        return
    if queue_full(app) and not vip_priority:
        password = None
        await app.bot.send_message(chat_id, QUEUE_FULL_TEXT, reply_markup=after_kb())
        return

    cancel        = threading.Event()
    timed_out     = threading.Event()
    data["cancels"][uid] = cancel
    data.setdefault("job_states", {})[uid] = "waiting"      # لحد ما ياخد مكان: بعدها "running"
    counted_normal = not vip_priority            # المشتركين بيتحلوا فورًا: مش بيتحسبوا في الطابور
    if counted_normal:
        data["normal_active"] = data.get("normal_active", 0) + 1
    queued        = counted_normal and data["normal_active"] > MAX_JOBS
    queue_reserved = queued
    if queue_reserved:
        data["waiting"] += 1

    started      = time.time()
    loop         = asyncio.get_running_loop()
    progress     = None
    stop_ticker  = None
    ticker_task  = None
    timer        = None
    sem          = data["psem"] if vip_priority else data["sem"]
    acquired     = False
    result       = {"status": "error", "error": "unknown"}

    def hook(kind, text):
        if kind == "diagnostic":
            loop.call_soon_threadsafe(
                lambda: app.create_task(send_owner_diagnostic(app, text))
            )
            return
        if kind == "debug":
            safe = str(text)
            for secret in (password, username):
                if secret:
                    safe = safe.replace(secret, "***")
            log.info("solver uid=%s: %s", uid, safe[:600])
            return
        loop.call_soon_threadsafe(progress.handle, kind, text)

    def on_timeout():
        timed_out.set()
        cancel.set()

    try:
        msg = await app.bot.send_message(
            chat_id,
            "⏳ <b>جاري تجهيز المهمة</b>\n"
            "<i>هتظهر تفاصيل التقدم هنا.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=stop_kb(),
        )
        progress    = Progress(app.bot, chat_id, msg.message_id, username)
        stop_ticker = asyncio.Event()
        ticker_task = asyncio.create_task(ticker(progress, stop_ticker))

        if queued:
            progress.handle(
                "phase",
                f"في انتظار مكان في الطابور (ترتيبك {ar_num(data['waiting'])})",
            )
        acquired = await acquire_job_slot(app, sem, cancel, vip_priority)
        if queue_reserved:
            data["waiting"] -= 1
            queue_reserved   = False

        if not acquired:
            result = {"status": "stopped"}
        else:
            data["job_states"][uid] = "running"
            try:
                if cancel.is_set():
                    result = {"status": "stopped"}
                else:
                    progress.start_clock()                      # من هنا بس بيتحسب وقت الحل
                    progress.handle("phase", "بيسجّل الدخول")
                    timer = loop.call_later(JOB_TIMEOUT_MIN * 60, on_timeout)
                    if platform in ("qureo", "javascript"):
                        engine = qureo_engine
                        entries = (
                            data["qureo_entries"] if platform == "qureo"
                            else data["javascript_entries"]
                        )
                        course_id = (
                            "python_short_ar" if platform == "qureo"
                            else JAVASCRIPT_COURSE_ID
                        )
                        result = await asyncio.to_thread(
                            engine.run_job,
                            username, password,
                            entries,
                            hook, cancel,
                            True,
                            False,
                            course_id,
                        )
                    else:
                        engine = core
                        entries = data["entries"]
                        result = await asyncio.to_thread(
                            engine.run_job,
                            username, password,
                            entries,
                            hook, cancel,
                        )
            finally:
                sem.release()
                acquired = False
    except Exception as exc:  # noqa: BLE001
        log.exception("job crashed uid=%s (%s)", uid, type(exc).__name__)    # traceback كامل في اللوج
        detail = f"{type(exc).__name__}: {exc}"
        for secret in (password, username):
            if secret:
                detail = detail.replace(secret, "***")
        result = {"status": "error", "error": "حصل خطأ غير متوقع في البوت.", "internal": detail[:200]}
    finally:
        password = None
        if timer:
            timer.cancel()
        if acquired:
            sem.release()
        if queue_reserved:
            data["waiting"] -= 1
        if stop_ticker:
            stop_ticker.set()
        if ticker_task:
            try:
                await ticker_task
            except Exception as exc:  # noqa: BLE001
                log.warning("progress ticker stopped unexpectedly (%s)", type(exc).__name__)
        if data["cancels"].get(uid) is cancel:
            data["cancels"].pop(uid, None)
            data.get("job_states", {}).pop(uid, None)
        if counted_normal:
            data["normal_active"] = max(0, data.get("normal_active", 1) - 1)

    elapsed = progress.elapsed() if progress else 0.0      # وقت الحل الفعلي من غير انتظار الطابور

    if progress is None:
        db.log_job(uid, started, result.get("status", "error"), 0, 0, result.get("error", ""))
        return

    status           = result.get("status")
    if timed_out.is_set():
        status = "timeout"
    last             = result.get("last") or "—"
    log.info(
        "job uid=%s status=%s elapsed=%ss requests=%s guessed=%s retried=%s learned=%s",
        uid, status, result.get("elapsed"), result.get("requests"),
        result.get("guessed"), result.get("retried"), result.get("learned"),
    )
    platform_progress = progress.overall_items()
    summary = job_summary(progress.questions, progress.activities, elapsed, platform_progress)
    attempt_info = None       # نتيجة خصم المحاولة (لو الحل خلص فعلًا)

    if status == "finished":
        if result.get("precompleted"):
            status = "already_completed"
            progress.header = "ℹ️ <b>الحساب منجز مسبقًا</b>"
            final = (
                "ℹ️ <b>الحساب ده المنصة عليه منجزة بالكامل بالفعل.</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "البوت ما نفّذش أنشطة جديدة، ولذلك لم يتم خصم أي محاولة.\n"
                "جرّب حسابًا تاني أو تواصل مع المالك لو محتاج دعم."
            ) + chapters_report(result) + perfect_note(result)
        else:
            progress.header = "🎉 <b>اكتملت الأنشطة المتاحة</b>"
            final = completion_message(
                progress.questions, progress.activities, elapsed, last, platform_progress
            ) + chapters_report(result) + perfect_note(result)
            if uid != OWNER_CHAT_ID:
                attempt_info = db.consume_attempt(uid, platform, username)
                congrats = st.attempt_done_text(attempt_info, db.get_entitlement(uid), fmt_ts)
                if congrats:
                    final += "\n\n" + congrats       # مبروك + استخدمت كام وفاضلك كام
            db.mark_account_completed(uid, username, platform)
    elif status == "tests_pending":
        test_name = html.escape((result.get("test") or "")[:120])
        if result.get("reason") == "failed":
            progress.header = "📝 <b>الاختبار لم يُجتز</b>"
            final = (
                f"📝 <b>المنصة أكدت عدم اجتياز الاختبار:</b>\n"
                f"{('• ' + test_name) if test_name else ''}\n\n{summary}\n\n"
                "لم أسجّل الاختبار مكتملًا. راجع المحاولة في المنصة ثم شغّل الحل مجددًا."
            )
        else:
            progress.header = "📝 <b>وقفت عند اختبار — مفيش إجابات مطابقة</b>"
            final = (
                f"📝 <b>وصلت لاختبار مش موجود في بنك الإجابات:</b>\n"
                f"{('• ' + test_name) if test_name else ''}\n"
                f"{('❓ ' + html.escape(result['question'])) if result.get('question') else ''}\n\n{summary}\n\n"
                "حلّه بنفسك، وبعدين شغّل الحل تاني والبوت يكمّل اللي بعده."
            )
    elif status == "login_failed":
        progress.header = "❌ <b>فشل تسجيل الدخول</b>"
        if uid != OWNER_CHAT_ID:
            db.release_free_claim(uid, platform, username)   # الدخول فشل: الحساب مايتحسبش كأول حساب
        final = "❌ الكود أو الباسورد غلط، أو الحساب مش متاح.\nراجع بياناتك وحاول تاني."
    elif status == "stopped":
        progress.header = "⛔ <b>اتوقّف الحل</b>"
        final = f"⛔ وقفت الحل.\n\n{summary}"
    elif status in ("stuck", "error") and is_access_error(result.get("error")):
        progress.header = "🔒 <b>الحساب مالوش صلاحية على المادة</b>"
        log.warning("job no-access uid=%s platform=%s: %s", uid, platform, result.get("error"))
        if uid != OWNER_CHAT_ID:
            db.release_free_claim(uid, platform, username)   # ماحصلش حل فعلي: الحساب مايتحسبش كأول حساب
        final = (
            "🔒 <b>الدخول نجح، لكن الحساب ده مالوش صلاحية على المادة دي.</b>\n"
            "غالبًا مفيش اشتراك/تفعيل للكورس على المنصة، أو الكود تابع لدورة تانية.\n"
            "اتأكد من الكورس على المنصة نفسها، أو جرّب المنصة التانية من القائمة.\n\n"
            "لم يتم خصم أي محاولة."
        )
    elif status == "stuck":
        progress.header = "🧩 <b>تعذّر إكمال بعض الأنشطة</b>"
        log.warning("job stuck uid=%s: %s", uid, result.get("error"))
        if platform in ("qureo", "javascript") and "select " in result.get("error", ""):
            course_name = "JavaScript" if platform == "javascript" else "Python"
            final = (
                f"🧩 <b>اختار دورة {course_name} على Qureo أولًا.</b>\n"
                f"افتح المنصة وحدد دورة {course_name} ثم شغّل البوت مجددًا.\n\n"
                f"{summary}"
            )
        else:
            detail = (
                f"\nالتفاصيل: <i>{html.escape(result.get('error', '')[:200])}</i>\n"
                if platform in ("qureo", "javascript") and result.get("error") else ""
            )
            final = (
                "🧩 <b>في أنشطة ما قدرتش أكملها دلوقتي.</b>\n"
                f"\n{summary}\n"
                f"📍 آخر نشاط: <b>{html.escape(last)}</b>{detail}\n"
                "شغّل الحل مجددًا ليعيد المحاولة من التقدم المحفوظ."
            )
    elif status == "timeout":
        progress.header = "⌛ <b>خلصت المدة القصوى</b>"
        final = f"⌛ الحل خد وقت أطول من المسموح.\n\n{summary}\nتقدر تشغّله تاني وهيكمّل من حيث وقف."
    else:
        progress.header = "⚠️ <b>الحل وقف بسبب مشكلة</b>"
        final = (
            "⚠️ <b>حصلت مشكلة مؤقتة وقفت الحل.</b>\n"
            f"حاول تاني بعد شوية وهيكمّل من حيث وقف.\n\n{summary}\nآخر نشاط: {html.escape(last)}"
        )
        log.warning("job error uid=%s: %s", uid, result.get("error"))

    progress.show_stop = False
    progress.dirty     = True
    await progress.flush(force=True)
    await app.bot.send_message(chat_id, final, parse_mode=ParseMode.HTML, reply_markup=after_kb())
    if uid != OWNER_CHAT_ID and st.vip_pack_finished(attempt_info):
        try:   # خلّص باقة الـ١٠ محاولات: رسالة شكر + تجديد
            other = db.get_entitlement(uid)["attempts_remaining"]
            await app.bot.send_message(
                chat_id, subscription_text(st.subscription_ended_text(attempt_info, other)),
                parse_mode=ParseMode.HTML, reply_markup=subscription_kb(),
            )
        except TelegramError:
            pass
    elif uid != OWNER_CHAT_ID and not db.has_attempt(uid):
        try:   # خلّص آخر محاولة: نعرض له الاشتراكات
            await app.bot.send_message(
                chat_id, subscription_text("🚫 <b>خلصت محاولاتك.</b>"),
                parse_mode=ParseMode.HTML, reply_markup=subscription_kb(),
            )
        except TelegramError:
            pass
    db.log_job(
        uid, started, status,
        progress.questions, progress.activities,
        (result.get("internal") or result.get("error", "") or ""),
        duration=int(elapsed),
    )
    # إشعار الأدمن: بس لو المنصة اتنجزت فعلًا (مش دخول غلط ولا توقف ولا خطأ ولا حساب منجز قبل كده)
    if should_notify_admin_completion(status):
        await notify_admin_completion(
            app, uid, username, platform, status,
            progress.questions, progress.activities, elapsed, last,
            queue_wait=progress.queue_wait, subject_line=subject_note(platform, result),
        )


async def on_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    event = ctx.application.bot_data["cancels"].get(q.from_user.id)
    if event:
        event.set()
        await q.answer("هوقف بعد الخطوة الحالية…")
    else:
        await q.answer("مفيش حل شغّال.")


# ═══════════════════════════════════════════════════════
#  تهيئة وبناء التطبيق
# ═══════════════════════════════════════════════════════

BACKUP_EVERY_HOURS = _int_setting("BACKUP_EVERY_HOURS", 6, 1)
BACKUP_KEEP        = _int_setting("BACKUP_KEEP", 10, 1)


def _db_path():
    return os.getenv("DB_PATH", str(BASE / "bot.db"))


def _backup_now(reason):
    try:
        target = db.backup_file(_db_path(), Path(_db_path()).resolve().parent / "backups", BACKUP_KEEP)
        if target:
            log.info("database backup (%s): %s", reason, target.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("database backup failed (%s): %s", reason, type(exc).__name__)


async def _backup_loop():
    while True:
        await asyncio.sleep(BACKUP_EVERY_HOURS * 3600)
        await asyncio.to_thread(_backup_now, "periodic")


async def on_unmatched_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """أدمن: يبعت ملف الأسئلة اللي اتخمّنت (مش في البنك) عشان تكمّل البنك."""
    if not _is_admin(update.effective_user.id):
        return
    path = matching.unmatched_log_path()
    if path is None or not Path(path).exists() or Path(path).stat().st_size == 0:
        await update.effective_message.reply_text("✅ مفيش أسئلة متخمّنة متسجّلة. كل اللي اتحل كان من البنك أو من التعلّم.")
        return
    count = sum(1 for _ in Path(path).open(encoding="utf-8"))
    with Path(path).open("rb") as fh:
        await update.effective_message.reply_document(
            fh, filename="unmatched_questions.jsonl",
            caption=f"📝 {ar_num(count)} سؤال اتخمّن ومش في البنك. ضيف إجاباتهم للبنك وهيختفوا من الخطأ المحتمل.",
        )


async def post_init(app: Application):
    _backup_now("before-start")          # نسخة قبل أي ترحيل (ALTER TABLE) أو تحديث
    db.init(_db_path())
    app.bot_data["backup_task"] = asyncio.create_task(_backup_loop())
    seeded = db.seed_ready_codes(300)
    log.info(
        "ready activation codes: single=%d vip=%d vip_unlimited=%d",
        seeded["single"], seeded["vip"], seeded["vip_unlimited"],
    )
    # asyncio.to_thread بيستخدم pool افتراضي صغير (≈ عدد الأنوية+4): نكبّره عشان كل الحلول تشتغل فعلًا مع بعض
    app_loop = asyncio.get_running_loop()
    app_loop.set_default_executor(
        ThreadPoolExecutor(max_workers=MAX_JOBS + PRIORITY_JOBS + 8, thread_name_prefix="solver")
    )
    if core.LESSON_PARALLEL <= 1 or core.TEST_PARALLEL <= 1:
        log.warning(
            "التوازي متقفل (LESSON_PARALLEL=%s, TEST_PARALLEL=%s): الحل هيبقى أبطأ. "
            "غيّرهم في .env لـ 6 (تكيّفي ومحمي من 429).",
            core.LESSON_PARALLEL, core.TEST_PARALLEL,
        )
    app.bot_data["sem"]     = asyncio.Semaphore(MAX_JOBS)
    app.bot_data["psem"]    = asyncio.Semaphore(PRIORITY_JOBS)   # المشتركين: مسار منفصل من غير طابور
    app.bot_data["cancels"] = {}
    app.bot_data["waiting"] = 0
    app.bot_data["normal_active"] = 0
    app.bot_data["queue_closed"] = db.get_setting("queue_closed", "0") == "1"
    app.bot_data["maintenance"] = db.get_setting("maintenance", "0") == "1"
    app.bot_data["entries"] = core.parse_answer_file(ANSWERS_FILE)
    app.bot_data["qureo_entries"] = qureo_engine.parse_answer_file(QUREO_ANSWERS_FILE)
    app.bot_data["javascript_entries"] = qureo_engine.parse_answer_file(JAVASCRIPT_ANSWERS_FILE)
    log.info("loaded %d answers", len(app.bot_data["entries"]))
    log.info("loaded %d Qureo Python answers", len(app.bot_data["qureo_entries"]))
    log.info("loaded %d Qureo JavaScript answers", len(app.bot_data["javascript_entries"]))


def build_app():
    req = HTTPXRequest(
        connect_timeout=30.0, read_timeout=30.0,
        write_timeout=30.0,   pool_timeout=30.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    private = filters.ChatType.PRIVATE

    app.add_handler(TypeHandler(Update, on_gate_update), group=-1)    # بوابة الاشتراك الإجباري قبل أي معالج
    app.add_handler(CommandHandler("start",    on_start,    filters=private))
    app.add_handler(CommandHandler("menu",     on_start,    filters=private))
    app.add_handler(CommandHandler("cancel",   on_cancel_cmd, filters=private))
    app.add_handler(CommandHandler("help",     on_help_cmd, filters=private))
    app.add_handler(CommandHandler("mystats",  on_mystats_cmd, filters=private))
    app.add_handler(CommandHandler("stats",    on_stats_cmd, filters=private))  # أدمن
    app.add_handler(CommandHandler("admin",    on_stats_cmd, filters=private))
    app.add_handler(CommandHandler("user",     on_user_lookup_cmd, filters=private))
    app.add_handler(CommandHandler("broadcast", on_broadcast_cmd, filters=private))
    app.add_handler(CommandHandler("redeem", on_redeem_cmd, filters=private))
    app.add_handler(CommandHandler("maintenance", on_maintenance_cmd, filters=private))
    app.add_handler(CommandHandler("queue", on_queue_cmd, filters=private))
    app.add_handler(CommandHandler("unmatched", on_unmatched_cmd, filters=private))
    app.add_handler(CommandHandler("plans", on_plans_cmd, filters=private))
    app.add_handler(CommandHandler("gencodes", on_gencodes_cmd, filters=private))
    app.add_handler(CommandHandler("vips", on_vips_cmd, filters=private))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(on_error)

    for pattern, handler in [
        (r"^solve(_financial|_qureo|_javascript)?$", on_solve),
        (r"^use_saved$",              on_use_saved),
        (r"^new_user$",               on_new_user),
        (r"^cancel_flow$",            on_cancel_flow),
        (r"^stop$",                   on_stop),
        (r"^help$",                   on_help),
        (r"^privacy$",                on_privacy),
        (r"^salat$",                  on_salat),
        (r"^menu$",                   on_menu),
        (r"^mystats$",                on_mystats_cb),
        (r"^check_channels$", on_check_channels),
        (r"^redeem_start$",   on_redeem_start),
        (r"^invite$",         on_invite),
        (r"^admin_channels$", on_admin_channels),
        (r"^admin_channel_toggle$", on_admin_channel_toggle),
        (r"^admin_channel_add$", on_admin_channel_add),
        (r"^admin_channel_message$", on_admin_channel_message),
        (r"^admin_channel_remove_.+$", on_admin_channel_remove),
        (r"^admin_codes$",    on_admin_codes),
        (r"^admin_codes_(single|vip|unlimited)$", on_admin_codes_category),
        (r"^admin_code_generate_(single|vip|vip_unlimited)$", on_admin_code_generate),
        (r"^admin_stats$",            on_admin_stats_cb),
        (r"^admin_users(_page_\d+)?$", on_admin_users),
        (r"^admin_vips(_(all|active|done)_\d+)?$", on_admin_vips),
        (r"^admin_user_\d+$",         on_admin_user),
        (r"^admin_block_\d+$",        on_admin_toggle_block),
        (r"^admin_active$",           on_admin_active),
        (r"^admin_broadcast_start$",  on_admin_broadcast_start),
        (r"^admin_broadcast_confirm_[a-f0-9]{12}$", on_admin_broadcast_confirm),
        (r"^admin_broadcast_cancel_[a-f0-9]{12}$", on_admin_broadcast_cancel),
        (r"^admin_search$",           on_admin_search_start),
        (r"^admin_jobs$",             on_admin_jobs),
        (r"^admin_export$",           on_admin_export),
        (r"^admin_stop_\d+$",         on_admin_stop_job),
        (r"^admin_health$",           on_admin_health),
        (r"^admin_maintenance_toggle$", on_admin_maintenance),
        (r"^admin_queue_toggle$",     on_admin_queue),
        (r"^plans$",                  on_plans),
    ]:
        app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    return app


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("حط BOT_TOKEN في ملف .env")
    build_app().run_polling()
