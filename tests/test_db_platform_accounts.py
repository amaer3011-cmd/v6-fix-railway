import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


def test_platform_logins_are_saved_independently(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "accounts.db"))
        db.touch_user(42, "Student")
        db.save_username(42, "financial-id")
        db.save_username(42, "qureo-id", "qureo")

        assert db.get_username(42, "financial") == "financial-id"
        assert db.get_username(42, "qureo") == "qureo-id"
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_completed_account_is_owned_by_original_telegram_account(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "completed-accounts.db"))
        db.touch_user(801, "Original")
        db.touch_user(802, "Second")

        db.mark_account_completed(801, "STUDENT-123", "financial")
        owner = db.get_completed_account_owner("student-123")

        assert owner["tg_id"] == 801
        assert db.get_completed_account_owner("STUDENT-123")["tg_id"] == 801
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_seed_ready_codes(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "seed.db"))
        created = db.seed_ready_codes(300)
        assert created == {"single": 300, "vip": 300, "vip_unlimited": 300}
        assert len(db.get_codes("single", 500)) == 300
        assert len(db.get_codes("vip", 500)) == 300
        assert len(db.get_codes("vip_unlimited", 500)) == 300
        assert db.seed_ready_codes(300) == {"single": 0, "vip": 0, "vip_unlimited": 0}
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_attempt_settings_and_today_stats(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "stats.db"))
        db.touch_user(701, "Today User")
        db.log_job(701, int(time.time()), "finished", 5, 2)
        db.log_job(701, int(time.time()), "login_failed", 0, 0)
        db.log_job(701, int(time.time()), "stuck", 0, 0)

        db.set_setting("maintenance", "1")
        assert db.get_setting("maintenance") == "1"

        stats = db.get_global_stats()
        assert stats["joined_today"] == 1
        assert stats["today_success"] == 1
        assert stats["today_login_failed"] == 1
        assert stats["today_bot_failures"] == 1
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_attempt_is_not_reserved_before_success(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "attempt-success.db"))
        db.touch_user(601, "Success Student")

        assert db.has_attempt(601)
        assert db.get_entitlement(601)["attempts_remaining"] == 1

        result = db.consume_attempt(601)
        assert result["consumed"]
        assert db.get_entitlement(601)["attempts_remaining"] == 0
        assert not db.has_attempt(601)
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_admin_user_listing_and_blocking(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "admin.db"))
        db.touch_user(101, "User One")
        db.touch_user(102, "User Two")

        users = db.get_recent_users(10)
        assert len(users) == 2
        assert db.get_user_count() == 2

        db.set_user_blocked(101, True, "admin test")
        assert db.is_user_blocked(101)
        assert db.get_user_detail(101)["blocked"] is not None

        db.set_user_blocked(101, False)
        assert not db.is_user_blocked(101)
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_admin_search_and_recent_jobs(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "admin-search.db"))
        db.touch_user(201, "Alice Example")
        db.touch_user(202, "Bob Example")
        db.log_job(201, int(time.time()), "finished", 12, 3, duration=45)

        assert [row["user_id"] for row in db.search_users("Alice")] == [201]
        assert [row["user_id"] for row in db.search_users("202")] == [202]
        jobs = db.get_recent_jobs()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "finished"
        assert jobs[0]["questions"] == 12

        stats = db.get_global_stats()
        assert stats["weekly_jobs"] == 1
        assert stats["weekly_finished"] == 1
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_required_channels_storage(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "required-channels.db"))
        db.add_required_channel("-100123", "قناة الاختبار", "https://t.me/test_channel")

        channels = db.get_required_channels()
        assert len(channels) == 1
        assert channels[0]["chat_id"] == "-100123"
        assert channels[0]["title"] == "قناة الاختبار"
        assert channels[0]["link"] == "https://t.me/test_channel"

        db.remove_required_channel("-100123")
        assert db.get_required_channels() == []
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection


def test_attempt_codes_and_referral_rewards(tmp_path):
    previous_connection = db._conn
    try:
        db.init(str(tmp_path / "subscriptions.db"))
        db.touch_user(301, "Student")

        single = db.generate_code("single")
        vip = db.generate_code("vip")
        unlimited = db.generate_code("vip_unlimited")

        assert db.redeem_code(single, 301, "Student")["ok"]
        ent = db.get_entitlement(301)
        assert ent["attempts_remaining"] == 2

        assert db.redeem_code(vip, 301, "Student")["ok"]
        ent = db.get_entitlement(301)
        assert ent["vip_attempts_remaining"] == 10

        assert db.redeem_code(unlimited, 301, "Student")["ok"]
        ent = db.get_entitlement(301)
        assert ent["unlimited_until"] > int(time.time())

        assert not db.redeem_code(single, 301, "Student")["ok"]

        db.touch_user(401, "Inviter")
        for invited_id in range(501, 506):
            db.touch_user(invited_id, f"Invite {invited_id}")
            db.register_referral(401, invited_id)

        ent = db.get_entitlement(401)
        assert ent["attempts_remaining"] == 2
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = previous_connection
