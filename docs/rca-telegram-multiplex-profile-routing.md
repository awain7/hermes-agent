# RCA: Telegram replies collapsed onto the default profile's bot in multiplex mode

**Status:** resolved by `fix: resolve Telegram adapter to the inbound profile in multiplex mode` (`0e640f136`) and `harden: stamp multiplex profile in the system prompt, invalidate stale cache` (`ea48fa6ab`)
**Severity:** P1 — with `gateway.multiplex_profiles: true`, every secondary profile's Telegram replies were delivered through the default profile's bot connection, and once a session was poisoned it kept replying with the wrong persona even after the routing bug was fixed.

## Summary

With `gateway.multiplex_profiles: true` and five profiles (default, travel, home, coding, daily) each wired to its own Telegram bot token, messages sent to any of the four secondary bots were answered correctly by the agent (right session, right persona in the *reasoning*) but delivered back through the **default bot's** Telegram connection — so every reply visually showed up in the default profile's chat regardless of which bot the user actually messaged.

Two independent bugs compounded the symptom, plus one config/process gotcha during triage:

1. A delivery-layer bug sent every reply through the wrong adapter.
2. Sessions created while bug (1) was live got a wrong persona's system prompt permanently baked into the session DB, and kept replaying it even after (1) was fixed.
3. The gateway process wasn't restarted after each code change during triage, which made partial fixes look ineffective.

## Root cause

### 1. Delivery layer: `self.adapters` is not profile-aware

`gateway/run.py` had 44 call sites of the form:

```python
adapter = self.adapters.get(source.platform)
```

`self.adapters` only holds the **default profile's** adapters. In multiplex mode, secondary-profile adapters live in `self._profile_adapters[profile_name]` (populated by `_start_one_profile_adapters`), and `gateway/authz_mixin.py::_adapter_for_source(source)` is the helper that resolves the correct one via `source.profile`. Every one of those 44 sites bypassed it and always resolved to the default bot's adapter — including the single most consequential one, the `GatewayStreamConsumer` construction inside `_run_agent_inner` (`gateway/run.py`, was line ~16851), which is what actually streams/edits the live reply back to Telegram. Session and agent routing were correct (session keys like `agent:travel:telegram:dm:...` were right, and the LLM turn ran under the right profile's config), but the network call that sent the answer went out over the default bot's connection.

Confirmed via a live diagnostic: after the fix, `_resolve_profile_home_for_source(source)` and `agent.secret_scope.get_secret("TELEGRAM_BOT_TOKEN")` both resolved correctly per profile even *before* this fix — the token/config layer (`gateway/config.py::_apply_env_overrides`, see `_scoped_env`) was already correct. The bug was purely in the delivery layer.

### 2. Session-cache layer: stale system prompt reused across profiles forever

`agent/conversation_loop.py::_restore_or_build_system_prompt` reuses a session's previously-stored `system_prompt` from the session DB (for prefix-cache efficiency) whenever `_stored_prompt_matches_runtime()` returns `True`. That function only compared the persisted `Model:`/`Provider:` lines — it had no way to know which profile's identity (SOUL.md) the stored prompt was built from.

Because bug (1) was live when the affected sessions (`agent:travel:...`, `agent:home:...`) were first created, their *first-ever turn* built and persisted a system prompt containing the default profile's persona (misattributed at prompt-build time via the same class of scope issue). Every subsequent turn for those sessions reused that poisoned prompt verbatim — so even after (1) was fixed and routing was verified correct, those two bots kept answering as the default persona until the sessions were manually reset with `/new`.

Verified directly against `state.db`: `sessions.system_prompt` for `agent:home:telegram:dm:...` and `agent:travel:telegram:dm:...` began with the default profile's SOUL.md content ("美股與選擇權投資助手").

### 3. Process/deployment gotcha (not a code bug, but caused a false negative during triage)

The running gateway process was started at 11:40:19, but `gateway/run.py` kept being edited until 11:59:14 in the same debugging window. Python doesn't hot-reload edited modules, so the first round of manual Telegram testing exercised old code and looked like the fix hadn't worked. `hermes --profile default gateway restart` after every code change is required to pick up changes — this project has no auto-reload for the gateway process.

## Fix

**Delivery layer** (`0e640f136`):
- Replaced all 44 occurrences of `self.adapters.get(source.platform)` / `self.adapters.get(event.source.platform)` in `gateway/run.py` with `self._adapter_for_source(source)` / `self._adapter_for_source(event.source)`.
- No behavior change for non-multiplex gateways: `_adapter_for_source` falls back to `self.adapters.get(platform)` when `source.profile` is unset.

**Session-cache hardening** (`ea48fa6ab`):
- Threaded a new `profile=` kwarg through `AIAgent.__init__` (`run_agent.py`) → `init_agent()` (`agent/agent_init.py`) → stored as `agent.profile`, mirroring the existing `platform=` kwarg.
- `agent/system_prompt.py::build_system_prompt_parts` stamps a `Profile: <name>` line alongside the existing `Model:`/`Provider:` lines whenever `agent.profile` is set.
- `agent/conversation_loop.py::_stored_prompt_matches_runtime` now rejects (forces a rebuild of) a stored prompt whenever the agent is running under a named profile and the stored `Profile:` line doesn't match exactly — **including when the line is absent entirely**, unlike the symmetric Model/Provider checks. A session whose stored prompt predates this field, or was written while profile routing was broken, must not be trusted just because the marker happens to be missing.
- `gateway/run.py` passes `profile=source.profile` when constructing the per-turn `AIAgent`.
- No-op for single-profile gateways: `agent.profile` is always `None` there, so the new check never fires.

## Recovery

Sessions poisoned **before** the session-cache hardening landed are not retroactively fixed by it (the guard only prevents *future* poisoning; it doesn't scan/repair existing rows). Send `/new` to any affected bot to start a fresh session — the next turn rebuilds the system prompt from the correct profile's `SOUL.md` under `_profile_runtime_scope`.

```bash
# restart to pick up a code change — always required, no hot reload
hermes --profile default gateway restart
```

Then in each affected Telegram chat: send `/new`, then re-test with a prompt like "你是哪個 profile？你的角色是什麼？" and confirm the persona matches that bot.

## Known follow-up (not yet fixed)

`gateway/run.py`'s startup log always prints `Channel directory built: 1 target(s)` in multiplex mode. `build_channel_directory()` (`gateway/channel_directory.py`) is keyed by `Platform` only and reads `self.adapters` (default profile's adapters), so secondary-profile channels never appear in it. This only affects tooling that resolves a delivery target *by channel name* (e.g. cron `deliver: telegram` by name) for a secondary profile — normal inbound/outbound chat is unaffected. Fixing it properly requires making the channel directory profile-aware (e.g. keyed by `(profile, platform)` instead of `platform` alone).
