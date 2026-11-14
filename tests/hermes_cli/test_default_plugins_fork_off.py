"""Fork: catalog ``default: true`` plugins never install unattended on this host.

Upstream installs and enables every official/verified default plugin in every profile home at the end
of ``hermes update`` and on a new revision's first boot, dependencies and tools included, which
``agent.disabled_toolsets`` does not cover. The fork keeps that off unless
``HERMES_FORK_ALLOW_DEFAULT_PLUGINS=1`` is set: no catalog read, no install, and the boot step settles
instead of retrying. Fork-owned file name; listed in ``.github/workflows/fork-python-tests.yml``.
"""

from __future__ import annotations

import pytest

import hermes_cli.left_core_migration as lcm
import hermes_cli.plugin_catalog
import pm.plugins_state
from hermes_cli import default_plugins
from hermes_cli.plugin_catalog import entry_from_mapping


def _entry(name: str):
    return entry_from_mapping({"name": name, "repo": "https://github.com/NousResearch/hermes-official-plugins",
                               "sha": "a" * 40, "description": "d", "maintainer": "NousResearch",
                               "tier": "official", "default": True}, name)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A home that allows lazy installs, with every install, catalog read and spawn recorded."""
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.delenv("HERMES_FORK_ALLOW_DEFAULT_PLUGINS", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("security:\n  allow_lazy_installs: true\n", encoding="utf-8")
    calls: list = []

    def install_into(target, consent=None):
        def install(name, discard=None):
            calls.append(("install", name))
            return {"ok": True}
        return install
    monkeypatch.setattr(lcm, "_install_into", install_into)
    monkeypatch.setattr(hermes_cli.plugin_catalog, "fetch_live_catalog",
                        lambda **kwargs: calls.append(("catalog",)) or {"entries": [{"name": "x"}]})
    monkeypatch.setattr(default_plugins, "default_entries", lambda: calls.append(("entries",)) or [_entry("x")])
    monkeypatch.setattr(pm.plugins_state, "dependency_homes", lambda: [home])
    monkeypatch.setattr(default_plugins, "_identity", lambda: "rev-1")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append(("spawn",)))
    return home, calls


def test_defaults_are_off_without_the_opt_in(host):
    home, calls = host
    assert default_plugins.fetch_policy(home) == "denied"
    assert default_plugins.install_defaults(home, entries=[_entry("x")])["installed"] == []
    assert default_plugins.step_install_default_plugins().get("skipped")
    assert default_plugins.spawn_install(home) is False
    assert default_plugins.install_and_record(wait=1.0) == {"installed": [], "owed": []}
    assert calls == [], "nothing may be fetched, installed or spawned"
    assert not (home / "config.yaml").read_text(encoding="utf-8").count("_default_plugins_installed")


def test_the_opt_in_restores_upstream_behaviour(host, monkeypatch):
    home, calls = host
    monkeypatch.setenv("HERMES_FORK_ALLOW_DEFAULT_PLUGINS", "1")
    assert default_plugins.fetch_policy(home) == "allowed"
    assert default_plugins.install_defaults(home, entries=[_entry("x")])["installed"] == ["x"]
    assert ("install", "x") in calls
