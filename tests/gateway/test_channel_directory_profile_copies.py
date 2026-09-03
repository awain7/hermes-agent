"""Fork regression test: under multiplexing, ``build_channel_directory`` writes
each secondary profile's own ``channel_directory.json`` next to the shared,
profile-tagged one in the launch home.

Upstream resolves ``DIRECTORY_PATH`` lazily from the current HERMES_HOME. The
fork keeps ONE shared directory in the launch home with secondary profiles'
entries tagged by ``profile`` (see ``build_channel_directory``); the gateway
pins the shared path at start-up, but a process running under a secondary
profile's home - ``hermes --profile <name> send --list`` - used to find no
directory at all. Fork-owned file name; listed in
``.github/workflows/fork-python-tests.yml``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from gateway import channel_directory as cd
from gateway.config import Platform


class _Adapter:
    """No ``list_channels``: forces session-based discovery for telegram."""


def _fake_sessions(platform_name, home_override=None):
    shared_dm = {"id": "8815498158", "name": "Tiat-uí Khóo", "type": "dm"}
    if home_override is not None:
        return [dict(shared_dm)]
    return [dict(shared_dm), {"id": "42", "name": "default-only", "type": "dm"}]


def _build(tmp_path):
    launch_home = tmp_path / "root"
    launch_home.mkdir()
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    with patch.object(cd, "DIRECTORY_PATH", launch_home / "channel_directory.json"), patch.object(
        cd, "CHANNEL_ALIASES_PATH", launch_home / "channel_aliases.json"
    ), patch.object(cd, "_build_from_sessions", side_effect=_fake_sessions), patch(
        "hermes_cli.profiles.get_profile_dir", return_value=work_home
    ):
        directory = asyncio.run(
            cd.build_channel_directory(
                {Platform.TELEGRAM: _Adapter()},
                {"work": {Platform.TELEGRAM: _Adapter()}},
            )
        )
    return directory, launch_home, work_home


def test_shared_directory_keeps_profile_tags(tmp_path):
    directory, launch_home, _ = _build(tmp_path)
    shared = json.loads((launch_home / "channel_directory.json").read_text(encoding="utf-8"))
    assert shared["platforms"] == directory["platforms"]
    assert [e.get("profile") for e in shared["platforms"]["telegram"]] == [None, None, "work"]


def test_secondary_profile_gets_its_own_filtered_copy(tmp_path):
    directory, _, work_home = _build(tmp_path)
    own = json.loads((work_home / "channel_directory.json").read_text(encoding="utf-8"))
    assert own["updated_at"] == directory["updated_at"]
    telegram = own["platforms"]["telegram"]
    # Same view resolve_channel_name(profile="work") serves in-process: the
    # profile's own tagged entry plus the untagged default-profile entries.
    assert [(e["id"], e.get("profile")) for e in telegram] == [
        ("8815498158", None),
        ("42", None),
        ("8815498158", "work"),
    ]


def test_profile_copy_is_readable_through_the_lazy_path(tmp_path):
    _, _, work_home = _build(tmp_path)
    with patch.object(cd, "DIRECTORY_PATH", None), patch.object(
        cd, "CHANNEL_ALIASES_PATH", None
    ), patch("gateway.channel_directory.get_hermes_home", return_value=work_home):
        assert cd.resolve_channel_name("telegram", "Tiat-uí Khóo", "work") == "8815498158"
        assert cd.resolve_channel_name("telegram", "default-only") == "42"


def test_no_profile_adapters_writes_no_copies(tmp_path):
    launch_home = tmp_path / "root"
    launch_home.mkdir()
    with patch.object(cd, "DIRECTORY_PATH", launch_home / "channel_directory.json"), patch.object(
        cd, "CHANNEL_ALIASES_PATH", launch_home / "channel_aliases.json"
    ), patch.object(cd, "_build_from_sessions", side_effect=_fake_sessions):
        asyncio.run(cd.build_channel_directory({Platform.TELEGRAM: _Adapter()}))
    assert (launch_home / "channel_directory.json").exists()
    assert not (tmp_path / "profiles").exists()
