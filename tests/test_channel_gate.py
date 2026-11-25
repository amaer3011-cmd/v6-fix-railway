"""بوابة الاشتراك الإجباري: لازم المستخدم يشوف رسالة الاشتراك قبل أي استخدام، وما يسكتش البوت أبدًا."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram.ext import ApplicationHandlerStop, TypeHandler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LEARNED_FILE", str(Path(tempfile.mkdtemp()) / "l.json"))

import bot
import db

OWNER = 999
USER = 5
CH1 = "-1004376502752"
CH2 = "-1003822687707"
VALID_STYLES = (None, "primary", "success", "danger")


def run(coro):
    return asyncio.run(coro)


class StrictChat:
    """شات بيرفض الرسايل زي ما تيليجرام بيرفضها (style غلط، رابط زرار مش صالح، HTML غلط)."""
    type = "private"

    def __init__(self, reject_html=False):
        self.sent = []
        self.reject_html = reject_html

    async def send_message(self, text, parse_mode=None, reply_markup=None, **kw):
        if self.reject_html and parse_mode:
            raise BadRequest("Can't parse entities")
        for row in getattr(reply_markup, "inline_keyboard", []):
            for b in row:
                if getattr(b, "style", None) not in VALID_STYLES:
                    raise BadRequest(f"Bad Request: BUTTON_STYLE_INVALID ({b.style})")
                if b.url is not None and not b.url.startswith(("http://", "https://", "tg://")):
                    raise BadRequest("Wrong http url")
        self.sent.append(SimpleNamespace(text=text, parse_mode=parse_mode, markup=reply_markup))
        return SimpleNamespace(message_id=1)


class FakeBot:
    def __init__(self, members=None, errors=None, chats=None):
        self.members = members or {}            # (chat_id, uid) -> status
        self.errors = errors or {}              # chat_id -> exception
        self.chats = chats or {}
        self.member_calls = 0
        self.messages = []                      # (chat_id, text)

    async def get_chat_member(self, chat_id, uid):
        self.member_calls += 1
        if chat_id in self.errors:
            raise self.errors[chat_id]
        return SimpleNamespace(status=self.members.get((chat_id, uid), "left"), is_member=False)

    async def get_chat(self, chat_id):
        return self.chats.get(chat_id) or (_ for _ in ()).throw(BadRequest("Chat not found"))

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


def make_app(tg):
    return SimpleNamespace(bot=tg)


def make_update(chat, text="/start", data=None, user_id=USER, chat_type="private"):
    chat.type = chat_type
    user = SimpleNamespace(id=user_id, full_name="Mohamed", first_name="Mohamed", username="mohamed")
    query = None
    message = None
    if data is not None:
        query = SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(chat=chat, reply_text=AsyncMock()),
                                from_user=user)
    else:
        message = SimpleNamespace(text=text)
    return SimpleNamespace(effective_chat=chat, effective_user=user, message=message, callback_query=query)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.init(str(tmp_path / "t.db"))
    monkeypatch.setattr(bot, "OWNER_CHAT_ID", OWNER)
    bot._sub_ok.clear()
    bot._gate_warned.clear()
    db.add_required_channel(CH1, "𝑨𝒉𝒎𝒆𝒅'𝒔 𝑺𝒑𝒂𝒄𝒆", "https://t.me/ahmeds_space")
    db.add_required_channel(CH2, "𝑩𝒐𝒕 𝑼𝒑𝒅𝒂𝒕𝒆𝒔♥️", "https://t.me/bot_updates")
    db.set_setting("required_join_message", "محتاجينك تشترك في القنوات دي ياحب ❤️🌿")
    return db


def ctx_for(tg):
    return SimpleNamespace(application=make_app(tg), user_data={})


# ── سبب المشكلة الأصلية ───────────────────────────────────────
def test_url_button_has_no_invalid_style():
    button = bot.btn("قناة", "url", url="https://t.me/x")
    assert getattr(button, "style", None) in VALID_STYLES
    assert bot.btn("ok", "success", callback_data="x").style == "success"


def test_gate_message_reaches_non_member_with_valid_buttons(env):
    chat, tg = StrictChat(), FakeBot()
    allowed = run(bot.send_channel_gate(make_app(tg), chat, USER))
    assert allowed is False
    assert len(chat.sent) == 1, "المستخدم لازم يستلم رسالة الاشتراك"
    sent = chat.sent[0]
    assert "محتاجينك تشترك" in sent.text
    urls = [b.url for row in sent.markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/ahmeds_space", "https://t.me/bot_updates"]
    assert sent.markup.inline_keyboard[-1][0].callback_data == "check_channels"


def test_only_missing_channels_are_listed(env):
    chat, tg = StrictChat(), FakeBot(members={(CH1, USER): "member"})
    assert run(bot.send_channel_gate(make_app(tg), chat, USER)) is False
    urls = [b.url for row in chat.sent[0].markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/bot_updates"]


def test_member_of_all_passes_and_is_cached(env):
    tg = FakeBot(members={(CH1, USER): "member", (CH2, USER): "administrator"})
    chat = StrictChat()
    assert run(bot.ensure_subscribed(make_app(tg), chat, USER)) is True
    calls = tg.member_calls
    assert run(bot.ensure_subscribed(make_app(tg), chat, USER)) is True
    assert tg.member_calls == calls, "التحقق التاني لازم يكون من الكاش"
    assert chat.sent == []


# ── البوابة العامة: قبل أي معالج ────────────────────────────────
def test_non_member_start_gets_gate_and_handlers_are_stopped(env):
    chat, tg = StrictChat(), FakeBot()
    update = make_update(chat, "/start")
    with pytest.raises(ApplicationHandlerStop):
        run(bot.on_gate_update(update, ctx_for(tg)))
    assert len(chat.sent) == 1 and "محتاجينك تشترك" in chat.sent[0].text


def test_non_member_text_and_menu_buttons_are_blocked_too(env):
    tg = FakeBot()
    for kwargs in ({"text": "hello"}, {"data": "plans"}, {"data": "solve_qureo"}, {"data": "mystats"}):
        chat = StrictChat()
        update = make_update(chat, **kwargs)
        with pytest.raises(ApplicationHandlerStop):
            run(bot.on_gate_update(update, ctx_for(tg)))
        assert chat.sent, kwargs
        if update.callback_query:
            update.callback_query.answer.assert_awaited()


def test_member_passes_gate(env):
    tg = FakeBot(members={(CH1, USER): "member", (CH2, USER): "creator"})
    chat = StrictChat()
    run(bot.on_gate_update(make_update(chat, "/start"), ctx_for(tg)))    # من غير ApplicationHandlerStop
    assert chat.sent == []


def test_owner_and_disabled_and_groups_and_check_button_are_exempt(env):
    tg = FakeBot()
    chat = StrictChat()
    run(bot.on_gate_update(make_update(chat, "/start", user_id=OWNER), ctx_for(tg)))
    run(bot.on_gate_update(make_update(chat, data="check_channels"), ctx_for(tg)))
    run(bot.on_gate_update(make_update(chat, "hi", chat_type="supergroup"), ctx_for(tg)))
    db.set_setting("required_channels_enabled", "0")
    run(bot.on_gate_update(make_update(chat, "/start"), ctx_for(tg)))
    assert chat.sent == [] and tg.member_calls == 0


def test_no_channels_configured_means_no_gate(env):
    for ch in (CH1, CH2):
        db.remove_required_channel(ch)
    chat, tg = StrictChat(), FakeBot()
    run(bot.on_gate_update(make_update(chat, "/start"), ctx_for(tg)))
    assert chat.sent == [] and tg.member_calls == 0


def test_new_user_registered_and_owner_told_once(env):
    chat, tg = StrictChat(), FakeBot()
    for _ in range(2):
        with pytest.raises(ApplicationHandlerStop):
            run(bot.on_gate_update(make_update(chat, "/start"), ctx_for(tg)))
    notices = [t for cid, t in tg.messages if cid == OWNER and "عضو جديد" in t]
    assert len(notices) == 1
    assert db.touch_user(USER, "Mohamed", "mohamed") is False       # اتسجّل فعلًا


def test_gate_crash_fails_open(env, monkeypatch):
    monkeypatch.setattr(bot, "_gate_blocks", AsyncMock(side_effect=RuntimeError("boom")))
    run(bot.on_gate_update(make_update(StrictChat(), "/start"), ctx_for(FakeBot())))   # ما يرميش ولا يوقف


# ── بعد الاشتراك ───────────────────────────────────────────────
def test_check_button_welcomes_after_joining_and_applies_pending_referral(env, monkeypatch):
    welcome, referral = AsyncMock(), AsyncMock()
    monkeypatch.setattr(bot, "send_welcome", welcome)
    monkeypatch.setattr(bot, "_apply_referral", referral)
    tg = FakeBot()
    ctx = ctx_for(tg)
    chat = StrictChat()
    with pytest.raises(ApplicationHandlerStop):
        run(bot.on_gate_update(make_update(chat, "/start ref_123"), ctx))
    assert ctx.user_data["pending_ref"] == "ref_123"

    tg.members.update({(CH1, USER): "member", (CH2, USER): "member"})
    update = make_update(chat, data="check_channels")
    run(bot.on_check_channels(update, ctx))
    welcome.assert_awaited_once()
    referral.assert_awaited_once()
    assert referral.await_args.args[2] == "ref_123"
    assert "pending_ref" not in ctx.user_data


def test_check_button_resends_gate_if_still_missing(env, monkeypatch):
    welcome = AsyncMock()
    monkeypatch.setattr(bot, "send_welcome", welcome)
    chat, tg = StrictChat(), FakeBot(members={(CH1, USER): "member"})
    run(bot.on_check_channels(make_update(chat, data="check_channels"), ctx_for(tg)))
    welcome.assert_not_awaited()
    assert len(chat.sent) == 1


# ── مقاومة الأخطاء ─────────────────────────────────────────────
def test_misconfigured_channel_is_skipped_and_owner_warned_once(env):
    tg = FakeBot(
        members={(CH2, USER): "member"},
        errors={CH1: BadRequest("Bad Request: chat not found")},
    )
    chat = StrictChat()
    assert run(bot.ensure_subscribed(make_app(tg), chat, USER)) is True      # القناة الغلط ما تقفلش البوت
    bot._sub_ok.clear()
    assert run(bot.ensure_subscribed(make_app(tg), chat, USER)) is True
    warns = [t for cid, t in tg.messages if cid == OWNER and "مش قادر يتحقق" in t]
    assert len(warns) == 1


def test_forbidden_is_config_error_but_network_is_not(env):
    assert bot._is_channel_config_error(Forbidden("bot was kicked"))
    assert bot._is_channel_config_error(BadRequest("Bad Request: member list is inaccessible"))
    assert not bot._is_channel_config_error(NetworkError("timeout"))
    assert not bot._is_channel_config_error(BadRequest("Bad Request: user not found"))


def test_transient_error_counts_as_not_joined(env):
    tg = FakeBot(errors={CH1: NetworkError("timeout")}, members={(CH2, USER): "member"})
    chat = StrictChat()
    assert run(bot.send_channel_gate(make_app(tg), chat, USER)) is False
    assert chat.sent


def test_invalid_stored_link_falls_back_to_chat_username(env):
    db.add_required_channel(CH1, "A", "")
    db.add_required_channel(CH2, "B", "not a link")
    tg = FakeBot(chats={CH1: SimpleNamespace(username="real_user", invite_link=None),
                        CH2: SimpleNamespace(username=None, invite_link="https://t.me/+abc")})
    chat = StrictChat()
    assert run(bot.send_channel_gate(make_app(tg), chat, USER)) is False
    urls = sorted(b.url for row in chat.sent[0].markup.inline_keyboard for b in row if b.url)
    assert urls == ["https://t.me/+abc", "https://t.me/real_user"]


def test_message_still_delivered_when_no_link_can_be_found(env):
    db.add_required_channel(CH1, "A", "")
    db.add_required_channel(CH2, "B", "")
    chat, tg = StrictChat(), FakeBot()          # get_chat بيفشل → مفيش روابط
    assert run(bot.send_channel_gate(make_app(tg), chat, USER)) is False
    assert chat.sent and chat.sent[0].markup.inline_keyboard[-1][0].callback_data == "check_channels"
    assert "• A" in chat.sent[0].text or "محتاجينك" in chat.sent[0].text


def test_bad_html_in_custom_message_falls_back_to_plain_text(env):
    db.set_setting("required_join_message", "اشترك <في القنوات")
    chat, tg = StrictChat(reject_html=True), FakeBot()
    assert run(bot.send_channel_gate(make_app(tg), chat, USER)) is False
    assert len(chat.sent) == 1 and chat.sent[0].parse_mode is None


@pytest.mark.parametrize("link,expected", [
    ("https://t.me/x", "https://t.me/x"), ("@chan", "https://t.me/chan"), ("t.me/chan", "https://t.me/chan"),
    ("", ""), ("javascript:alert(1)", ""), ("chan", ""),
])
def test_normalize_link(link, expected):
    assert bot._normalize_link(link) == expected


def test_admin_changes_reset_cache(env):
    bot._sub_ok[USER] = 10 ** 12
    bot.reset_sub_cache()
    assert bot._sub_ok == {}


# ── التسجيل ────────────────────────────────────────────────────
def test_gate_registered_before_all_other_handlers(monkeypatch):
    monkeypatch.setattr(bot, "BOT_TOKEN", "123456:TEST-token")
    app = bot.build_app()
    first_group = min(app.handlers)
    assert first_group == -1
    handler = app.handlers[-1][0]
    assert isinstance(handler, TypeHandler) and handler.callback is bot.on_gate_update
