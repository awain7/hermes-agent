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


class _TopicAdapter:
    """The default bot declares a configured DM topic (upstream ``configured_channels()``)."""

    def configured_channels(self):
        return [{"id": "222:9", "name": "Reports", "type": "dm", "thread_id": "9"}]


def _topic_sessions(platform_name, home_override=None):
    return [{"id": "111", "name": "Reports", "type": "dm"}] if home_override is not None else []


def test_default_bot_configured_topic_stays_out_of_secondary_views(tmp_path):
    """A DM topic's thread_id exists only in its own bot's chat: a secondary profile's bare-name
    lookup must find its own target, never the default bot's configured topic."""
    launch_home = tmp_path / "root"
    launch_home.mkdir()
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    with patch.object(cd, "DIRECTORY_PATH", launch_home / "channel_directory.json"), patch.object(
        cd, "CHANNEL_ALIASES_PATH", launch_home / "channel_aliases.json"
    ), patch.object(cd, "_build_from_sessions", side_effect=_topic_sessions), patch(
        "hermes_cli.profiles.get_profile_dir", return_value=work_home
    ):
        directory = asyncio.run(
            cd.build_channel_directory(
                {Platform.TELEGRAM: _TopicAdapter()},
                {"work": {Platform.TELEGRAM: _Adapter()}},
            )
        )
        assert [(e["id"], e.get("profile")) for e in directory["platforms"]["telegram"]] == [
            ("222:9", "default"),
            ("111", "work"),
        ]
        assert cd.resolve_channel_name("telegram", "Reports", "work") == "111"
        assert cd.resolve_channel_name("telegram", "Reports", "default") == "222:9"
    own = json.loads((work_home / "channel_directory.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in own["platforms"]["telegram"]] == ["111"]


def test_display_lists_each_target_once_and_hides_other_profiles():
    dm = {"id": "8815498158", "name": "Tiat-uí Khóo", "type": "dm"}
    platforms = {
        "telegram": [
            dict(dm),
            dict(dm, profile="coding"),
            dict(dm, profile="work"),
            {"id": "-100777", "name": "home-only-group", "type": "group", "profile": "home"},
        ]
    }
    unscoped = cd.format_directory_for_display(platforms)
    assert unscoped.count("telegram:Tiat-uí Khóo") == 1
    assert "home-only-group" in unscoped
    work_view = cd.format_directory_for_display(platforms, profile="work")
    assert work_view.count("telegram:Tiat-uí Khóo") == 1
    assert "home-only-group" not in work_view


def test_directory_view_for_profile_filters_then_dedupes():
    dm = {"id": "8815498158", "name": "Tiat-uí Khóo", "type": "dm"}
    platforms = {
        "telegram": [dict(dm), dict(dm, profile="work"), dict(dm, profile="home")],
        "discord": [],
    }
    view = cd.directory_view_for_profile(platforms, "work")
    assert [(e["id"], e.get("profile")) for e in view["telegram"]] == [("8815498158", None)]
    assert view["discord"] == []
    # Falsy profile keeps every profile's targets but still one line per id.
    assert [e["id"] for e in cd.directory_view_for_profile(platforms, "")["telegram"]] == ["8815498158"]
    # Entries without an id are never collapsed.
    raw = {"x": [{"name": "a"}, {"name": "b"}]}
    assert cd.directory_view_for_profile(raw, "work")["x"] == raw["x"]
