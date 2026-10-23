"""(fork) An idle ROOT poll must not rebuild the launch profile's scope on the event loop.

The handoff watcher ticks every 2 s. Secondary profiles are idle-gated (``run_idle_gates``): no
pending handoff, no scope entry. The root poll was left ungated because it used to be unscoped —
but once a process multiplexes, the root poll binds the launch profile's scope, and
``launch_profile_runtime_scope`` re-reads ``.env`` and rebuilds the terminal policy synchronously on
the loop: about nine filesystem calls per tick, forever, with nothing to do. On a CPU-starved host
(2026-10-04) that was the frame the blocked-loop dumps kept catching the loop thread in, shortly
before the loop-liveness watchdog killed the gateway.

The root poll now takes the same gate, against the launch home's store. A single-profile host is
unchanged: its root poll really is unscoped.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import threading
import types

import pytest

from gateway import run


class _RecordingDB:
    """AsyncSessionDB-shaped root store; nothing pending, counts polls."""

    def __init__(self):
        self.polls = 0

    async def list_pending_handoffs(self):
        self.polls += 1
        return []


class _ProbeDB:
    """Probe-side store (goals SessionDB cache) for the idle gate; records where it was asked."""

    def __init__(self, pending, *, raises=False):
        self._pending, self._raises = pending, raises
        self.asked_on_loop_thread: list = []

    def has_pending_handoffs(self):
        self.asked_on_loop_thread.append(_on_loop_thread())
        if self._raises:
            raise RuntimeError("store unavailable")
        return self._pending


def _on_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def _run_watcher(monkeypatch, db, *, ticks=1):
    """Drive ``_handoff_watcher`` for the startup reclaim plus *ticks* poll ticks."""
    monkeypatch.setattr(run, "_handoff_watch_scopes", lambda _runner: [(None, None)])

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(run.asyncio, "sleep", _no_sleep)
    states = iter([True] * ticks)

    class _Running:
        def __bool__(_self):
            return next(states, False)

    async def _process_handoff(row, profile_name=None):
        return None

    fake = types.SimpleNamespace(
        _session_db=db, _running=_Running(), _process_handoff=_process_handoff,
        _run_in_executor_with_context=asyncio.to_thread)
    await asyncio.wait_for(run.GatewayRunner._handoff_watcher(fake, interval=0.0), timeout=10)


@pytest.fixture
def launch_home(monkeypatch):
    """The process home, with multi-profile hosting active (what a multiplexed gateway runs as)."""
    from agent import secret_scope
    from hermes_constants import get_process_hermes_home

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    return get_process_hermes_home()


@pytest.fixture
def launch_scope_entries(monkeypatch):
    """Replace the launch scope with a spy; yields the list of homes it was entered for."""
    from tui_gateway import launch_profile_policy

    entered: list = []

    @contextlib.contextmanager
    def _spy(home):
        entered.append(home)
        yield

    monkeypatch.setattr(launch_profile_policy, "launch_profile_runtime_scope", _spy)
    return entered


def _probe_store(monkeypatch, home, probe) -> None:
    from hermes_cli import goals

    monkeypatch.setattr(goals, "_DB_CACHE", {str(home): probe})


@pytest.mark.asyncio
async def test_idle_root_poll_skips_the_launch_scope(monkeypatch, launch_home, launch_scope_entries):
    probe = _ProbeDB(pending=False)
    _probe_store(monkeypatch, launch_home, probe)
    db = _RecordingDB()

    await _run_watcher(monkeypatch, db, ticks=3)

    assert launch_scope_entries == [launch_home], (
        "only the once-per-boot stale-handoff reclaim may enter an idle launch scope; "
        f"got {len(launch_scope_entries)} entries for 3 ticks")
    assert db.polls == 0, "an idle root store is not polled inside the scope at all"
    assert probe.asked_on_loop_thread == [False, False, False], (
        "the gate must read the store off the event loop, once per tick")


@pytest.mark.asyncio
async def test_pending_root_handoff_still_enters_the_launch_scope(
        monkeypatch, launch_home, launch_scope_entries):
    _probe_store(monkeypatch, launch_home, _ProbeDB(pending=True))
    db = _RecordingDB()

    await _run_watcher(monkeypatch, db, ticks=2)

    assert launch_scope_entries == [launch_home] * 3, "reclaim + one entry per tick with work"
    assert db.polls == 2, "each tick with pending work polls the root store inside the scope"


@pytest.mark.asyncio
async def test_an_unreadable_store_fails_open(monkeypatch, launch_home, launch_scope_entries):
    # "Cannot prove the store is empty" is never "idle": a handoff must not be starved by a bad probe.
    _probe_store(monkeypatch, launch_home, _ProbeDB(pending=False, raises=True))
    db = _RecordingDB()

    await _run_watcher(monkeypatch, db, ticks=1)

    assert launch_scope_entries == [launch_home] * 2
    assert db.polls == 1


@pytest.mark.asyncio
async def test_single_profile_root_poll_is_not_gated(monkeypatch, launch_scope_entries):
    """No multiplexing: the root poll is a nullcontext, so there is nothing to save and no probe."""
    from agent import secret_scope
    from hermes_constants import get_process_hermes_home

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    probe = _ProbeDB(pending=False)
    _probe_store(monkeypatch, get_process_hermes_home(), probe)
    db = _RecordingDB()

    await _run_watcher(monkeypatch, db, ticks=2)

    assert db.polls == 2, "the single-profile root poll keeps running every tick"
    assert probe.asked_on_loop_thread == [], "no gate probe on a single-profile host"
    assert launch_scope_entries == [], "and no launch scope either"


def test_launch_gate_home_follows_multi_profile_hosting(monkeypatch):
    from agent import secret_scope
    from gateway.run_idle_gates import launch_gate_home
    from hermes_constants import get_process_hermes_home

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    assert launch_gate_home() is None
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    assert launch_gate_home() == get_process_hermes_home()


@pytest.mark.asyncio
async def test_idle_ticks_do_no_file_io_on_the_event_loop(monkeypatch, launch_home):
    """The incident's mechanism, against the REAL launch scope: count ``open()`` calls made on the
    loop thread. Idle ticks must add none on top of the one scope entry the startup reclaim makes."""
    launch_home.mkdir(parents=True, exist_ok=True)
    (launch_home / ".env").write_text("TERMINAL_TIMEOUT=180\nSOME_KEY=value\n", encoding="utf-8")
    (launch_home / "config.yaml").write_text("terminal:\n  timeout: 180\n", encoding="utf-8")
    _probe_store(monkeypatch, launch_home, _ProbeDB(pending=False))

    loop_thread = threading.get_ident()
    opens_on_loop = [0]
    real_open = io.open

    def _counting_open(*args, **kwargs):
        if threading.get_ident() == loop_thread:
            opens_on_loop[0] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(io, "open", _counting_open)
    monkeypatch.setattr(builtins, "open", _counting_open)

    from tui_gateway.launch_profile_policy import launch_profile_runtime_scope

    with launch_profile_runtime_scope(launch_home):
        pass
    one_entry = opens_on_loop[0]
    assert one_entry > 0, "the launch scope is expected to read the profile's files when entered"

    opens_on_loop[0] = 0
    await _run_watcher(monkeypatch, _RecordingDB(), ticks=5)

    assert opens_on_loop[0] <= one_entry, (
        f"5 idle ticks opened files {opens_on_loop[0]} times on the event loop; one scope entry "
        f"(the startup reclaim) costs {one_entry}")
