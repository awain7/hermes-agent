"""Fork regression test: a multiplex gateway's file logs survive the start of its session listener.

``start_gateway`` swaps the queued file handlers for per-profile routers
(``_enable_multiplex_log_routing``) and only then builds the uvicorn session listener
(``start_gateway_runtime_api`` -> ``build_listener_server``). uvicorn's default ``log_config``
runs ``logging.config.dictConfig``, whose ``_clearExistingHandlers()`` calls
``logging.shutdown`` on every handler in the process, and a closed
``_ProfileRoutingFileHandler`` never writes again. After the 2026-10-19 deploy the bots kept
working while gateway.log, agent.log, errors.log and every ``profiles/<name>/logs`` file stayed
silent from that point on. Fork-owned file name; listed in
``.github/workflows/fork-python-tests.yml``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

import hermes_logging
from hermes_cli.web_server_lifecycle import build_listener_server
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(autouse=True)
def _fresh_queued_logging():
    """Start and end with no queued file handlers (other tests may leave some behind)."""
    hermes_logging._logging_initialized = False
    hermes_logging._reset_queued_handlers()
    root = logging.getLogger()
    previous_level, pre_existing = root.level, list(root.handlers)
    yield
    hermes_logging._reset_queued_handlers()
    for handler in list(root.handlers):
        if handler not in pre_existing:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(previous_level)
    hermes_logging._logging_initialized = False


async def _app(scope, receive, send):  # never served; the listener is only built
    return None


def _build_session_listener():
    """The call ``gateway/run_api.start_gateway_api`` makes."""
    return build_listener_server(
        _app, "127.0.0.1", 0, auth_required=False, timeout_graceful_shutdown=5, lifespan="off"
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig") if path.exists() else ""


def test_session_listener_leaves_process_logging_to_hermes_logging():
    config, _server = _build_session_listener()
    assert config.log_config is None


def test_routed_file_logs_keep_writing_after_the_session_listener_is_built():
    home = Path(os.environ["HERMES_HOME"])
    profile = home / "profiles" / "coding"
    (profile / "logs").mkdir(parents=True)
    hermes_logging.setup_logging(hermes_home=home, mode="gateway")
    assert hermes_logging.enable_profile_log_routing([home, profile]) is True

    _build_session_listener()

    routers = [
        handler for handler in hermes_logging._queued_file_handlers
        if isinstance(handler, hermes_logging._ProfileRoutingFileHandler)
    ]
    assert routers, "routing did not install profile routers"
    assert not any(router._router_closed for router in routers), "listener closed the routers"

    log = logging.getLogger("gateway.run")
    log.info("after-listener default info")
    log.warning("after-listener default warning")
    token = set_hermes_home_override(str(profile))
    try:
        log.info("after-listener profile info")
    finally:
        reset_hermes_home_override(token)
    hermes_logging.flush_log_queue()

    assert "after-listener default info" in _read(home / "logs" / "gateway.log")
    assert "after-listener default info" in _read(home / "logs" / "agent.log")
    assert "after-listener default warning" in _read(home / "logs" / "errors.log")
    assert "after-listener profile info" in _read(profile / "logs" / "gateway.log")
    assert "after-listener profile info" in _read(profile / "logs" / "agent.log")
    assert "after-listener profile info" not in _read(home / "logs" / "agent.log")
