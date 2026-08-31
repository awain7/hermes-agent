"""A ``/p/<profile>/`` prefix on a non-multiplexed gateway must not misdeliver.

The prefix is an address: the caller is naming WHICH agent the request is
for. The old behavior ignored it whenever ``gateway.multiplex_profiles`` was
off and answered as the process's own (single) profile — so a request
explicitly addressed to one agent was silently answered by a different one.
Observed live (Aug 2026): ``hermes peer dm mini/researcher`` was answered by
the mini's *default* agent, with no error on either side, because the mini
runs one LaunchDaemon per profile and only the default daemon hosted an
api_server.

The contract now: with multiplexing off, a prefix naming this process's own
profile is honored (peers address single-profile daemons this way without
knowing the host's topology); any other name fails closed with the existing
404, because a wrong-agent answer is strictly worse than an error.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server import _PROFILE_REJECTED, APIServerAdapter
from gateway.config import PlatformConfig


@pytest.fixture()
def adapter():
    # No gateway_runner configured -> multiplex_profiles is falsy, the exact
    # shape of a standalone single-profile daemon.
    return APIServerAdapter(PlatformConfig(enabled=True))


def _request(profile: str | None):
    return SimpleNamespace(match_info={} if profile is None else {"profile": profile})


class TestResolverWithMultiplexOff:
    def test_no_prefix_is_untouched(self, adapter):
        assert adapter._resolve_request_profile(_request(None)) is None

    def test_prefix_naming_own_profile_is_honored(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_matches_home",
            lambda name, home=None: name == "researcher",
        )
        assert adapter._resolve_request_profile(_request("researcher")) is None

    def test_prefix_naming_default_on_default_home_is_honored(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_matches_home",
            lambda name, home=None: name == "default",
        )
        assert adapter._resolve_request_profile(_request("default")) is None

    def test_prefix_naming_another_agent_fails_closed(self, adapter, monkeypatch):
        """The misdelivery case: addressed to researcher, running as default."""
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_matches_home",
            lambda name, home=None: name == "default",
        )
        assert adapter._resolve_request_profile(_request("researcher")) is _PROFILE_REJECTED

    def test_unresolvable_own_identity_fails_closed(self, adapter, monkeypatch):
        """If the process cannot prove who it is, it must not answer as anyone."""

        def boom(name, home=None):
            raise RuntimeError("no home")

        monkeypatch.setattr("hermes_cli.profiles.profile_matches_home", boom)
        assert adapter._resolve_request_profile(_request("researcher")) is _PROFILE_REJECTED


class TestMultiplexOnUnchanged:
    def test_served_profile_resolves(self, adapter, monkeypatch):
        adapter.gateway_runner = SimpleNamespace(
            config=SimpleNamespace(
                multiplex_profiles=True, multiplex_profile_allowlist=None
            )
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex, profile_allowlist: [("worker", object())],
        )
        assert adapter._resolve_request_profile(_request("worker")) == "worker"
        assert adapter._resolve_request_profile(_request("ghost")) is _PROFILE_REJECTED


@pytest.mark.asyncio
async def test_http_request_addressed_to_another_agent_is_404_not_answered(
    adapter, monkeypatch
):
    """End to end through the real middleware: the wrong-agent request must
    404 instead of being served — a body would mean the misdelivery is back."""
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "default"
    )

    async def handler(request):
        return web.json_response({"served_by": "default"})

    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_get("/p/{profile}/v1/test", handler)
    app.router.add_get("/v1/test", handler)

    async with TestClient(TestServer(app)) as cli:
        # Addressed to a different agent: refused.
        resp = await cli.get("/p/researcher/v1/test")
        assert resp.status == 404
        body = await resp.json()
        assert "profile" in str(body.get("error", "")).lower()

        # Addressed to this agent, and unaddressed: both served.
        assert (await cli.get("/p/default/v1/test")).status == 200
        assert (await cli.get("/v1/test")).status == 200


class TestAgentIsStampedWithRequestProfile:
    """The agent built for a ``/p/<profile>/`` request carries that profile.

    Unlike the gateway's /bg path, this one genuinely exercises the
    stored-prompt reuse guard: the API server passes BOTH a persisted
    ``session_id`` and a ``conversation_history``, which satisfies the
    ``if conversation_history and agent._session_db:`` gate in
    ``conversation_loop._restore_or_build_system_prompt()``. That guard skips
    its ``Profile:`` comparison whenever the READER is unstamped, so an
    unstamped agent here would silently reuse a cached system prompt built
    under a different profile — and every profile shares one ``state.db``.
    """

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_prefix_profile_is_passed_to_the_agent(self, adapter):
        from unittest.mock import MagicMock, patch as _patch

        from gateway.platforms import api_server as _api

        token = _api._api_request_profile.set("travel")
        try:
            with _patch("gateway.run._resolve_runtime_agent_kwargs") as mk, \
                 _patch("gateway.run._resolve_gateway_model") as mm, \
                 _patch("gateway.run._load_gateway_config") as mc, \
                 _patch("run_agent.AIAgent") as mock_agent_cls:
                mk.return_value = {"api_key": "test-key", "base_url": None,
                                   "provider": None, "api_mode": None,
                                   "command": None, "args": []}
                mm.return_value = "test/model"
                mc.return_value = {}
                mock_agent_cls.return_value = MagicMock()

                adapter._create_agent()

            assert mock_agent_cls.call_args.kwargs["profile"] == "travel"
        finally:
            _api._api_request_profile.reset(token)

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_no_prefix_stamps_nothing(self, adapter):
        from unittest.mock import MagicMock, patch as _patch

        with _patch("gateway.run._resolve_runtime_agent_kwargs") as mk, \
             _patch("gateway.run._resolve_gateway_model") as mm, \
             _patch("gateway.run._load_gateway_config") as mc, \
             _patch("run_agent.AIAgent") as mock_agent_cls:
            mk.return_value = {"api_key": "test-key", "base_url": None,
                               "provider": None, "api_mode": None,
                               "command": None, "args": []}
            mm.return_value = "test/model"
            mc.return_value = {}
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

        assert mock_agent_cls.call_args.kwargs["profile"] is None

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_explicit_profile_survives_the_executor_thread_hop(self, adapter):
        """The profile must be PLUMBED, not read from the ContextVar.

        _create_agent runs inside `_run()` on a run_in_executor thread, and
        ContextVars do not follow that hop -- the code says so itself where it
        captures `request_profile = _api_request_profile.get()` on the loop
        thread before defining `_run()`. `_profile_scope()` re-enters the
        HERMES_HOME scope there but does not re-set _api_request_profile, so an
        implementation that only read the ContextVar would stamp None on every
        real request while still passing a same-thread test. This test runs
        _create_agent on a worker thread with the ContextVar unset, exactly as
        production does.
        """
        import concurrent.futures
        from unittest.mock import MagicMock, patch as _patch

        from gateway.platforms import api_server as _api

        assert _api._api_request_profile.get() is None

        with _patch("gateway.run._resolve_runtime_agent_kwargs") as mk, \
             _patch("gateway.run._resolve_gateway_model") as mm, \
             _patch("gateway.run._load_gateway_config") as mc, \
             _patch("run_agent.AIAgent") as mock_agent_cls:
            mk.return_value = {"api_key": "test-key", "base_url": None,
                               "provider": None, "api_mode": None,
                               "command": None, "args": []}
            mm.return_value = "test/model"
            mc.return_value = {}
            mock_agent_cls.return_value = MagicMock()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(adapter._create_agent, profile="travel").result()

        assert mock_agent_cls.call_args.kwargs["profile"] == "travel"
