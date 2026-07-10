# token-guard — Design Spec v1

General-purpose hermes plugin. Two-part philosophy:
- **Part 1 (safe, always active when plugin enabled)**: observation only — never changes model behavior. Cost ledger, cache guard, config audit.
- **Part 2 (optional toggles, OFF by default)**: each toggle = a minimal config.yaml diff with backup + one-command rollback. Enabling requires an explicit two-step confirm showing "savings → risk → safeguard".

Target environment: see [`API_CONTRACT_PLUGINS.md`](../../API_CONTRACT_PLUGINS.md) (READ IT FULLY FIRST). Verify every import/signature against your LOCAL hermes checkout `<HERMES_HOME>\hermes-agent` (v0.18.0) — not a newer research clone.

## Files (live in `plugins/token-guard/`)

```
token-guard/
├── plugin.yaml          # name: token-guard, version: 0.1.0, description (RU ok), author
├── __init__.py          # register(ctx) ONLY: wire hooks/commands/CLI; thin glue, no logic
├── ledger.py            # SQLite ledger (WAL) at get_hermes_home()/token-guard/ledger.db
├── cache_guard.py       # cache-bust detection + hit-rate stats (reads ledger)
├── audit.py             # config audit rules → list of findings (RU strings)
├── toggles.py           # part-2 toggles: enable/disable + backup/restore
├── report.py            # /cost and status text rendering (RU)
├── README.md            # RU: what it does, install, commands, toggles with risk cards
└── tests/
    └── test_plugin.py   # pytest; see Testing section
```

## Part 1 mechanics

### Ledger (ledger.py)
- SQLite at `get_hermes_home()/token-guard/ledger.db`, WAL mode, thread-safe via `plugins.plugin_utils.lazy_singleton` (or SingletonSlot) for the connection; all writes wrapped try/except — a ledger failure must NEVER break the host.
- Tables:
  - `requests(id INTEGER PK, ts REAL, session_id TEXT, task_id TEXT, turn_id TEXT, api_request_id TEXT, model TEXT, provider TEXT, api_mode TEXT, duration_ms REAL, input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT, finish_reason TEXT)`
  - `errors(id PK, ts, session_id TEXT, model TEXT, error_type TEXT, status_code INT, retry_count INT, retryable INT)`
  - `tool_calls(id PK, ts, session_id TEXT, tool_name TEXT, duration_ms REAL)`
  - `events(id PK, ts, session_id TEXT, kind TEXT, detail TEXT)`  -- e.g. kind='cache_bust'
  - Indexes on (ts), (session_id). Prune rows older than 90 days (lazy: at most once per process, on first write).
- Feed from hooks (all plain `def`, fast, no heavy work inline):
  - `post_api_request` → insert into requests (fields straight from payload + usage dict).
  - `api_request_error` → insert into errors.
  - `post_tool_call` → insert into tool_calls (tool_name, duration_ms).
- Dollars are NOT computed in hooks. Report-time enrichment: read `estimated_cost_usd`/`actual_cost_usd` from `SessionDB(read_only=True).get_session(session_id)` for sessions in window; per-model token breakdown from ledger. Optional best-effort per-model $ via `agent.usage_pricing.estimate_usage_cost` in try/except — if import fails, show tokens only.

### Cache guard (cache_guard.py)
- On each `post_api_request`: compare (model, provider) with the previous request of the same session (in-memory dict + ledger fallback). Changed mid-session → insert events row kind='cache_bust' with detail "model X→Y".
- Hit-rate stat (report-time): `sum(cache_read_tokens) / sum(input_tokens)` over window, per model and total. Warning entry in report if a session with ≥5 requests has hit-rate < 0.3.

### Audit (audit.py)
Pure function: `run_audit() -> list[Finding]`, Finding = dict(severity, title_ru, detail_ru, fix_hint_ru). Reads config via `load_config_readonly()` + ledger stats. Checks v1:
1. `prompt_caching.cache_ttl` unset/5m while gateway sessions exist → suggest 1h toggle.
2. `auxiliary.compression.model` set? If set: best-effort context-window check vs main model via `agent.model_metadata` (try/except; if lookup unavailable → severity=info "проверьте вручную"). If unset → info: суммаризация идёт основной моделью (дорого).
3. `delegation.model` unset → suggest cheap_delegation toggle.
4. Enabled toolsets with ZERO tool_calls in ledger over 14+ days of data (only if ledger has ≥14 days of history; map tool→toolset best-effort via `model_tools.get_toolset_for_tool` try/except) → «включено, но не используется — кандидат на отключение, −N токенов схем» (do NOT auto-disable).
5. `plugins` section sanity: token_guard toggles state vs backup file consistency.

### Commands
- Slash `/cost [days]` (default 7): totals (requests, tokens by bucket, $ if available), top-5 models by tokens, top-5 sessions by $, cache hit-rate + cache_bust count, error/retry count, active toggles line. RU text, compact monospace-friendly.
- Slash `/tokenguard <status|audit|enable <toggle> [confirm]|disable <toggle>|set-cheap-model <provider> <model>>`.
  - `enable X` without `confirm` → print risk card (экономия/риск/страховка) + «повторите: /tokenguard enable X confirm».
  - `enable X confirm` → apply via toggles.py, reply what changed.
  - `disable X` → restore, no confirm needed.
- CLI `hermes token-guard ...` mirroring the same subcommands via register_cli_command.
- Handler contract: `fn(raw_args: str) -> str` (may be sync). No per-chat state needed in v1.

## Part 2 mechanics (toggles.py)

Toggle registry (v1): `cheap_aux`, `cheap_delegation`, `cache_1h`. Also recognized but NOT implemented: `cron_cascade`, `context_editing` → reply «зарезервировано, появится в следующей версии».

- Backup file `get_hermes_home()/token-guard/config_backup.json`: `{toggle: {dotted.key: old_value_or_null}}`. Written BEFORE first change; `disable` restores each key (null → remove key via save_config path or set to empty per set_config_value semantics — verify what set_config_value does for removal; if removal unsupported, store and restore previous literal value only, and if key was absent, restore by writing previous absent-marker via save_config bulk edit).
- Writes via `from hermes_cli.config import set_config_value` (dotted keys). Read current via `load_config()` + `cfg_get`.
- `cheap_aux`: requires `token_guard.cheap_model` + `token_guard.cheap_provider` set (via set-cheap-model). Sets `auxiliary.compression.model/provider`, `auxiliary.title_generation.model/provider`, `auxiliary.session_search.model/provider`. Risk card mentions: контекст модели компрессии должен быть ≥ окна основной; audit rechecks after enable.
- `cheap_delegation`: sets `delegation.model` + `delegation.provider` from the same cheap-model setting.
- `cache_1h`: sets `prompt_caching.cache_ttl: "1h"`.
- Config self-section read keys: `token_guard.cheap_model`, `token_guard.cheap_provider` (strings).

## Risk cards (RU, used by enable flow and README)
- cheap_aux: Экономия: суммаризация/заголовки/поиск-по-сессиям — самые частые служебные вызовы. Риск: слабое резюме при сжатии теряет детали навсегда; модель с коротким окном молча выкидывает середину. Страховка: ставьте длинноконтекстную флеш-модель; аудит проверяет окно; откат одной командой.
- cheap_delegation: Экономия: сабагенты (поиск/сбор) на дешёвой модели — до −50% на делегированиях. Риск: глубокие рассуждения в сабагенте просядут. Страховка: тяжёлое не делегировать или отключить тумблер; откат одной командой.
- cache_1h: Экономия: чтение кэша 10% цены; 1h окно выгодно при паузах в диалоге. Риск: запись в кэш при 1h стоит дороже (2× vs 1.25×) — при очень редких сообщениях может выйти в ноль. Страховка: /cost покажет hit-rate; откат одной командой.

## Non-negotiables
- stdlib only (sqlite3, json, threading, datetime, pathlib, argparse). No pip deps.
- Hooks: plain `def`, fast (single INSERT max), try/except everything, no raising.
- No heavy imports at module top of __init__.py; host-internal imports (`hermes_state`, `agent.usage_pricing`, `model_tools`, `agent.model_metadata`) ONLY inside functions with try/except and graceful degradation flags.
- All state under `get_hermes_home()/token-guard/` via `from hermes_constants import get_hermes_home`.
- User-facing strings in Russian, plain language. Code comments/identifiers in English.
- Never modify core files.

## Testing (tests/test_plugin.py)
Follow the isolation pattern from `tests/plugins/test_disk_cleanup_plugin.py` in the local checkout:
- monkeypatch env HERMES_HOME → tmp_path; ensure modules re-read it (import inside tests or reload).
- Load plugin package via importlib with `hermes_plugins` namespace trick so relative imports work.
- Fake ctx object recording register_* calls → assert registration set.
- Feed synthetic hook payloads (post_api_request with usage dict; api_request_error; post_tool_call) → assert ledger rows.
- Cache guard: two synthetic requests same session different model → events row cache_bust.
- Toggles: fake minimal config.yaml in tmp HERMES_HOME; enable cheap_aux (with cheap model preset) → keys written + backup file; disable → restored byte-identical.
- Audit: synthetic config → expected findings present.
- Report: renders without exception on empty and populated ledger.
Run with the hermes venv python: `<HERMES_HOME>\hermes-agent\venv\Scripts\python.exe -m pytest <tests> -v` with `sys.path` including the local checkout root (conftest.py inserting the path is fine).

## Out of scope v1
HTML report, telegram setup wizard, context_editing, cron_cascade, auto-apply of audit findings.
