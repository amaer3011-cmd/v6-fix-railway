"""المحاولة المجانية لكل حساب على المنصة + توليد الأكواد بكميات."""
import time

import pytest

import db


@pytest.fixture()
def conn(tmp_path):
    db.init(str(tmp_path / "t.db"))
    for uid in (1, 2, 3):
        db.touch_user(uid, f"u{uid}", f"u{uid}")
    return db


def test_free_trial_is_one_per_platform_account(conn):
    assert db.check_account_access(1, "financial", "ACC-1")["ok"] is True
    assert db.consume_attempt(1, "financial", "ACC-1") == {"consumed": True, "kind": "single", "remaining": 0}
    # أكونت تيليجرام جديد بنفس حساب المنصة: مرفوض (حتى لو كتب الكود بحروف مختلفة)
    verdict = db.check_account_access(2, "financial", "acc-1 ")
    assert verdict == {"ok": False, "reason": "account_used"}
    assert db.consume_attempt(2, "financial", "ACC-1")["consumed"] is False
    assert db.get_entitlement(2)["attempts_remaining"] == 1          # محاولته ما اتخصمتش
    # نفس الشخص على حساب منصة تاني: مسموح
    assert db.check_account_access(2, "financial", "ACC-2")["ok"] is True
    # منصة تانية بنفس الـ ID: مسموح
    assert db.check_account_access(2, "qureo", "ACC-1")["ok"] is True


def test_purchased_code_attempt_works_on_used_account(conn):
    db.consume_attempt(1, "financial", "ACC-1")
    code = db.generate_code("single")
    assert db.redeem_code(code, 2)["ok"] is True
    assert db.check_account_access(2, "financial", "ACC-1") == {"ok": True, "kind": "paid"}
    res = db.consume_attempt(2, "financial", "ACC-1")
    assert res["consumed"] is True and res["kind"] == "single_paid"
    ent = db.get_entitlement(2)
    assert ent["attempts_remaining"] == 1 and ent["paid_attempts"] == 0   # المجانية لسه معاه
    # والمجانية دي تشتغل على حساب جديد
    assert db.check_account_access(2, "financial", "ACC-9")["ok"] is True
    # والحساب القديم يفضل مقفول قدام المجانية
    assert db.check_account_access(2, "financial", "ACC-1")["ok"] is False


def test_qureo_python_and_javascript_share_the_same_account(conn):
    db.consume_attempt(1, "qureo", "464635969")
    assert db.check_account_access(2, "javascript", "464635969")["reason"] == "account_used"


def test_vip_codes_work_on_any_account(conn):
    db.consume_attempt(1, "financial", "ACC-1")
    db.redeem_code(db.generate_code("vip"), 2)
    assert db.check_account_access(2, "financial", "ACC-1")["ok"] is True
    assert db.consume_attempt(2, "financial", "ACC-1")["kind"] == "vip"


def test_same_owner_can_retry_own_account(conn):
    db.consume_attempt(1, "financial", "ACC-1")
    db.redeem_code(db.generate_code("single"), 1)
    assert db.check_account_access(1, "financial", "ACC-1")["ok"] is True


def test_legacy_completed_accounts_are_backfilled(tmp_path):
    path = str(tmp_path / "old.db")
    db.init(path)
    db.touch_user(1, "a", "a"); db.touch_user(2, "b", "b")
    db.mark_account_completed(1, "OldAcc", "financial")
    db.init(path)                                   # إعادة تشغيل = الـ backfill بيشتغل
    assert db.check_account_access(2, "financial", "oldacc")["reason"] == "account_used"


def test_bulk_codes_are_fast_unique_and_redeemable(conn):
    t0 = time.perf_counter()
    codes = db.generate_codes_bulk("single", 5000)
    assert time.perf_counter() - t0 < 5
    assert len(codes) == len(set(codes)) == 5000 and all(c.startswith("TRY-") for c in codes)
    assert db.generate_codes_bulk("vip", 3)[0].startswith("VIP-")
    assert db.redeem_code(codes[0], 3)["ok"] is True
    assert db.redeem_code(codes[0], 3) == {"ok": False, "reason": "used"}
    assert len(db.generate_codes_bulk("vip_unlimited", 10**9)) == db.MAX_BULK_CODES
    with pytest.raises(ValueError):
        db.generate_codes_bulk("nope", 5)


def test_free_trial_locked_to_first_account_entered(conn):
    assert db.claim_free_account(1, "financial", "ACC-1")["claimed"] is True
    # حساب تاني بالمجانية: مرفوض حتى قبل ما الأول يخلص
    assert db.check_account_access(1, "financial", "ACC-2") == {"ok": False, "reason": "other_account"}
    assert db.check_account_access(1, "qureo", "ACC-1") == {"ok": False, "reason": "other_account"}
    # الأول لسه مسموح
    assert db.check_account_access(1, "financial", "acc-1")["ok"] is True
    # وأكونت تاني ما ياخدش نفس الحساب (حتى والأول لسه شغال)
    assert db.check_account_access(2, "financial", "ACC-1") == {"ok": False, "reason": "account_used"}
    assert db.claim_free_account(2, "financial", "ACC-1")["claimed"] is False


def test_bought_code_unlocks_any_account(conn):
    db.claim_free_account(1, "financial", "ACC-1")
    db.consume_attempt(1, "financial", "ACC-1")
    db.redeem_code(db.generate_code("single"), 1)
    assert db.check_account_access(1, "financial", "ACC-2") == {"ok": True, "kind": "paid"}
    assert db.consume_attempt(1, "financial", "ACC-2")["kind"] == "single_paid"


def test_unlimited_unlocks_any_account(conn):
    db.claim_free_account(1, "financial", "ACC-1")
    db.redeem_code(db.generate_code("vip_unlimited"), 1)
    assert db.check_account_access(1, "financial", "ACC-7")["ok"] is True
    assert db.claim_free_account(1, "financial", "ACC-7")["claimed"] is False


def test_failed_login_releases_claim(conn):
    db.claim_free_account(1, "financial", "WRONG")
    db.release_free_claim(1, "financial", "WRONG")
    assert db.check_account_access(1, "financial", "REAL")["ok"] is True
    assert db.check_account_access(2, "financial", "WRONG")["ok"] is True


def test_referral_progress_info(conn):
    for u in range(10, 20):
        db.touch_user(u, "x", "x")
    r = db.register_referral(1, 10)
    assert (r["counted"], r["progress"], r["remaining"], r["new_rewards"]) == (True, 1, 4, 0)
    assert db.register_referral(1, 10)["counted"] is False          # نفس الشخص تاني: مايتعدّش
    assert db.register_referral(1, 1)["counted"] is False           # دعوة نفسه: مايتعدّش
    for u in (11, 12, 13):
        r = db.register_referral(1, u)
    assert (r["progress"], r["remaining"]) == (4, 1)
    r = db.register_referral(1, 14)
    assert r["new_rewards"] == 1 and r["remaining"] == 5 and r["count"] == 5


def test_success_rate_in_global_stats(conn):
    now = int(time.time())
    assert db.get_global_stats()["rate_today"] is None            # مفيش عمليات
    for status in ("finished", "finished", "finished", "stuck", "login_failed", "stopped"):
        db.log_job(1, now, status, 0, 0, "")
    g = db.get_global_stats()
    assert g["today_jobs"] == 6
    assert g["rate_today"] == 75                                  # 3 من 4 (الدخول الخاطئ والإيقاف مش بيتحسبوا)
    assert g["rate_week"] == 75 and g["rate_total"] == 75
