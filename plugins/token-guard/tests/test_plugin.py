"""Tests for the token-guard plugin.

Loads the plugin package fresh per test (via importlib + the
``hermes_plugins`` namespace-package trick, same as
``tests/plugins/test_disk_cleanup_plugin.py`` in the local hermes-agent
checkout) so module-level singletons (ledger connection, cache-guard
in-memory dict) always rebuild against the current test's HERMES_HOME.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin():
    """Import (fresh) the token-guard package as hermes_plugins.token_guard."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns

    # Drop any submodules from a previous test's load so `from . import X`
    # re-resolves against the fresh __init__ exec below instead of reusing
    # a stale cached submodule tied to the previous HERMES_HOME.
    for name in list(sys.modules):
        if name == "hermes_plugins.token_guard" or name.startswith("hermes_plugins.token_guard."):
            del sys.modules[name]

    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.token_guard",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.token_guard"
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules["hermes_plugins.token_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_config(hermes_home: Path, data: dict) -> None:
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


class FakeCtx:
    """Records register_* calls instead of touching a real PluginManager."""

    def __init__(self):
        self.hooks = {}
        self.commands = {}
        self.cli_commands = {}

    def register_hook(self, event, callback):
        self.hooks.setdefault(event, []).append(callback)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_wires_hooks_and_commands(self, _isolate_hermes_home):
        pi = _load_plugin()
        ctx = FakeCtx()
        pi.register(ctx)

        assert set(ctx.hooks.keys()) == {"post_api_request", "api_request_error", "post_tool_call"}
        assert "cost" in ctx.commands
        assert "tokenguard" in ctx.commands
        assert "token-guard" in ctx.cli_commands


# ---------------------------------------------------------------------------
# Ledger hooks
# ---------------------------------------------------------------------------

class TestLedgerHooks:
    def test_post_api_request_writes_row(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(
            session_id="s1", task_id="t1", turn_id="tu1", api_request_id="r1",
            model="model-x", provider="anthropic", api_mode="anthropic_messages",
            api_duration=1.5, finish_reason="stop",
            usage={
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_tokens": 10, "cache_write_tokens": 5, "reasoning_tokens": 0,
            },
        )
        rows = pi.ledger.requests_in_window(1)
        assert len(rows) == 1
        assert rows[0]["model"] == "model-x"
        assert rows[0]["input_tokens"] == 100
        assert rows[0]["duration_ms"] == 1500.0

    def test_post_api_request_never_raises_without_usage(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(session_id="s2", model="m", provider="p")
        rows = pi.ledger.requests_in_window(1)
        assert any(r["session_id"] == "s2" for r in rows)
        assert rows[0]["input_tokens"] == 0

    def test_api_request_error_writes_row(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_api_request_error(
            session_id="s1", model="model-x", error_type="RateLimitError",
            status_code=429, retry_count=1, retryable=True,
        )
        errors = pi.ledger.errors_in_window(1)
        assert len(errors) == 1
        assert errors[0]["status_code"] == 429
        assert errors[0]["retryable"] == 1

    def test_post_tool_call_writes_row(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_tool_call(tool_name="read_file", duration_ms=12.3, session_id="s1")
        calls = pi.ledger.tool_calls_in_window(1)
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "read_file"


# ---------------------------------------------------------------------------
# Cache guard
# ---------------------------------------------------------------------------

class TestCacheGuard:
    def test_model_switch_mid_session_logs_cache_bust(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(session_id="s1", model="model-a", provider="anthropic", usage={})
        pi._on_post_api_request(session_id="s1", model="model-b", provider="anthropic", usage={})
        events = pi.ledger.events_in_window(1, kind="cache_bust")
        assert len(events) == 1
        assert "model-a" in events[0]["detail"]
        assert "model-b" in events[0]["detail"]

    def test_same_model_no_bust(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(session_id="s1", model="model-a", provider="anthropic", usage={})
        pi._on_post_api_request(session_id="s1", model="model-a", provider="anthropic", usage={})
        events = pi.ledger.events_in_window(1, kind="cache_bust")
        assert len(events) == 0

    def test_different_sessions_no_bust(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(session_id="s1", model="model-a", provider="anthropic", usage={})
        pi._on_post_api_request(session_id="s2", model="model-b", provider="anthropic", usage={})
        events = pi.ledger.events_in_window(1, kind="cache_bust")
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

class TestReport:
    def test_cost_renders_on_empty_ledger(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.report.render_cost("")
        assert "token-guard" in out
        assert "Нет данных" in out

    def test_cost_renders_with_data(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(
            session_id="s1", model="model-x", provider="anthropic",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        out = pi.report.render_cost("7")
        assert "model-x" in out
        assert "Запросов: 1" in out

    def test_status_renders_without_exception(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.report.render_status()
        assert "cheap_aux" in out
        assert "выключен" in out

    def test_audit_render_empty(self, _isolate_hermes_home):
        pi = _load_plugin()
        assert "замечаний нет" in pi.report.render_audit([])

    def test_audit_render_nonempty(self, _isolate_hermes_home):
        pi = _load_plugin()
        findings = [{"severity": "warning", "title_ru": "Тест", "detail_ru": "детали", "fix_hint_ru": "чините"}]
        out = pi.report.render_audit(findings)
        assert "Тест" in out and "детали" in out and "чините" in out


# ---------------------------------------------------------------------------
# Audit rules
# ---------------------------------------------------------------------------

class TestAudit:
    def test_delegation_unset_flags_finding(self, _isolate_hermes_home):
        pi = _load_plugin()
        findings = pi.audit.run_audit()
        titles = [f["title_ru"] for f in findings]
        assert any("Делегирование" in t for t in titles)

    def test_compression_unset_flags_finding(self, _isolate_hermes_home):
        pi = _load_plugin()
        findings = pi.audit.run_audit()
        titles = [f["title_ru"] for f in findings]
        assert any("Сжатие контекста" in t for t in titles)

    def test_cache_ttl_finding_needs_multiple_sessions(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi._on_post_api_request(session_id="only1", model="m", provider="p", usage={})
        findings = pi.audit.run_audit()
        assert not any("Кэш промптов" in f["title_ru"] for f in findings)

        pi._on_post_api_request(session_id="only2", model="m", provider="p", usage={})
        findings2 = pi.audit.run_audit()
        assert any("Кэш промптов" in f["title_ru"] for f in findings2)

    def test_audit_never_raises_on_empty_env(self, _isolate_hermes_home):
        pi = _load_plugin()
        # No config.yaml, no ledger rows at all — must not raise.
        findings = pi.audit.run_audit()
        assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------

class TestToggles:
    def test_enable_without_confirm_shows_risk_card(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.toggles.enable_flow("cache_1h", confirm=False)
        assert "Повторите" in out
        assert not pi.toggles.is_enabled("cache_1h")

    def test_enable_confirm_applies_and_backs_up_absent_key(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.toggles.enable_flow("cache_1h", confirm=True)
        assert "включён" in out
        assert pi.toggles.is_enabled("cache_1h")

        backup_path = _isolate_hermes_home / "token-guard" / "config_backup.json"
        assert backup_path.exists()
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        assert backup["cache_1h"]["prompt_caching.cache_ttl"] is None  # absent before enable

        cfg_text = (_isolate_hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "1h" in cfg_text

    def test_disable_removes_key_that_was_absent_before(self, _isolate_hermes_home):
        pi = _load_plugin()
        pi.toggles.enable_flow("cache_1h", confirm=True)
        out = pi.toggles.disable("cache_1h")
        assert "отключён" in out
        assert not pi.toggles.is_enabled("cache_1h")

        cfg_text = (_isolate_hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "cache_ttl" not in cfg_text
        assert "prompt_caching" not in cfg_text  # emptied parent pruned too

    def test_disable_restores_previous_literal_value(self, _isolate_hermes_home):
        _write_config(_isolate_hermes_home, {"prompt_caching": {"cache_ttl": "5m"}})
        pi = _load_plugin()
        pi.toggles.enable_flow("cache_1h", confirm=True)
        pi.toggles.disable("cache_1h")

        cfg_text = (_isolate_hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "5m" in cfg_text

    def test_unrelated_config_survives_enable_disable(self, _isolate_hermes_home):
        _write_config(_isolate_hermes_home, {"model": {"default": "keep-me"}})
        pi = _load_plugin()
        pi.toggles.enable_flow("cache_1h", confirm=True)
        pi.toggles.disable("cache_1h")

        cfg_text = (_isolate_hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "keep-me" in cfg_text

    def test_cheap_aux_requires_cheap_model_first(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.toggles.enable_flow("cheap_aux", confirm=True)
        assert "Сначала укажите" in out
        assert not pi.toggles.is_enabled("cheap_aux")

    def test_set_cheap_model_then_enable_cheap_aux(self, _isolate_hermes_home):
        pi = _load_plugin()
        out1 = pi.toggles.set_cheap_model("openrouter", "google/gemini-flash")
        assert "openrouter" in out1 and "google/gemini-flash" in out1

        out2 = pi.toggles.enable_flow("cheap_aux", confirm=True)
        assert "cheap_aux" in out2
        assert pi.toggles.is_enabled("cheap_aux")

        cfg_text = (_isolate_hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "google/gemini-flash" in cfg_text
        assert "openrouter" in cfg_text

        pi.toggles.disable("cheap_aux")
        assert not pi.toggles.is_enabled("cheap_aux")
        cfg_text_after = (_isolate_hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "auxiliary" not in cfg_text_after

    def test_reserved_toggle_message(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.toggles.enable_flow("cron_cascade", confirm=True)
        assert "зарезервировано" in out
        out2 = pi.toggles.enable_flow("context_editing", confirm=False)
        assert "зарезервировано" in out2

    def test_disable_when_not_enabled(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.toggles.disable("cache_1h")
        assert "не включён" in out

    def test_unknown_toggle(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi.toggles.enable_flow("does_not_exist", confirm=True)
        assert "Неизвестный переключатель" in out


# ---------------------------------------------------------------------------
# Slash / CLI command dispatch (thin glue in __init__.py)
# ---------------------------------------------------------------------------

class TestCommandDispatch:
    def test_tokenguard_status_subcommand(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi._handle_tokenguard("status")
        assert "cheap_aux" in out

    def test_tokenguard_help_on_empty_args(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi._handle_tokenguard("")
        assert "tokenguard" in out

    def test_tokenguard_unknown_subcommand(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi._handle_tokenguard("banana")
        assert "Неизвестная подкоманда" in out

    def test_tokenguard_enable_disable_roundtrip(self, _isolate_hermes_home):
        pi = _load_plugin()
        out1 = pi._handle_tokenguard("enable cache_1h")
        assert "Повторите" in out1
        out2 = pi._handle_tokenguard("enable cache_1h confirm")
        assert "включён" in out2
        out3 = pi._handle_tokenguard("disable cache_1h")
        assert "отключён" in out3

    def test_cost_command(self, _isolate_hermes_home):
        pi = _load_plugin()
        out = pi._handle_cost("")
        assert "token-guard" in out

    def test_cli_setup_and_handler_exist(self, _isolate_hermes_home):
        pi = _load_plugin()
        ctx = FakeCtx()
        pi.register(ctx)
        entry = ctx.cli_commands["token-guard"]
        assert callable(entry["setup_fn"])
        assert callable(entry["handler_fn"])
