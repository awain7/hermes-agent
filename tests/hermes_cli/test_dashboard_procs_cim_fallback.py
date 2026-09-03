"""Fork regression test: the Windows dashboard/serve process scan must fall
back to PowerShell ``Get-CimInstance`` when ``wmic`` is unavailable.

Windows 11 (and late Windows 10 builds) removed wmic.exe. Before the
fallback, ``_scan_dashboard_processes`` returned ``[]`` on such hosts, so
``hermes dashboard --stop`` / ``--status`` and the stale-dashboard reaper in
``hermes update`` silently found nothing. The fork lost an earlier version of
this fix in an upstream merge; this file has a fork-only name so the weekly
upstream merge cannot conflict on it, and it is listed in
``.github/workflows/fork-python-tests.yml`` so a future merge that drops the
fallback fails CI instead of regressing quietly.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from hermes_cli.dashboard_procs import _scan_dashboard_processes

_LIST_STDOUT = (
    "CommandLine=python -m hermes_cli.main dashboard --port 9119\n"
    "ProcessId=4242\n"
    "\n"
    "CommandLine=notepad.exe\n"
    "ProcessId=7\n"
    "\n"
)


def _which_powershell_only(name):
    return "C:/ps/powershell" if name == "powershell" else None


def test_falls_back_to_powershell_cim_when_wmic_is_missing():
    calls = []

    def fake_probe(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "wmic":
            return None  # spawn failure: wmic.exe is absent on Windows 11
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=_LIST_STDOUT, stderr=""
        )

    with patch("sys.platform", "win32"), patch(
        "hermes_cli._subprocess_compat.bounded_probe_run", side_effect=fake_probe
    ), patch("hermes_cli.dashboard_procs.shutil.which", side_effect=_which_powershell_only):
        procs = _scan_dashboard_processes()

    assert [argv[0] for argv, _ in calls] == ["wmic", "C:/ps/powershell"]
    ps_argv, ps_kwargs = calls[1]
    assert "Get-CimInstance Win32_Process" in ps_argv[-1]
    assert "-NonInteractive" in ps_argv
    assert ps_kwargs.get("errors") == "ignore"
    assert isinstance(ps_kwargs.get("timeout"), (int, float))
    assert procs == [(4242, "python -m hermes_cli.main dashboard --port 9119")]


def test_empty_wmic_output_also_trips_the_fallback():
    calls = []

    def fake_probe(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "wmic":
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=_LIST_STDOUT, stderr=""
        )

    with patch("sys.platform", "win32"), patch(
        "hermes_cli._subprocess_compat.bounded_probe_run", side_effect=fake_probe
    ), patch("hermes_cli.dashboard_procs.shutil.which", side_effect=_which_powershell_only):
        procs = _scan_dashboard_processes()

    assert calls == ["wmic", "C:/ps/powershell"]
    assert procs == [(4242, "python -m hermes_cli.main dashboard --port 9119")]


def test_wmic_success_does_not_spawn_powershell():
    calls = []

    def fake_probe(argv, **kwargs):
        calls.append(argv[0])
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=_LIST_STDOUT, stderr=""
        )

    with patch("sys.platform", "win32"), patch(
        "hermes_cli._subprocess_compat.bounded_probe_run", side_effect=fake_probe
    ):
        procs = _scan_dashboard_processes()

    assert calls == ["wmic"]
    assert procs == [(4242, "python -m hermes_cli.main dashboard --port 9119")]


def test_returns_empty_when_neither_scanner_is_available():
    with patch("sys.platform", "win32"), patch(
        "hermes_cli._subprocess_compat.bounded_probe_run", return_value=None
    ), patch("hermes_cli.dashboard_procs.shutil.which", return_value=None):
        assert _scan_dashboard_processes() == []
