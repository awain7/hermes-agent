"""Fork regression test: ``hermes send --list`` shows one line per target,
scoped to the multiplex profile the CLI runs as.

The shared channel directory carries one copy of a shared DM per profile
(untagged for the default profile, ``profile``-tagged for each secondary),
so the raw listing printed the same person six times on this install.
Fork-owned file name; listed in ``.github/workflows/fork-python-tests.yml``.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import send_cmd

_DM = {"id": "8815498158", "name": "Tiat-uí Khóo", "type": "dm"}
_DIRECTORY = {
    "updated_at": "2026-09-04T00:00:00",
    "platforms": {
        "telegram": [
            dict(_DM),
            dict(_DM, profile="coding"),
            dict(_DM, profile="work"),
            {"id": "-100777", "name": "home-only-group", "type": "group", "profile": "home"},
        ]
    },
}


class _NoPlatformsConfig:
    def get_connected_platforms(self):
        return []


@pytest.fixture
def directory(monkeypatch):
    import gateway.channel_directory as cd
    import gateway.config as gw_config

    monkeypatch.setattr(cd, "load_directory", lambda: json.loads(json.dumps(_DIRECTORY)))
    monkeypatch.setattr(gw_config, "load_gateway_config", lambda: _NoPlatformsConfig())
    return cd


def _set_profile(monkeypatch, name):
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: name)


def test_default_profile_json_lists_one_target_and_hides_other_profiles(monkeypatch, capsys, directory):
    _set_profile(monkeypatch, "default")
    rc = send_cmd._list_targets(None, json_mode=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    telegram = payload["platforms"]["telegram"]
    assert [(e["id"], e.get("profile")) for e in telegram] == [("8815498158", None)]


def test_secondary_profile_human_listing_prints_the_dm_once(monkeypatch, capsys, directory):
    _set_profile(monkeypatch, "work")
    rc = send_cmd._list_targets("telegram", json_mode=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("Tiat-uí Khóo") == 1
    assert "home-only-group" not in out


def test_home_profile_keeps_its_own_group(monkeypatch, capsys, directory):
    _set_profile(monkeypatch, "home")
    rc = send_cmd._list_targets(None, json_mode=True)
    assert rc == 0
    telegram = json.loads(capsys.readouterr().out)["platforms"]["telegram"]
    assert [e["id"] for e in telegram] == ["8815498158", "-100777"]


def test_unrecognised_home_still_collapses_duplicates(monkeypatch, capsys, directory):
    _set_profile(monkeypatch, "custom")
    rc = send_cmd._list_targets("telegram", json_mode=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("Tiat-uí Khóo") == 1
    assert "home-only-group" in out
