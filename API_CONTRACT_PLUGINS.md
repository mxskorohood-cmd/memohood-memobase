# hermes-agent Plugin API Contract (verified 2026-07-06)

Source: code inspection of hermes-agent, verified against a live editable install (v0.18.0, commit 09693cd3 2026-07-04, venv Python 3.11.9). Paths below use `<HERMES_HOME>` for the hermes home directory (e.g. `%LOCALAPPDATA%\hermes` on Windows, `~/.local/share/hermes` on Linux). NOTE: a research clone may be a few days newer — always verify line numbers against your LOCAL checkout.

## 1. Plugin shape

- Manifest `plugins/<name>/plugin.yaml`, consumed keys: `name, version, description, author, requires_env (informational only), provides_tools, provides_hooks, kind (standalone default)`. **`pip_dependencies` is NOT supported for general plugins** (only memory-provider loader) — general plugins must be stdlib-only, vendor deps, or instruct pip install into hermes venv.
- Entry: `__init__.py` with `register(ctx)`. Loaded via importlib as `hermes_plugins.<slug>`.
- Discovery: bundled `plugins/` → user `~/.hermes/plugins/` (= `get_hermes_home()/plugins`) → project `./.hermes/plugins/` (env-gated) → pip entry points `hermes_agent.plugins`. Later wins on key collision. Discovery runs as side effect of importing `model_tools.py`; call `discover_plugins()` explicitly if reading plugin state earlier (idempotent).
- Gating: standalone plugins are opt-in via `plugins.enabled` list in config.yaml (`plugins.disabled` deny-list always wins). Local config currently has NO `plugins:` section — must add or run `hermes plugins enable <name>`.

## 2. PluginContext API (hermes_cli/plugins.py)

### register_tool
```python
ctx.register_tool(name: str, toolset: str, schema: dict, handler: Callable,
    check_fn=None, requires_env=None, is_async: bool=False,
    description: str="", emoji: str="", override: bool=False) -> None
```
- `toolset` — free-form string; becomes usable in `delegate_task(toolsets=[...])` automatically.
- Handler receives tool `args: dict` + registry kwargs (`parent_agent`, `task_id`, `session_id`, ...).
- delegate_task isolation VERIFIED (tools/delegate_tool.py:1044-1142): child CAN be restricted to only a plugin toolset via `delegate_task(toolsets=["my_toolset"])` with `role="leaf"` (default). Orchestrator role re-adds "delegation" toolset.
- `override=True` of built-in names requires operator opt-in `plugins.entries.<id>.allow_tool_override`.

### register_hook(event, callback)
VALID_HOOKS: `pre_tool_call, post_tool_call, transform_terminal_output, transform_tool_result, transform_llm_output, pre_llm_call, post_llm_call, pre_verify, pre_api_request, post_api_request, api_request_error, on_session_start, on_session_end, on_session_finalize, on_session_reset, subagent_start, subagent_stop, pre_gateway_dispatch, pre_approval_request, post_approval_response, kanban_task_claimed, kanban_task_completed, kanban_task_blocked`.

**CRITICAL: callbacks are invoked synchronously as `cb(**kwargs)`, NEVER awaited. `async def` hook callbacks silently no-op their awaited side effects. Write plain `def`, keep them FAST, never raise (host wraps try/except but be polite).**

Payloads (callback receives these as kwargs; accept `**kw` for forward-compat):
- `pre_tool_call(tool_name, args, task_id, session_id, tool_call_id, turn_id, api_request_id, middleware_trace, **kw)` → return `{"action":"block","message":str}` to short-circuit.
- `post_tool_call(tool_name, args, result: str, task_id, duration_ms, **kw)` → ignored.
- `pre_llm_call(session_id, task_id, turn_id, user_message, conversation_history, is_first_turn, model, platform, sender_id, **kw)` → return `{"context": str}` or `str` → appended to user message (cache-safe).
- `post_llm_call(session_id, task_id, turn_id, user_message, assistant_response, conversation_history, model, platform, **kw)` — once/turn, only successful turns. NO token/cost data here.
- `pre_api_request(task_id, turn_id, api_request_id, session_id, user_message, conversation_history, platform, model, provider, base_url, api_mode, api_call_count, request_messages, message_count, tool_count, approx_input_tokens, request_char_count, request={method,body}, **kw)` — once per real API call.
- `post_api_request(task_id, turn_id, api_request_id, session_id, platform, model, provider, base_url, api_mode, api_call_count, api_duration, started_at, ended_at, finish_reason, message_count, response_model, response=<sanitized>, usage=<dict>, assistant_message, assistant_content_chars, assistant_tool_call_count, **kw)`.
  - `usage` dict keys: `input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, request_count, prompt_tokens, total_tokens`. **No cost_usd**.
- `api_request_error(task_id, turn_id, api_request_id, api_call_count, error_type, error_message, status_code, retry_count, max_retries, retryable, reason, error={type,message}, request=<sanitized>, **kw)`.
- `on_session_start(session_id, model, platform, **kw)`; `on_session_end(session_id, completed, interrupted, model, platform, **kw)`.
- `subagent_start(parent_session_id, parent_turn_id, parent_subagent_id, child_session_id, child_subagent_id, child_role, child_goal, **kw)`; `subagent_stop(parent_session_id, child_role, child_summary, child_status, duration_ms, **kw)`.
- `pre_gateway_dispatch(event: MessageEvent, gateway: GatewayRunner, session_store, **kw)` → `{"action":"skip","reason":...}` | `{"action":"rewrite","text":...}` | `{"action":"allow"}`/None. `event.text`, `event.source.{chat_id,user_id,user_name,platform,chat_type,thread_id,message_id,...}`, `event.internal`. To reply on skip: from a PLAIN def callback do `asyncio.create_task(gateway.adapters[event.source.platform].send(event.source.chat_id, text))` (loop is live; async def callback would be dropped!).

Payloads pass through sanitizer: api_key/authorization/cookie-shaped keys redacted; truncation at HERMES_PLUGIN_PAYLOAD_MAX_CHARS (50000).

### register_command (slash, CLI + gateway/telegram)
```python
ctx.register_command(name: str, handler, description: str="", args_hint: str="")
# handler: fn(raw_args: str) -> str | None   — may be async def (properly awaited in CLI AND gateway)
```
- NO chat/user identity passed. For per-chat wizard state: capture identity via pre_gateway_dispatch keyed off event.source.chat_id; persist state in module dict / file under get_hermes_home()/<plugin>/.
- Name colliding with built-in command → silently rejected with a log warning.

### register_cli_command
```python
ctx.register_cli_command(name, help, setup_fn, handler_fn=None, description="")
# setup_fn(subparser): add argparse args; handler via subparser.set_defaults(func=...)
# → `hermes <name> ...`
```

### register_auxiliary_task
```python
ctx.register_auxiliary_task(key, *, display_name, description, defaults=None)
# defaults merged under {provider:"auto", model:"", base_url:"", api_key:"", timeout:60, extra_body:{}}
# user config auxiliary.<key> overrides plugin defaults
```

### ctx.llm (agent/plugin_llm.py)
```python
ctx.llm.complete(messages: List[dict], *, provider=None, model=None, temperature=None,
    max_tokens=None, timeout=None, agent_id=None, profile=None, purpose=None)
    -> PluginLlmCompleteResult  # .text, .provider, .model, .usage (incl. cost_usd best-effort), .audit
ctx.llm.complete_structured(*, instructions, input, json_schema=None, json_mode=False,
    schema_name=None, system_prompt=None, ...) -> PluginLlmStructuredResult
# + acomplete/acomplete_structured async twins
```
- model/provider/agent_id/profile overrides are fail-closed: need `plugins.entries.<id>.llm.allow_*_override` in config; without config block plugin still gets complete() on host's active model.

### Config access (no ctx.config — import hermes_cli.config)
```python
from hermes_cli.config import load_config, load_config_readonly, cfg_get, set_config_value, save_config
set_config_value(key: str, value: str)  # safe single-key writer, dotted paths, routes API keys to .env
save_config(config: dict, *, strip_defaults=True, preserve_keys=None)  # bulk
```

### ctx.inject_message(content, role="user") -> bool
CLI-only; returns False in gateway. Proactive gateway messaging: `gateway.adapters[platform].send(chat_id, text)` (async) via the gateway object from pre_gateway_dispatch.

## 3. Cost/usage data

`hermes_state.py` sessions table columns: `input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count, message_count, tool_call_count, billing_provider, billing_base_url, billing_mode, estimated_cost_usd, actual_cost_usd, cost_status, cost_source, pricing_version`.
```python
from hermes_state import SessionDB, DEFAULT_DB_PATH  # top-level module
db = SessionDB(read_only=True)   # file:...?mode=ro — no write lock, safe to poll
row = db.get_session(session_id)  # dict with all columns
```
**Recommendation**: ledger = per-request rows from post_api_request hook (token buckets, model, duration); DOLLARS from sessions.estimated_cost_usd / actual_cost_usd (host-priced, authoritative). Per-request $ estimate if needed: `agent.usage_pricing.estimate_usage_cost(model_name, CanonicalUsage(...), provider=, base_url=)` — wrap in try/except, treat as best-effort.

## 4. Reference plugin & tests

- `plugins/disk-cleanup/` — minimal general plugin: plugin.yaml + __init__.py (register: 2 hooks + 1 slash command) + library module. State under `get_hermes_home()/disk-cleanup/`.
- Test pattern: `tests/plugins/test_disk_cleanup_plugin.py` — monkeypatch HERMES_HOME to tmp; load library via spec_from_file_location; register `hermes_plugins` namespace package so relative imports work. Richer example: `plugins/kanban/` + its tests.

## 5. Gotchas

- Hooks sync-only (see above). Slash commands may be async.
- Thread safety: use `plugins.plugin_utils.lazy_singleton` / `SingletonSlot` (stdlib-only, importable).
- Always `from hermes_constants import get_hermes_home` for state paths; `display_hermes_home()` for user-facing strings.
- Keep heavy imports inside functions (import-time weight slows every hermes invocation).
- Core-file rule: plugins must NOT modify core files.
- `pre_verify` must self-throttle on `attempt`.

## 6. Local environment

- HERMES_HOME: `<HERMES_HOME>` (e.g. `%LOCALAPPDATA%\hermes` on Windows; config.yaml present; NO plugins/ dir yet; NO plugins: config section yet).
- hermes 0.18.0 editable install at `<HERMES_HOME>\hermes-agent` (git checkout 09693cd3). Venv: `<HERMES_HOME>\hermes-agent\venv` (Python 3.11.9). `hermes.exe` on PATH from that venv.
- Use the venv python for tests, not a system python: `<HERMES_HOME>\hermes-agent\venv\Scripts\python.exe`.
