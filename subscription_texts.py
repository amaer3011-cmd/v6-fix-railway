"""
subscription_texts.py — رسائل الاشتراكات والأكواد (نصوص بس، من غير أي اعتماد على تيليجرام).

  • رسالة تفعيل الكود + مميزات الاشتراك
  • رسالة بعد كل محاولة ناجحة (استخدمت كام وفاضلك كام)
  • رسالة انتهاء الاشتراك
  • شكل قائمة مشتركي VIP عند المالك

كلها بترجّع نص HTML جاهز للإرسال بـ parse_mode=HTML.
"""
import html

_AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

VIP_LABEL = "VIP عادي — ١٠ محاولات"
SINGLE_LABEL = "محاولة واحدة"
WEEKLY_LABEL = "VIP مميز — أسبوع غير محدود"
LINE = "━━━━━━━━━━━━━━━━━━━━"


def ar_num(n):
    return f"{int(n):,}".replace(",", "٬").translate(_AR_DIGITS)


def ar_attempts(n):
    """«محاولة واحدة» / «محاولتين» / «٣ محاولات» / «١١ محاولة»."""
    n = int(n)
    if n <= 0:
        return "صفر محاولات"
    if n == 1:
        return "محاولة واحدة"
    if n == 2:
        return "محاولتين"
    if n <= 10:
        return f"{ar_num(n)} محاولات"
    return f"{ar_num(n)} محاولة"


def used_bar(used, total, width=10):
    """شريط 🟩⬜ بيبيّن المستخدم من الإجمالي."""
    total = max(int(total), 1)
    done = max(0, min(width, round(width * max(0, int(used)) / total)))
    if used > 0 and done == 0:
        done = 1
    if used >= total:
        done = width
    return "🟩" * done + "⬜" * (width - done)


# ───────────────────────── تفعيل الكود ─────────────────────────

_FEATURES_COMMON = (
    "⚡ حلّك بيبدأ <b>فورًا</b> من غير ما تقف في الطابور\n"
    "🔓 تحل <b>أي حساب</b> على <b>أي منصة</b> (مش متقفل على حساب واحد)\n"
    "✅ المحاولة بتتخصم بس لما الحل <b>يخلص فعلًا</b> — لو الدخول فشل أو الحساب كان منجز قبل كده، مفيش خصم\n"
)

_THANKS = (
    "💚 شكرًا إنك اشتريت الكود، ونتمنى إن الخدمة تعجبك وتكون عند حسن ظنك 🌹\n"
)


def redeem_success_text(code_type, ent, fmt_ts=None):
    """رسالة الفرحة بعد تفعيل كود. ent = db.get_entitlement(uid) بعد التفعيل."""
    fmt_ts = fmt_ts or str
    head = "🎉🎊 <b>مبروووووك! اشتراكك اتفعّل بنجاح</b> 🎊🎉\n" + LINE + "\n"

    if code_type == "vip":
        left = ent.get("vip_attempts_remaining", 0)
        return (
            head
            + f"💎 <b>الباقة:</b> {VIP_LABEL}\n"
            + f"🎟️ <b>رصيدك دلوقتي:</b> {ar_attempts(left)} VIP\n\n"
            + _THANKS + "\n"
            + "✨ <b>مميزات اشتراكك:</b>\n"
            + _FEATURES_COMMON
            + "📊 بعد كل محاولة بتعرف استخدمت كام وفاضلك كام\n\n"
            + "🚀 اختار منصتك وابدأ دلوقتي!"
        )

    if code_type == "vip_unlimited":
        until = ent.get("unlimited_until", 0)
        return (
            head
            + f"👑 <b>الباقة:</b> {WEEKLY_LABEL}\n"
            + f"📅 <b>شغّالة لحد:</b> {fmt_ts(until)}\n\n"
            + _THANKS + "\n"
            + "✨ <b>مميزات اشتراكك:</b>\n"
            + "♾️ حل <b>غير محدود</b> طول مدة الاشتراك — مفيش عدّاد محاولات\n"
            + "⚡ حلّك بيبدأ <b>فورًا</b> من غير ما تقف في الطابور\n"
            + "🔓 تحل <b>أي حساب</b> على <b>أي منصة</b> (مش متقفل على حساب واحد)\n\n"
            + "🚀 اختار منصتك وابدأ دلوقتي!"
        )

    # single
    paid = max(0, ent.get("paid_attempts", 0))
    return (
        head
        + f"🎟️ <b>الباقة:</b> {SINGLE_LABEL}\n"
        + f"🎟️ <b>محاولاتك المشتراة:</b> {ar_attempts(paid)}\n\n"
        + _THANKS + "\n"
        + "✨ <b>مميزات اشتراكك:</b>\n"
        + _FEATURES_COMMON
        + "\n🚀 اختار منصتك وابدأ دلوقتي!"
    )


# ───────────────────────── بعد كل محاولة ناجحة ─────────────────────────

def attempt_done_text(info, ent=None, fmt_ts=None):
    """
    بلوك يتضاف على رسالة نهاية الحل الناجح. info = نتيجة db.consume_attempt.
    بيرجّع "" لو مفيش حاجة تتقال (محاولة مجانية/دعوات/مالك).
    """
    if not info or not info.get("consumed"):
        return ""
    kind = info.get("kind")
    fmt_ts = fmt_ts or str

    if kind == "vip":
        remaining = int(info.get("remaining", 0))
        granted = int(info.get("granted", remaining))
        used = int(info.get("used", granted - remaining))
        if remaining > 1:
            left_line = f"🎟️ فاضلك: <b>{ar_attempts(remaining)}</b> VIP"
        elif remaining == 1:
            left_line = "🎟️ فاضلك: <b>محاولة واحدة بس</b> — استغلّها كويس 😉"
        else:
            left_line = "🎟️ دي كانت <b>آخر محاولة</b> في اشتراكك"
        return (
            "🏆 <b>مبروووووك! خلّصت محاولة جديدة بنجاح</b> 🎉\n"
            + LINE + "\n"
            + f"✅ استخدمت: <b>{ar_num(used)}</b> من <b>{ar_num(granted)}</b>\n"
            + used_bar(used, granted) + "\n"
            + left_line
        )

    if kind == "single_paid":
        left = int(info.get("paid_remaining", 0))
        tail = (
            f"🎟️ فاضلك من محاولاتك المشتراة: <b>{ar_attempts(left)}</b>"
            if left > 0 else "🎟️ دي كانت <b>آخر محاولة مشتراة</b> عندك"
        )
        return "🏆 <b>مبروووووك! خلّصت محاولة بنجاح</b> 🎉\n" + LINE + "\n" + tail

    if kind == "vip_unlimited":
        until = (ent or {}).get("unlimited_until", 0)
        line = f"\n📅 اشتراكك المميز شغّال لحد <b>{fmt_ts(until)}</b>" if until else ""
        return "🏆 <b>مبروووووك! خلّصت المنصة بنجاح</b> 🎉\n" + LINE + line + "\n♾️ حلّك غير محدود طول مدة الاشتراك"

    return ""


def vip_pack_finished(info):
    """هل المحاولة دي كانت آخر محاولة في باقة الـ١٠؟"""
    return bool(info) and info.get("consumed") and info.get("kind") == "vip" and int(info.get("remaining", 0)) == 0


# ───────────────────────── انتهاء الاشتراك ─────────────────────────

def subscription_ended_text(info, other_attempts=0):
    """رسالة جميلة بعد آخر محاولة في باقة VIP. other_attempts = محاولات عادية لسه معاه."""
    granted = int(info.get("granted", 0)) if info else 0
    body = (
        "🌟 <b>خلّصت كل محاولات اشتراكك</b> 🌟\n" + LINE + "\n"
        + (f"✅ استخدمت <b>{ar_num(granted)} من {ar_num(granted)}</b> محاولة VIP\n"
           + used_bar(granted, granted) + "\n\n" if granted else "")
        + "💚 شكرًا إنك كنت معانا، ونتمنى إن التجربة كانت حلوة وساعدتك.\n"
    )
    if other_attempts > 0:
        body += f"🎟️ لسه معاك <b>{ar_attempts(other_attempts)}</b> عادية تقدر تستخدمها.\n"
    body += "🔄 لو الخدمة عجبتك، تقدر تجدد اشتراكك في أي وقت وتكمل بنفس المميزات 👇"
    return body


# ───────────────────────── لوحة المالك: مشتركو VIP ─────────────────────────

FILTER_TITLES = {"all": "الكل", "active": "النشطين", "done": "المنتهين"}


def vip_summary_text(summary, status="all"):
    return (
        "💎 <b>مشتركو VIP</b> — " + FILTER_TITLES.get(status, "الكل") + "\n" + LINE + "\n"
        f"👥 الإجمالي: <b>{ar_num(summary['total'])}</b> • "
        f"🟢 نشط: <b>{ar_num(summary['active'])}</b> • "
        f"🔴 منتهي: <b>{ar_num(summary['done'])}</b>\n"
        f"👑 أسبوع غير محدود شغّال حاليًا: <b>{ar_num(summary['weekly_active'])}</b>\n"
        f"🎟️ محاولات VIP: اتمنحت <b>{ar_num(summary['granted'])}</b> • "
        f"اتستخدمت <b>{ar_num(summary['used'])}</b> • "
        f"فاضلة <b>{ar_num(summary['remaining'])}</b>"
    )


def vip_status_lines(sub, fmt_ts=None):
    """سطور نوع الاشتراك والاستخدام لمشترك واحد (من db.get_vip_status / get_vip_subscribers)."""
    fmt_ts = fmt_ts or str
    lines = []
    if sub["packs"] or sub["remaining"]:
        packs = f" ({ar_num(sub['packs'])} باقات)" if sub["packs"] > 1 else ""
        lines.append(
            f"💎 VIP عادي{packs}: استخدم <b>{ar_num(sub['used'])}</b> من <b>{ar_num(sub['granted'])}</b>"
            f" • فاضل <b>{ar_num(sub['remaining'])}</b>"
        )
    if sub["weeklies"]:
        if sub["unlimited_active"]:
            lines.append(f"👑 VIP مميز (أسبوع غير محدود): شغّال حتى <b>{fmt_ts(sub['unlimited_until'])}</b>")
        else:
            ended = f" في {fmt_ts(sub['unlimited_expired_at'])}" if sub.get("unlimited_expired_at") else ""
            lines.append(f"👑 VIP مميز (أسبوع غير محدود): انتهى{ended}")
    return lines


def vip_row_text(index, sub, fmt_ts=None):
    icon = "🟢" if sub["active"] else "🔴"
    name = html.escape(sub["name"] or "مجهول")
    head = f"{ar_num(index)}. <b>{name}</b> — <code>{sub['tg_id']}</code> {icon}"
    return "\n".join([head] + ["   " + line for line in vip_status_lines(sub, fmt_ts)])
