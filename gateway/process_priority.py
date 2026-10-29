"""(fork) Undo an inherited background priority when the gateway starts on Windows.

Task Scheduler starts a task at its default priority 7: ``BELOW_NORMAL_PRIORITY_CLASS``, Low I/O
priority and memory priority 2, and Windows hands all three to every descendant. The gateway's own
task template (``hermes_cli/gateway_windows.py``) carries that default, and so do the scheduled
scripts that restart or cold-start it, so a gateway brought up at logon, by a watchdog or by an
update runs below every interactive process on the host. When those saturate the CPUs it is left with
the scheduler's anti-starvation scraps: the event loop cannot answer a liveness probe inside its
window, three misses hard-exit the process with code 75, and on Windows nothing restarts it (#91097).

The gateway is a long-running service, not a background batch job, so each of the three is raised
back to normal. Nothing is ever lowered, and nothing above normal is touched.
``HERMES_GATEWAY_KEEP_PRIORITY=1`` leaves a deliberately lowered priority alone.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

NORMAL_PRIORITY_CLASS = 0x20
IDLE_PRIORITY_CLASS = 0x40
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
IO_PRIORITY_NORMAL = 2  # IO_PRIORITY_HINT.IoPriorityNormal (0 very low, 1 low)
PAGE_PRIORITY_NORMAL = 5  # MEMORY_PRIORITY_NORMAL (1 lowest)
PROCESS_IO_PRIORITY = 33  # PROCESSINFOCLASS.ProcessIoPriority
PROCESS_PAGE_PRIORITY = 39  # PROCESSINFOCLASS.ProcessPagePriority
_LOWERED_PRIORITY_CLASSES = frozenset({IDLE_PRIORITY_CLASS, BELOW_NORMAL_PRIORITY_CLASS})
_KEEP_PRIORITY_ENV = "HERMES_GATEWAY_KEEP_PRIORITY"


class _WindowsPriorityApi:
    """The Win32 calls on the current process, kept apart from the decisions so those run anywhere."""

    def __init__(self) -> None:
        from ctypes import WinDLL, byref, c_int, c_long, c_ulong, c_void_p, sizeof, wintypes

        kernel32, ntdll = WinDLL("kernel32", use_last_error=True), WinDLL("ntdll")
        # Each foreign function is bound once with its prototype; the methods call the bound name.
        current_process = kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE  # the default c_int truncates the pseudo-handle on 64-bit
        self._get_class = kernel32.GetPriorityClass
        self._get_class.argtypes, self._get_class.restype = [wintypes.HANDLE], wintypes.DWORD
        self._set_class = kernel32.SetPriorityClass
        self._set_class.argtypes, self._set_class.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.BOOL
        self._query_info = ntdll.NtQueryInformationProcess
        self._query_info.argtypes = [wintypes.HANDLE, c_int, c_void_p, wintypes.ULONG, c_void_p]
        self._query_info.restype = c_long
        self._set_info = ntdll.NtSetInformationProcess
        self._set_info.argtypes = [wintypes.HANDLE, c_int, c_void_p, wintypes.ULONG]
        self._set_info.restype = c_long
        self._ulong = c_ulong
        self._byref = byref
        self._sizeof = sizeof
        self._process = current_process()

    def get_priority_class(self) -> Optional[int]:
        return int(self._get_class(self._process)) or None  # 0 = the call failed

    def set_priority_class(self, value: int) -> bool:
        return bool(self._set_class(self._process, value))

    def get_process_info(self, info_class: int) -> Optional[int]:
        value = self._ulong(0)
        status = self._query_info(self._process, info_class, self._byref(value), self._sizeof(value), None)
        return int(value.value) if status == 0 else None

    def set_process_info(self, info_class: int, value: int) -> bool:
        buf = self._ulong(value)
        return self._set_info(self._process, info_class, self._byref(buf), self._sizeof(buf)) == 0


def _keep_priority() -> bool:
    return os.environ.get(_KEEP_PRIORITY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def restore_normal_process_priority(api: Any = None) -> Dict[str, Any]:
    """Raise a lowered CPU class, I/O priority and memory priority back to normal; never raises.

    Returns what the process started with and what was raised, e.g. for a Task Scheduler launch
    ``{"priority_class": 0x4000, "io_priority": 1, "page_priority": 2, "raised": ["priority_class",
    "io_priority", "page_priority"]}``; ``{}`` when there is nothing to inspect (not Windows, opted
    out, or the Win32 surface is unavailable). *api* is the seam for tests.
    """
    if _keep_priority():
        return {}
    if api is None:
        if sys.platform != "win32":
            return {}
        try:
            api = _WindowsPriorityApi()
        except Exception:
            logger.debug("process priority API unavailable", exc_info=True)
            return {}
    found: Dict[str, Any] = {"raised": []}
    # Independent steps: one refused call must not leave the other two lowered.
    try:
        found["priority_class"] = priority_class = api.get_priority_class()
        if priority_class in _LOWERED_PRIORITY_CLASSES and api.set_priority_class(NORMAL_PRIORITY_CLASS):
            found["raised"].append("priority_class")
    except Exception:
        logger.debug("could not restore the process priority class", exc_info=True)
    for name, info_class, normal in (
        ("io_priority", PROCESS_IO_PRIORITY, IO_PRIORITY_NORMAL),
        ("page_priority", PROCESS_PAGE_PRIORITY, PAGE_PRIORITY_NORMAL),
    ):
        try:
            found[name] = current = api.get_process_info(info_class)
            if current is not None and current < normal and api.set_process_info(info_class, normal):
                found["raised"].append(name)
        except Exception:
            logger.debug("could not restore the process %s", name, exc_info=True)
    return found
