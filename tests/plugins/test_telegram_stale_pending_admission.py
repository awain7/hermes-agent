"""Fork: the stale-pending-update guard through real PTB dispatch and update admission.

tests/gateway/test_telegram_stale_pending_guard.py pins the guard's age rules. This file pins
where it runs: alone in the first handler group, so a stale queued message is dropped before any
plugin (the usual group -1 slot included) or core handler, even under Defaults(block=False), and
is receipted, so a redelivery stops at admission. Fresh messages and callback taps on old messages
still reach group -1 plugins and core.
"""

import asyncio
import logging
import time

import pytest

pytest.importorskip("telegram")
from telegram import Update
from telegram.ext import Defaults, TypeHandler

from tests.plugins.test_telegram_update_admission import connected, update


def _aged(bot, uid, age_s, kind="text"):
    payload = update(bot, uid, kind).to_dict()
    message = payload["callback_query"]["message"] if kind == "callback" else payload["message"]
    message["date"] = int(time.time() - age_s)
    return Update.de_json(payload, bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("nonblocking_defaults", [False, True])
async def test_stale_update_dropped_before_plugins_and_receipted(monkeypatch, caplog, nonblocking_defaults):
    caplog.set_level(logging.INFO)
    async with connected(monkeypatch) as (adapter, app, delivered):
        adapter._pending_message_max_age_s = adapter._PENDING_MESSAGE_MAX_AGE_S  # the fixture disables it
        guard_group = adapter._STALE_PENDING_GUARD_GROUP
        assert next(iter(app.handlers)) == guard_group and len(app.handlers[guard_group]) == 1
        if nonblocking_defaults:
            # Core then runs as PTB tasks; the guard's ApplicationHandlerStop must still hold.
            monkeypatch.setattr(app.bot, "_defaults", Defaults(block=False))
        seen, deep = [], []

        async def plugin(incoming, context):
            seen.append(incoming.update_id)

        async def deep_plugin(incoming, context):
            deep.append(incoming.update_id)

        app.add_handler(TypeHandler(Update, plugin), group=-1)
        # A plugin far below the usual slot still runs after the guard.
        app.add_handler(TypeHandler(Update, deep_plugin), group=-1000)
        for uid, age, kind in ((10, 3600, "text"), (10, 3600, "text"), (11, 5, "text"), (12, 3600, "callback")):
            await app.process_update(_aged(app.bot, uid, age, kind))
        await app.stop()  # PTB awaits the handlers it ran as tasks
        await asyncio.gather(*adapter._pending_text_batch_tasks.values())

        assert seen == [11, 12] and deep == [11, 12]
        assert [event.text for event in delivered] == ["hello"]
        assert len(app.bot.request.answers) == 1  # the callback tap was served
        assert "111:10" in adapter._seen_update_ids and not adapter._inflight_update_ids
        messages = [record.getMessage() for record in caplog.records]
        # The redelivery stopped at its receipt, before the guard or any handler ran.
        assert sum("Dropping stale queued message" in m for m in messages) == 1
        assert not any("matched no core handler" in m for m in messages)
