"""
Tests for the Telegram stale-pending-update guard.

connect() now always preserves the server-side getUpdates queue
(drop_pending_updates=False on cold boot too), so messages sent while the
gateway was down — nightly auto-update restarts, crashes, reboots — are
delivered instead of silently vanishing. The group=-1 guard
``_drop_stale_pending_update`` bounds the replay window instead: queued
messages older than HERMES_TELEGRAM_PENDING_MESSAGE_MAX_AGE (default 600s)
are dropped before any group-0 handler runs.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class _HandlerStop(Exception):
    """Stand-in for telegram.ext.ApplicationHandlerStop.

    The gateway conftest replaces the ``telegram`` package with a MagicMock
    when the real library isn't already imported, which turns the adapter's
    ``ApplicationHandlerStop`` symbol into a mock attribute that cannot be
    raised. Patch in a real exception class so the guard's raise/except
    behavior is what's under test, not the mock."""


@pytest.fixture(autouse=True)
def _real_handler_stop(monkeypatch):
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.ApplicationHandlerStop", _HandlerStop
    )


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))


def _update(message=None, callback_query=None):
    return SimpleNamespace(
        message=message,
        effective_message=message,
        callback_query=callback_query,
    )


def _message(age_seconds: float, *, edit_age_seconds=None, naive=False):
    now = datetime.now(timezone.utc)
    date = now - timedelta(seconds=age_seconds)
    if naive:
        date = date.replace(tzinfo=None)
    edit_date = None
    if edit_age_seconds is not None:
        edit_date = now - timedelta(seconds=edit_age_seconds)
    return SimpleNamespace(date=date, edit_date=edit_date, chat=SimpleNamespace(id=42))


@pytest.mark.asyncio
async def test_fresh_message_passes():
    adapter = _make_adapter()
    await adapter._drop_stale_pending_update(_update(_message(5)), None)


@pytest.mark.asyncio
async def test_stale_message_dropped():
    adapter = _make_adapter()
    with pytest.raises(_HandlerStop):
        await adapter._drop_stale_pending_update(_update(_message(3600)), None)


@pytest.mark.asyncio
async def test_message_just_under_cutoff_passes():
    adapter = _make_adapter()
    cutoff = adapter._pending_message_max_age_s
    await adapter._drop_stale_pending_update(_update(_message(cutoff - 30)), None)


@pytest.mark.asyncio
async def test_callback_query_on_old_message_passes():
    """Pressing a button on an old message is a legitimate current action —
    the guard must not age callbacks by their anchor message's date."""
    adapter = _make_adapter()
    update = _update(
        message=_message(7200),
        callback_query=SimpleNamespace(data="confirm"),
    )
    await adapter._drop_stale_pending_update(update, None)


@pytest.mark.asyncio
async def test_edited_message_aged_by_edit_date():
    """A fresh edit of an old message is fresh intent — passes. A stale edit
    replayed from the queue is dropped."""
    adapter = _make_adapter()
    await adapter._drop_stale_pending_update(
        _update(_message(7200, edit_age_seconds=5)), None
    )
    with pytest.raises(_HandlerStop):
        await adapter._drop_stale_pending_update(
            _update(_message(7200, edit_age_seconds=3600)), None
        )


@pytest.mark.asyncio
async def test_zero_cutoff_disables_guard(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_PENDING_MESSAGE_MAX_AGE", "0")
    adapter = _make_adapter()
    assert adapter._pending_message_max_age_s == 0.0
    await adapter._drop_stale_pending_update(_update(_message(86400)), None)


@pytest.mark.asyncio
async def test_env_override_changes_cutoff(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_PENDING_MESSAGE_MAX_AGE", "60")
    adapter = _make_adapter()
    with pytest.raises(_HandlerStop):
        await adapter._drop_stale_pending_update(_update(_message(120)), None)
    await adapter._drop_stale_pending_update(_update(_message(10)), None)


@pytest.mark.asyncio
async def test_naive_datetime_fails_open():
    """A naive datetime (test fakes, exotic clients) must pass through —
    dropping real traffic is the worse error."""
    adapter = _make_adapter()
    await adapter._drop_stale_pending_update(
        _update(_message(7200, naive=True)), None
    )


@pytest.mark.asyncio
async def test_update_without_message_passes():
    adapter = _make_adapter()
    await adapter._drop_stale_pending_update(_update(message=None), None)


@pytest.mark.asyncio
async def test_message_without_date_passes():
    adapter = _make_adapter()
    msg = SimpleNamespace(date=None, edit_date=None, chat=SimpleNamespace(id=42))
    await adapter._drop_stale_pending_update(_update(msg), None)
