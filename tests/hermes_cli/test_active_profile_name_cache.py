"""(fork) Tests for the remembered answers in ``get_active_profile_name``.

Every lookup used to be up to three ``Path.resolve()`` filesystem calls. The gateway asks on its
event loop — ``gateway_health._safe_profile`` runs on every runtime-status transition — and the stack
dump the loop-liveness watchdog took before killing a starved gateway (2026-10-04) had the loop
thread parked in exactly that ``resolve()``.

The answer itself must not change, so most of these tests compare against the plain uncached
calculation (same shape as ``tests/test_hermes_home_key_cache.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_constants as hc
from hermes_cli import profiles


def _uncached() -> str:
    """The calculation as it was before answers were remembered."""
    resolved = hc.get_hermes_home().resolve()
    if resolved == profiles._get_default_hermes_home().resolve():
        return "default"
    profiles_root = profiles._get_profiles_root().resolve()
    try:
        parts = resolved.relative_to(profiles_root).parts
        if len(parts) == 1 and profiles._PROFILE_ID_RE.match(parts[0]):
            return parts[0]
    except ValueError:
        pass
    return "custom"


@pytest.fixture(autouse=True)
def _clear_cache():
    profiles.reset_active_profile_name_cache()
    yield
    profiles.reset_active_profile_name_cache()


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A Hermes root holding two named profiles, served as the process home."""
    root = tmp_path / "root"
    for name in ("coder", "daily"):
        (root / "profiles" / name).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def override():
    """Bind a context-local home the way a multiplexed turn does; always unbound afterwards."""
    tokens = []

    def _bind(path) -> None:
        tokens.append(hc.set_hermes_home_override(str(path)))

    yield _bind
    for token in reversed(tokens):
        hc.reset_hermes_home_override(token)


class TestSameAnswerAsBefore:
    """Remembering an answer must not change what comes back."""

    def test_the_root_is_default(self, root):
        assert profiles.get_active_profile_name() == _uncached() == "default"

    def test_a_named_profile_home_from_the_environment(self, root, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coder"))
        assert profiles.get_active_profile_name() == _uncached() == "coder"

    def test_a_named_profile_home_from_the_override(self, root, override):
        override(root / "profiles" / "daily")
        assert profiles.get_active_profile_name() == _uncached() == "daily"

    @pytest.mark.parametrize("case", ["elsewhere", "nested", "bad_id", "missing"])
    def test_everything_else_is_custom(self, root, tmp_path, override, case):
        target = {
            "elsewhere": tmp_path / "elsewhere",
            "nested": root / "profiles" / "coder" / "nested",
            "bad_id": root / "profiles" / "Not A Profile Id",
            "missing": tmp_path / "not_there",
        }[case]
        if case != "missing":
            target.mkdir(parents=True)
        override(target)
        assert profiles.get_active_profile_name() == _uncached() == "custom"

    def test_repeated_lookups_keep_agreeing(self, root, override):
        override(root / "profiles" / "coder")
        for _ in range(3):
            assert profiles.get_active_profile_name() == _uncached() == "coder"


class TestHomeChanges:
    def test_each_bound_home_gets_its_own_answer(self, root, override):
        # One multiplexed gateway serves every profile from one process: an answer remembered for
        # one home must never be handed to a turn bound to another.
        assert profiles.get_active_profile_name() == "default"
        override(root / "profiles" / "coder")
        assert profiles.get_active_profile_name() == "coder"
        override(root / "profiles" / "daily")
        assert profiles.get_active_profile_name() == "daily"
        override(root)
        assert profiles.get_active_profile_name() == "default"

    def test_unbinding_returns_to_the_process_home(self, root):
        token = hc.set_hermes_home_override(str(root / "profiles" / "coder"))
        try:
            assert profiles.get_active_profile_name() == "coder"
        finally:
            hc.reset_hermes_home_override(token)
        assert profiles.get_active_profile_name() == "default"

    def test_a_different_root_is_a_different_answer(self, root, tmp_path, monkeypatch, override):
        # Same bound home, different root: "coder" under one, an unrelated path under the other.
        override(root / "profiles" / "coder")
        assert profiles.get_active_profile_name() == "coder"
        other = tmp_path / "other_root"
        other.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(other))
        assert profiles.get_active_profile_name() == _uncached() == "custom"


class TestWhatGetsRemembered:
    def test_a_missing_home_is_not_remembered(self, root, tmp_path, override):
        # The answer can change once the directory is created (part of the path may turn out to
        # be a link), so it must not stick.
        override(tmp_path / "not_there")
        profiles.get_active_profile_name()
        assert profiles._ACTIVE_PROFILE_NAME_CACHE == {}

    def test_a_home_created_later_picks_up_the_real_answer(self, root, override):
        later = root / "profiles" / "later"
        override(later)
        before = profiles.get_active_profile_name()
        assert profiles._ACTIVE_PROFILE_NAME_CACHE == {}
        later.mkdir()
        assert profiles.get_active_profile_name() == _uncached() == before == "later"
        assert list(profiles._ACTIVE_PROFILE_NAME_CACHE.values()) == ["later"]

    def test_a_relative_home_is_not_remembered(self, root, tmp_path, monkeypatch):
        # A relative path resolves against the cwd, which is not part of the key.
        (tmp_path / "rel_home").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HERMES_HOME", "rel_home")
        assert profiles.get_active_profile_name() == _uncached()
        assert profiles._ACTIVE_PROFILE_NAME_CACHE == {}

    def test_the_cache_stays_small(self, root, tmp_path, override):
        # A process sees a handful of homes; a test session sees thousands of tmp dirs.
        for index in range(profiles._ACTIVE_PROFILE_NAME_CACHE_MAX + 5):
            home = tmp_path / f"home_{index}"
            home.mkdir()
            override(home)
            profiles.get_active_profile_name()
        assert len(profiles._ACTIVE_PROFILE_NAME_CACHE) <= profiles._ACTIVE_PROFILE_NAME_CACHE_MAX

    def test_reset_forgets_everything(self, root):
        profiles.get_active_profile_name()
        assert profiles._ACTIVE_PROFILE_NAME_CACHE
        profiles.reset_active_profile_name_cache()
        assert profiles._ACTIVE_PROFILE_NAME_CACHE == {}


class TestSymlinks:
    def test_a_linked_profile_home_resolves_to_its_target(self, root, tmp_path, override):
        link = tmp_path / "link_to_coder"
        try:
            link.symlink_to(root / "profiles" / "coder", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform or account cannot create symlinks")
        override(link)
        assert profiles.get_active_profile_name() == _uncached() == "coder"


class _ResolveCounter:
    def __init__(self, monkeypatch):
        self.calls = 0
        real_resolve = Path.resolve

        def counting_resolve(path, *args, **kwargs):
            self.calls += 1
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", counting_resolve)


class TestLookupsDoNotHitTheDisk:
    """The reason this cache exists."""

    def test_repeat_lookups_resolve_nothing(self, root, monkeypatch):
        counter = _ResolveCounter(monkeypatch)

        profiles.get_active_profile_name()
        assert counter.calls > 0, "the first lookup has to resolve the home"
        after_first = counter.calls
        for _ in range(50):
            profiles.get_active_profile_name()

        assert counter.calls == after_first, (
            f"get_active_profile_name resolved paths on the filesystem "
            f"{counter.calls - after_first} extra times across 50 calls")

    def test_the_gateway_health_emitter_resolves_nothing_once_warm(self, root, monkeypatch, override):
        # gateway_health._safe_profile is the caller that runs ON the gateway event loop, once per
        # runtime-status transition, under whichever profile's adapter published it.
        from agent.monitoring.gateway_health import _safe_profile

        homes = [root, root / "profiles" / "coder", root / "profiles" / "daily"]
        expected = ["default", "coder", "daily"]
        for home in homes:  # warm: one resolve round per served home
            override(home)
            _safe_profile()
        counter = _ResolveCounter(monkeypatch)

        seen = []
        for _ in range(20):
            for home in homes:
                override(home)
                seen.append(_safe_profile())

        assert seen == expected * 20
        assert counter.calls == 0, (
            f"_safe_profile made {counter.calls} filesystem resolve call(s) on a warm cache")
