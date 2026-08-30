"""TelegramAdapter wedged-getUpdates detection via pending_update_count.

PTB can report ``updater.running == True`` while its long-poll consumer is
silently stuck (observed on WSL2), so DMs queue in the Bot API and never reach
handlers (#42909). ``get_me()`` stays healthy (general request path), so the
CLOSE-WAIT heartbeat is blind to it. ``_probe_pending_updates`` watches
``get_webhook_info().pending_update_count`` and escalates to the existing
network-error recovery ladder after two consecutive stuck probes.

The same probe also covers the harsher case where the updater has stopped
entirely (``running=False``) with no reconnect in flight — the long-poll task
is gone, so the gateway silently stops receiving messages while the process
stays alive (#55769) — and feeds it into the same recovery ladder.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter(*, pending: int) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._webhook_mode = False
    adapter._app = MagicMock()
    adapter._app.updater.running = True
    bot = MagicMock()
    bot.get_webhook_info = AsyncMock(
        return_value=MagicMock(pending_update_count=pending)
    )
    adapter._app.bot = bot
    adapter._bot = bot
    return adapter


@pytest.mark.asyncio
async def test_single_stopped_updater_probe_does_not_escalate():
    """One probe finding a stopped updater only increments the counter (#55769)."""
    adapter = _make_adapter(pending=9)
    adapter._app.updater.running = False
    adapter._polling_pending_stuck_count = 1
    with patch.object(adapter, "_handle_polling_network_error", new=AsyncMock()) as rec:
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    # Stopped updater means no live consumer to query for a queue.
    adapter._app.bot.get_webhook_info.assert_not_called()
    assert adapter._polling_pending_stuck_count == 0
    assert adapter._polling_not_running_count == 1
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_two_stopped_updater_probes_trigger_recovery():
    """A stopped updater that stays stopped routes into recovery (#55769)."""
    adapter = _make_adapter(pending=9)
    adapter._app.updater.running = False
    recovery = AsyncMock()
    with patch.object(adapter, "_handle_polling_network_error", new=recovery):
        await adapter._probe_pending_updates(adapter._app.bot, 5)
        assert adapter._polling_not_running_count == 1
        await adapter._probe_pending_updates(adapter._app.bot, 5)
        task = adapter._polling_error_task
        assert task is not None
        await task
    recovery.assert_awaited_once()
    assert adapter._polling_not_running_count == 0


@pytest.mark.asyncio
async def test_reconnect_in_flight_skips_stopped_updater_escalation():
    """A stopped updater during an in-flight reconnect must not re-escalate."""
    adapter = _make_adapter(pending=9)
    adapter._app.updater.running = False
    adapter._polling_not_running_count = 1
    inflight = MagicMock()
    inflight.done.return_value = False
    adapter._polling_error_task = inflight
    with patch.object(adapter, "_handle_polling_network_error", new=AsyncMock()) as rec:
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    # The in-flight reconnect owns recovery; the stopped-updater counter resets
    # so the transient stop()->start_polling() window never trips a re-trigger.
    assert adapter._polling_not_running_count == 0
    rec.assert_not_called()


# ---------------------------------------------------------------------------
# Heartbeat self-resurrection: the heartbeat loop is the watchdog for the
# polling connection, so its own death must not be silent/terminal. Observed
# 2026-08-24: a profile wedged for 75 minutes with zero heartbeat warnings -
# the loop itself had died earlier and nothing brought it back.
# ---------------------------------------------------------------------------

import asyncio
import time as _time


async def _finished_task(exc=None):
    """Return a completed asyncio.Task, optionally failed with exc."""
    async def _body():
        if exc is not None:
            raise exc
    task = asyncio.get_running_loop().create_task(_body())
    try:
        await task
    except Exception:
        pass
    return task


def _live_polling_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._webhook_mode = False
    adapter._running = True  # is_connected property reads this
    return adapter


async def _cleanup(adapter):
    task = adapter._polling_heartbeat_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_heartbeat_unexpected_death_resurrects():
    adapter = _live_polling_adapter()
    dead = await _finished_task(RuntimeError("boom"))

    adapter._on_heartbeat_task_done(dead)

    live = adapter._polling_heartbeat_task
    assert live is not None and not live.done()
    assert len(adapter._heartbeat_restart_times) == 1
    await _cleanup(adapter)


@pytest.mark.asyncio
async def test_heartbeat_clean_return_while_polling_resurrects():
    """A defensive `return` inside the loop while the adapter still believes
    it is polling is exactly the silent-death mode - it must re-arm too."""
    adapter = _live_polling_adapter()
    dead = await _finished_task()  # completed without exception

    adapter._on_heartbeat_task_done(dead)

    live = adapter._polling_heartbeat_task
    assert live is not None and not live.done()
    await _cleanup(adapter)


@pytest.mark.asyncio
async def test_heartbeat_cancelled_not_resurrected():
    adapter = _live_polling_adapter()
    task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    adapter._on_heartbeat_task_done(task)

    assert adapter._polling_heartbeat_task is None


@pytest.mark.asyncio
async def test_heartbeat_not_resurrected_when_disconnected():
    adapter = _live_polling_adapter()
    adapter._running = False
    dead = await _finished_task(RuntimeError("boom"))

    adapter._on_heartbeat_task_done(dead)

    assert adapter._polling_heartbeat_task is None


@pytest.mark.asyncio
async def test_heartbeat_not_resurrected_with_fatal_error():
    adapter = _live_polling_adapter()
    adapter._set_fatal_error("auth_error", "bad token", retryable=False)
    dead = await _finished_task(RuntimeError("boom"))

    adapter._on_heartbeat_task_done(dead)

    assert adapter._polling_heartbeat_task is None


@pytest.mark.asyncio
async def test_heartbeat_resurrection_rate_limited():
    """More than _HEARTBEAT_RESTART_LIMIT deaths inside the window gives up
    (the external watchdog covers wedge detection from outside the process)."""
    adapter = _live_polling_adapter()
    now = _time.monotonic()
    adapter._heartbeat_restart_times = [now - 1, now - 2, now - 3]
    dead = await _finished_task(RuntimeError("boom"))

    adapter._on_heartbeat_task_done(dead)

    assert adapter._polling_heartbeat_task is None


@pytest.mark.asyncio
async def test_heartbeat_rate_limit_window_expires():
    """Old deaths outside the window do not count against the limit."""
    adapter = _live_polling_adapter()
    old = _time.monotonic() - adapter._HEARTBEAT_RESTART_WINDOW_S - 5
    adapter._heartbeat_restart_times = [old, old + 1, old + 2]
    dead = await _finished_task(RuntimeError("boom"))

    adapter._on_heartbeat_task_done(dead)

    live = adapter._polling_heartbeat_task
    assert live is not None and not live.done()
    assert len(adapter._heartbeat_restart_times) == 1
    await _cleanup(adapter)


@pytest.mark.asyncio
async def test_arm_polling_heartbeat_cancels_previous():
    adapter = _live_polling_adapter()
    adapter._arm_polling_heartbeat()
    first = adapter._polling_heartbeat_task
    adapter._arm_polling_heartbeat()
    second = adapter._polling_heartbeat_task
    await asyncio.sleep(0)

    assert first is not second
    assert first.cancelled() or first.done()
    assert not second.done()
    await _cleanup(adapter)
