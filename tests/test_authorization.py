from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.bot.authorization import is_admin
from app.config import Settings
from app.database.models import AppSetting, AuditEvent, Draft, Publication, PublicationAttempt, Source
from app.timeutil import dump_datetime, utc_now
from tests.test_core import sample_rows


def _install_telegram_import_stubs() -> None:
    """Allow handler authorization tests without requiring Telegram network access."""
    try:
        importlib.import_module("telegram")
        return
    except ImportError:
        pass
    telegram = ModuleType("telegram")
    telegram.Update = type("Update", (), {})
    telegram.InlineKeyboardButton = type("InlineKeyboardButton", (), {
        "__init__": lambda self, *args, **kwargs: None})
    telegram.InlineKeyboardMarkup = type("InlineKeyboardMarkup", (), {
        "__init__": lambda self, *args, **kwargs: None})
    ext = ModuleType("telegram.ext")
    for name in ("Application", "CallbackQueryHandler", "CommandHandler", "MessageHandler"):
        setattr(ext, name, type(name, (), {}))
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    ext.filters = SimpleNamespace(TEXT=object(), COMMAND=object())
    telegram.ext = ext
    sys.modules["telegram"] = telegram
    sys.modules["telegram.ext"] = ext


_install_telegram_import_stubs()
from app.bot.application import draft_callback, draft_command, pause, reconcile_command, source_callback


def _context(sessions, args=()):
    settings = Settings(_env_file=None, telegram_admin_ids="123")
    bot = SimpleNamespace(send_message=AsyncMock())
    application = SimpleNamespace(bot_data={"settings": settings, "sessions": sessions}, bot=bot)
    return SimpleNamespace(application=application, args=list(args))


def _message_update(user_id=123, chat_type="private", text=""):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           effective_chat=SimpleNamespace(type=chat_type),
                           effective_message=message, callback_query=None)


def _callback_update(data, user_id=123, chat_type="private"):
    query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(effective_user=query.from_user,
                           effective_chat=SimpleNamespace(type=chat_type),
                           effective_message=None, callback_query=query)


def _unknown_attempt(sessions):
    with sessions() as session:
        _, _, draft = sample_rows(session)
        now = dump_datetime(utc_now())
        draft.draft_state, draft.approved_by, draft.approved_at = "publishing", 123, now
        attempt = PublicationAttempt(draft_id=draft.id, destination_id="private-test",
                                     attempt_number=1, state="unknown", started_at=now,
                                     completed_at=now)
        session.add(attempt)
        session.commit()
        return attempt.id, draft.id


def _snapshot(sessions, draft_id, attempt_id):
    with sessions() as session:
        draft, attempt = session.get(Draft, draft_id), session.get(PublicationAttempt, attempt_id)
        return (draft.draft_state, attempt.state, attempt.telegram_message_id,
                session.scalar(select(func.count(Publication.id))),
                session.scalar(select(func.count(AuditEvent.id))))


def test_admin_allowlist_is_exact_and_default_denies():
    settings = Settings(_env_file=None, telegram_admin_ids="123,456")
    assert is_admin(123, settings)
    assert is_admin(999, settings) is False
    assert is_admin(None, settings) is False


@pytest.mark.parametrize("args", [
    [], ["found"], ["found", "123"], ["1"], ["1", "found"],
    ["1", "absent", "extra"], ["1", "anything"],
    ["1", "found", "abc"], ["1", "found", "0"],
    ["1", "found", "+12"], ["1", "found", "１２"],
    ["1", "found", "9" * 5000],
    ["not-an-id", "absent"],
])
def test_invalid_reconciliation_commands_do_not_mutate_database(sessions, args):
    attempt_id, draft_id = _unknown_attempt(sessions)
    before = _snapshot(sessions, draft_id, attempt_id)
    update = _message_update(text="/reconcile " + " ".join(args))
    import asyncio
    asyncio.run(reconcile_command(update, _context(sessions, args)))
    update.effective_message.reply_text.assert_awaited_once()
    assert _snapshot(sessions, draft_id, attempt_id) == before


@pytest.mark.parametrize("suffix,expected", [
    (["found", "98765"], "published"), (["absent"], "scheduled"),
])
def test_valid_reconciliation_syntax_is_preserved(sessions, suffix, expected):
    attempt_id, draft_id = _unknown_attempt(sessions)
    args = [str(attempt_id), *suffix]
    update = _message_update(text="/reconcile " + " ".join(args))
    import asyncio
    asyncio.run(reconcile_command(update, _context(sessions, args)))
    assert _snapshot(sessions, draft_id, attempt_id)[0] == expected


@pytest.mark.parametrize("command_text,args,handler", [
    ("/approve 1", ["1"], draft_command),
    ("/reject 1 reason", ["1", "reason"], draft_command),
    ("/pause", [], pause),
    ("/reconcile 1 absent", ["1", "absent"], reconcile_command),
])
def test_unauthorized_commands_have_no_state_or_external_send(
        sessions, command_text, args, handler):
    attempt_id, draft_id = _unknown_attempt(sessions)
    with sessions() as session:
        source = session.scalar(select(Source))
        source.lifecycle_state = "validated"
        source_id = source.id
        session.commit()
    before = _snapshot(sessions, draft_id, attempt_id)
    update = _message_update(user_id=999, text=command_text)
    context = _context(sessions, args)
    import asyncio
    asyncio.run(handler(update, context))
    assert _snapshot(sessions, draft_id, attempt_id) == before
    with sessions() as session:
        assert session.get(Source, source_id).lifecycle_state == "validated"
        assert session.get(AppSetting, "publishing_paused") is None
    context.application.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("callback_data", [
    "draft:approve:1", "draft:reject:1", "source:approve:1",
])
def test_unauthorized_callbacks_do_not_mutate_database(sessions, callback_data):
    with sessions() as session:
        source, _, draft = sample_rows(session)
        source.lifecycle_state = "validated"
        session.commit()
        source_id, draft_id = source.id, draft.id
    with sessions() as session:
        before = (session.get(Draft, draft_id).draft_state,
                  session.get(Source, source_id).lifecycle_state,
                  session.scalar(select(func.count(AuditEvent.id))))
    update = _callback_update(callback_data, user_id=999)
    context = _context(sessions)
    handler = source_callback if callback_data.startswith("source:") else draft_callback
    import asyncio
    asyncio.run(handler(update, context))
    with sessions() as session:
        assert (session.get(Draft, draft_id).draft_state,
                session.get(Source, source_id).lifecycle_state,
                session.scalar(select(func.count(AuditEvent.id)))) == before
    update.callback_query.answer.assert_awaited_once()
    context.application.bot.send_message.assert_not_awaited()


def test_authorized_user_in_group_is_denied(sessions):
    update = _message_update(user_id=123, chat_type="group", text="/pause")
    context = _context(sessions)
    import asyncio
    asyncio.run(pause(update, context))
    with sessions() as session:
        assert session.get(AppSetting, "publishing_paused") is None
    context.application.bot.send_message.assert_not_awaited()
