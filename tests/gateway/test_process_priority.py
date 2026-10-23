"""(fork) The gateway must not keep the background priority a Task Scheduler launch hands it.

Incident (Windows 11, 4-core host, 2026-10-04): the gateway had been cold-started from a scheduled
task, so it ran at ``BELOW_NORMAL_PRIORITY_CLASS`` with Low I/O priority and memory priority 2 (Task
Scheduler's default priority 7, inherited by every descendant). Other workloads then saturated the
CPUs at normal priority. The event loop got too little CPU to answer three liveness probes in a row
and the loop-liveness watchdog hard-exited the process with code 75 — with no supervisor on Windows
to bring it back (#91097).

The decisions are tested against a fake of the Win32 surface so they run on every platform; one
test drives the real calls in a child process created the way the incident's gateway was.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gateway import process_priority as pp

ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
HIGH_PRIORITY_CLASS = 0x80
REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeApi:
    """Stands in for ``_WindowsPriorityApi``; records every attempted change."""

    def __init__(self, priority_class, io, page, *, refuse=(), explode=()):
        self.priority_class = priority_class
        self.info = {pp.PROCESS_IO_PRIORITY: io, pp.PROCESS_PAGE_PRIORITY: page}
        self.refuse, self.explode = set(refuse), set(explode)
        self.sets: list = []

    def get_priority_class(self):
        return self.priority_class

    def set_priority_class(self, value):
        self.sets.append(("priority_class", value))
        if "priority_class" in self.explode:
            raise OSError("access denied")
        if "priority_class" in self.refuse:
            return False
        self.priority_class = value
        return True

    def get_process_info(self, info_class):
        return self.info[info_class]

    def set_process_info(self, info_class, value):
        self.sets.append((info_class, value))
        if info_class in self.refuse:
            return False
        self.info[info_class] = value
        return True

    def snapshot(self):
        return (self.priority_class, self.info[pp.PROCESS_IO_PRIORITY], self.info[pp.PROCESS_PAGE_PRIORITY])


NORMAL = (pp.NORMAL_PRIORITY_CLASS, pp.IO_PRIORITY_NORMAL, pp.PAGE_PRIORITY_NORMAL)


@pytest.fixture(autouse=True)
def _no_opt_out_from_the_developer_shell(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_KEEP_PRIORITY", raising=False)


class TestLoweredPriorityIsRaised:
    def test_a_task_scheduler_launch_is_raised_on_all_three_axes(self):
        # Exactly what a priority-7 task hands down (measured on the incident host).
        api = _FakeApi(pp.BELOW_NORMAL_PRIORITY_CLASS, io=1, page=2)

        found = pp.restore_normal_process_priority(api)

        assert api.snapshot() == NORMAL
        assert found == {
            "priority_class": pp.BELOW_NORMAL_PRIORITY_CLASS, "io_priority": 1, "page_priority": 2,
            "raised": ["priority_class", "io_priority", "page_priority"],
        }, "the record must name what this life started with, for the next post-mortem"

    def test_an_idle_class_launch_is_raised_too(self):
        api = _FakeApi(pp.IDLE_PRIORITY_CLASS, io=0, page=1)

        pp.restore_normal_process_priority(api)

        assert api.snapshot() == NORMAL

    @pytest.mark.parametrize("lowered", ["priority_class", "io_priority", "page_priority"])
    def test_each_axis_is_raised_on_its_own(self, lowered):
        start = {"priority_class": pp.NORMAL_PRIORITY_CLASS, "io_priority": 2, "page_priority": 5}
        start[lowered] = {"priority_class": pp.BELOW_NORMAL_PRIORITY_CLASS, "io_priority": 1,
                          "page_priority": 2}[lowered]
        api = _FakeApi(start["priority_class"], io=start["io_priority"], page=start["page_priority"])

        found = pp.restore_normal_process_priority(api)

        assert api.snapshot() == NORMAL
        assert found["raised"] == [lowered]
        assert len(api.sets) == 1, f"only the lowered axis may be written; got {api.sets}"


class TestNothingElseIsTouched:
    def test_a_normal_process_is_left_alone(self):
        api = _FakeApi(*NORMAL)

        found = pp.restore_normal_process_priority(api)

        assert api.sets == []
        assert found["raised"] == []

    @pytest.mark.parametrize("priority_class", [ABOVE_NORMAL_PRIORITY_CLASS, HIGH_PRIORITY_CLASS])
    def test_a_raised_priority_is_never_lowered(self, priority_class):
        # An operator who started the gateway above normal chose that.
        api = _FakeApi(priority_class, io=3, page=5)

        pp.restore_normal_process_priority(api)

        assert api.sets == []
        assert api.snapshot() == (priority_class, 3, 5)

    def test_the_opt_out_keeps_a_deliberately_lowered_priority(self, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_KEEP_PRIORITY", "1")
        api = _FakeApi(pp.BELOW_NORMAL_PRIORITY_CLASS, io=1, page=2)

        assert pp.restore_normal_process_priority(api) == {}
        assert api.sets == []

    def test_other_platforms_are_a_no_op(self, monkeypatch):
        monkeypatch.setattr(pp.sys, "platform", "linux")

        assert pp.restore_normal_process_priority() == {}


class TestOneFailureDoesNotStrandTheRest:
    def test_a_refused_class_change_still_raises_io_and_memory(self):
        api = _FakeApi(pp.BELOW_NORMAL_PRIORITY_CLASS, io=1, page=2, refuse={"priority_class"})

        found = pp.restore_normal_process_priority(api)

        assert api.snapshot() == (pp.BELOW_NORMAL_PRIORITY_CLASS, pp.IO_PRIORITY_NORMAL, pp.PAGE_PRIORITY_NORMAL)
        assert found["raised"] == ["io_priority", "page_priority"]

    def test_a_raising_class_change_still_raises_io_and_memory(self):
        api = _FakeApi(pp.BELOW_NORMAL_PRIORITY_CLASS, io=1, page=2, explode={"priority_class"})

        found = pp.restore_normal_process_priority(api)  # must not propagate: this runs at gateway boot

        assert found["raised"] == ["io_priority", "page_priority"]

    def test_a_refused_io_change_still_raises_memory(self):
        api = _FakeApi(pp.BELOW_NORMAL_PRIORITY_CLASS, io=1, page=2, refuse={pp.PROCESS_IO_PRIORITY})

        found = pp.restore_normal_process_priority(api)

        assert found["raised"] == ["priority_class", "page_priority"]

    def test_an_unreadable_value_is_not_written(self):
        # A failed query reads as None: no evidence it is lowered, so no blind write.
        api = _FakeApi(None, io=None, page=None)

        found = pp.restore_normal_process_priority(api)

        assert api.sets == []
        assert found == {"priority_class": None, "io_priority": None, "page_priority": None, "raised": []}

    def test_an_unavailable_win32_surface_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(pp.sys, "platform", "win32")

        def _boom():
            raise OSError("ntdll unavailable")

        monkeypatch.setattr(pp, "_WindowsPriorityApi", _boom)

        assert pp.restore_normal_process_priority() == {}


def test_run_gateway_restores_priority_before_the_loop_starts():
    """The hook is one line in an upstream-owned function: a merge that drops it must fail here."""
    from hermes_cli import gateway as gateway_cli

    source = inspect.getsource(gateway_cli.run_gateway)

    assert "restore_normal_process_priority()" in source
    assert source.index("restore_normal_process_priority()") < source.index("asyncio.run("), (
        "the priority must be restored before the event loop the watchdog probes is running")


@pytest.mark.skipif(sys.platform != "win32", reason="drives the real Win32 priority calls")
def test_a_child_created_like_a_scheduled_task_ends_up_normal():
    """The incident, end to end: a process born BELOW_NORMAL with Low I/O and memory priority 2."""
    child = textwrap.dedent(
        """
        import json
        from gateway import process_priority as pp

        api = pp._WindowsPriorityApi()
        # CreateProcess only carries the CPU class; lower the other two the way Task Scheduler does.
        assert api.set_process_info(pp.PROCESS_IO_PRIORITY, 1)
        assert api.set_process_info(pp.PROCESS_PAGE_PRIORITY, 2)

        def read():
            return [api.get_priority_class(), api.get_process_info(pp.PROCESS_IO_PRIORITY),
                    api.get_process_info(pp.PROCESS_PAGE_PRIORITY)]

        before = read()
        found = pp.restore_normal_process_priority()
        print(json.dumps({"before": before, "found": found, "after": read()}))
        """
    )

    proc = subprocess.run(
        [sys.executable, "-c", child], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
        creationflags=pp.BELOW_NORMAL_PRIORITY_CLASS,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["before"] == [pp.BELOW_NORMAL_PRIORITY_CLASS, 1, 2], (
        f"the child did not start in the incident's state: {result['before']}")
    assert result["after"] == list(NORMAL)
    assert result["found"]["raised"] == ["priority_class", "io_priority", "page_priority"]
