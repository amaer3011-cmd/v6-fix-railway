"""رسائل الاشتراك بعد كل محاولة + انتهاء الباقة + لوحة مشتركي VIP عند المالك."""
import time

import pytest

import db
import subscription_texts as st


@pytest.fixture()
def conn(tmp_path):
    db.init(str(tmp_path / "t.db"))
    for uid, name in ((1, "Ahmed"), (2, "Mona"), (3, "Omar"), (4, "Free <b>User</b>")):
        db.touch_user(uid, name, f"u{uid}")
    return db


def _redeem(uid, code_type, name="N"):
    assert db.redeem_code(db.generate_code(code_type), uid, name)["ok"] is True


# ───────────────────────── المحاولات بعد كل حل ناجح ─────────────────────────

def test_vip_pack_reports_used_and_remaining_after_each_attempt(conn):
    _redeem(1, "vip")
    first = db.consume_attempt(1, "financial", "A-1")
    assert first == {"consumed": True, "kind": "vip", "remaining": 9, "granted": 10, "used": 1}
    for _ in range(2):
        last = db.consume_attempt(1, "qureo", "A-2")
    assert (last["used"], last["remaining"], last["granted"]) == (3, 7, 10)


def test_last_vip_attempt_is_flagged_as_pack_finished(conn):
    _redeem(1, "vip")
    results = [db.consume_attempt(1, "financial", f"A-{i}") for i in range(10)]
    assert [r["remaining"] for r in results] == list(range(9, -1, -1))
    assert not any(st.vip_pack_finished(r) for r in results[:-1])
    assert st.vip_pack_finished(results[-1]) is True
    assert db.get_entitlement(1)["vip_attempts_remaining"] == 0


def test_two_packs_count_as_twenty_attempts(conn):
    _redeem(1, "vip")
    _redeem(1, "vip")
    res = db.consume_attempt(1, "financial", "A-1")
    assert (res["used"], res["granted"], res["remaining"]) == (1, 20, 19)


def test_old_subscription_without_history_never_reports_negative_usage(conn):
    # اشتراك قديم: رصيد VIP موجود من غير سجل كود (مثلًا اتضاف يدويًا)
    with db._lock:
        db._conn.execute("INSERT OR IGNORE INTO entitlements(tg_id) VALUES (9)")
        db._conn.execute("UPDATE entitlements SET vip_attempts_remaining=5 WHERE tg_id=9")
        db._conn.commit()
    res = db.consume_attempt(9, "financial", "A-1")
    assert res["remaining"] == 4 and res["used"] >= 0 and res["granted"] >= res["remaining"]


def test_paid_single_attempt_reports_how_many_are_left(conn):
    _redeem(2, "single")
    _redeem(2, "single")
    # المحاولة المجانية بتتصرف الأول، فنخلصها
    db.consume_attempt(2, "financial", "FREE-1")
    res = db.consume_attempt(2, "financial", "ACC-X")
    assert res["kind"] == "single_paid" and res["paid_remaining"] == 1


# ───────────────────────── الأسبوع غير المحدود ─────────────────────────

def test_second_weekly_code_extends_instead_of_resetting(conn):
    _redeem(1, "vip_unlimited")
    first = db.get_entitlement(1)["unlimited_until"]
    _redeem(1, "vip_unlimited")
    second = db.get_entitlement(1)["unlimited_until"]
    assert second - first == db.UNLIMITED_DAYS * 86400


def test_expired_weekly_restarts_from_now(conn):
    _redeem(1, "vip_unlimited")
    with db._lock:
        db._conn.execute("UPDATE entitlements SET unlimited_until=? WHERE tg_id=1", (int(time.time()) - 5000,))
        db._conn.commit()
    _redeem(1, "vip_unlimited")
    left = db.get_entitlement(1)["unlimited_until"] - int(time.time())
    assert abs(left - db.UNLIMITED_DAYS * 86400) < 5


# ───────────────────────── لوحة المالك: مشتركو VIP ─────────────────────────

def test_admin_lists_only_vip_subscribers_with_usage_and_type(conn):
    _redeem(1, "vip", "Ahmed")
    for i in range(4):
        db.consume_attempt(1, "financial", f"A-{i}")
    _redeem(2, "vip_unlimited", "Mona")
    _redeem(3, "vip", "Omar")
    for i in range(10):
        db.consume_attempt(3, "financial", f"O-{i}")
    _redeem(4, "single", "FreeUser")           # كود محاولة واحدة: مش VIP

    by_id = {s["tg_id"]: s for s in db.get_vip_subscribers("all")}
    assert set(by_id) == {1, 2, 3}
    assert (by_id[1]["used"], by_id[1]["remaining"], by_id[1]["granted"]) == (4, 6, 10)
    assert by_id[1]["active"] is True and by_id[1]["weeklies"] == 0
    assert by_id[2]["weeklies"] == 1 and by_id[2]["unlimited_active"] is True and by_id[2]["packs"] == 0
    assert (by_id[3]["used"], by_id[3]["remaining"]) == (10, 0) and by_id[3]["active"] is False

    assert {s["tg_id"] for s in db.get_vip_subscribers("active")} == {1, 2}
    assert {s["tg_id"] for s in db.get_vip_subscribers("done")} == {3}
    # النشطين الأول
    assert db.get_vip_subscribers("all")[-1]["tg_id"] == 3


def test_vip_summary_totals(conn):
    _redeem(1, "vip")
    db.consume_attempt(1, "financial", "A-1")
    _redeem(2, "vip_unlimited")
    _redeem(3, "vip")
    summary = db.get_vip_summary()
    assert summary["total"] == 3 and summary["active"] == 3 and summary["done"] == 0
    assert summary["weekly_active"] == 1
    assert (summary["granted"], summary["used"], summary["remaining"]) == (20, 1, 19)


def test_expired_weekly_shows_as_finished(conn):
    _redeem(2, "vip_unlimited")
    with db._lock:
        db._conn.execute("UPDATE entitlements SET unlimited_until=? WHERE tg_id=2", (int(time.time()) - 60,))
        db._conn.commit()
    sub = db.get_vip_status(2)
    assert sub["active"] is False and sub["unlimited_active"] is False
    assert db.get_vip_summary()["done"] == 1


def test_vip_status_is_none_for_non_vip(conn):
    _redeem(4, "single")
    assert db.get_vip_status(4) is None
    assert db.get_vip_status(999) is None


# ───────────────────────── نصوص الرسائل ─────────────────────────

def test_arabic_attempt_plurals():
    assert st.ar_attempts(0) == "صفر محاولات"
    assert st.ar_attempts(1) == "محاولة واحدة"
    assert st.ar_attempts(2) == "محاولتين"
    assert st.ar_attempts(7) == "٧ محاولات"
    assert st.ar_attempts(20) == "٢٠ محاولة"


def test_redeem_messages_show_joy_thanks_and_features(conn):
    _redeem(1, "vip")
    text = st.redeem_success_text("vip", db.get_entitlement(1))
    assert "مبروووووك" in text and "شكرًا" in text and "تعجبك" in text
    assert "مميزات اشتراكك" in text and "من غير ما تقف في الطابور" in text
    assert "١٠ محاولات" in text

    _redeem(2, "vip_unlimited")
    weekly = st.redeem_success_text("vip_unlimited", db.get_entitlement(2), lambda ts: "DATE")
    assert "مبروووووك" in weekly and "DATE" in weekly and "غير محدود" in weekly

    _redeem(3, "single")
    single = st.redeem_success_text("single", db.get_entitlement(3))
    assert "مبروووووك" in single and "مميزات اشتراكك" in single


def test_congrats_after_a_successful_attempt_shows_used_and_remaining(conn):
    _redeem(1, "vip")
    for i in range(3):
        info = db.consume_attempt(1, "financial", f"A-{i}")
    text = st.attempt_done_text(info)
    assert "مبروووووك" in text and "٣</b> من <b>١٠" in text and "٧ محاولات" in text


def test_second_to_last_and_last_attempt_wording(conn):
    _redeem(1, "vip")
    for i in range(9):
        info = db.consume_attempt(1, "financial", f"A-{i}")
    assert "محاولة واحدة بس" in st.attempt_done_text(info)
    info = db.consume_attempt(1, "financial", "A-last")
    assert "آخر محاولة" in st.attempt_done_text(info)


def test_nothing_extra_for_free_or_failed_attempts(conn):
    assert st.attempt_done_text(None) == ""
    assert st.attempt_done_text({"consumed": False, "kind": "none"}) == ""
    assert st.attempt_done_text({"consumed": True, "kind": "single", "remaining": 0}) == ""


def test_subscription_ended_message(conn):
    _redeem(1, "vip")
    for i in range(10):
        info = db.consume_attempt(1, "financial", f"A-{i}")
    text = st.subscription_ended_text(info)
    assert "خلّصت كل محاولات اشتراكك" in text and "١٠ من ١٠" in text and "شكرًا" in text and "تجدد" in text
    assert "عادية" not in text                                   # مفيش محاولات تانية: السطر ما بيظهرش
    assert "محاولتين</b> عادية" in st.subscription_ended_text(info, 2)   # لسه معاه محاولتين عاديتين


def test_admin_row_text_escapes_names_and_shows_type(conn):
    _redeem(4, "vip", "x")
    db.touch_user(4, "<b>Evil</b>", "evil")
    sub = db.get_vip_status(4)
    row = st.vip_row_text(1, sub)
    assert "<b>Evil</b>" not in row and "&lt;b&gt;Evil" in row
    assert "VIP عادي" in row and "استخدم" in row and "فاضل" in row
