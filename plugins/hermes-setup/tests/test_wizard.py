"""Tests for wizard.py's state machine: happy path, cancel, wrong-key
re-ask, skip-already-set-key, plus the CLI command and the
pre_gateway_dispatch hook. HERMES_HOME is redirected to a fresh tmp dir by
the ``setup_plugin`` fixture (conftest.py) -- no real config.yaml/.env is
ever touched. All network-capable live checks go through a monkeypatched
``registry._http_get`` -- never a real HTTP call.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class _FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {"success": True}

    def json(self):
        return self._json_data


@pytest.fixture()
def no_network(setup_plugin, monkeypatch):
    """Every KEY_SPEC with a live_check hits the network via
    registry._http_get -- stub it out so wizard tests that fill in real
    keys never touch the network."""
    monkeypatch.setattr(setup_plugin.registry, "_http_get", lambda url, headers=None: _FakeResp())
    return setup_plugin


def _load_cfg(setup_plugin):
    from hermes_cli.config import load_config

    return load_config()


# ---------------------------------------------------------------------------
# Greeting / menu
# ---------------------------------------------------------------------------


def test_start_wizard_returns_menu(setup_plugin):
    wizard = setup_plugin.wizard
    reply = wizard.start_wizard("chat1", "user1")
    assert "1 —" in reply and "2 —" in reply
    assert wizard.is_active("chat1")


def test_menu_unrecognized_reply_reasks(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "user1")
    reply = wizard.handle_message("chat1", "user1", "banana")
    assert "Не понял ответ" in reply
    assert wizard.is_active("chat1")


def test_different_user_cannot_hijack_wizard(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "owner")
    reply = wizard.handle_message("chat1", "intruder", "1")
    assert reply is None
    # the wizard is still active for the real owner, untouched
    assert wizard.is_active("chat1")


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_mid_menu(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "user1")
    reply = wizard.handle_message("chat1", "user1", "отмена")
    assert "отменена" in reply
    assert not wizard.is_active("chat1")


def test_cancel_mid_key_entry(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")  # choose one plugin
    wizard.handle_message("chat1", "user1", "1")  # token-guard, completes instantly (0 keys)
    # Re-pick memobase to reach an actual key question
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    wizard.handle_message("chat1", "user1", "2")  # memobase
    assert wizard.is_active("chat1")
    reply = wizard.handle_message("chat1", "user1", "STOP")
    assert "отменена" in reply
    assert not wizard.is_active("chat1")


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_expired_wizard_resets(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "user1")
    entry = wizard._get_entry("chat1")
    entry["last_activity"] = time.time() - (16 * 60)
    wizard._set_entry("chat1", entry)
    reply = wizard.handle_message("chat1", "user1", "1")
    assert "15 минут" in reply
    assert not wizard.is_active("chat1")


# ---------------------------------------------------------------------------
# "all in order" flow: token-guard (0 keys) folds straight into memobase's
# intro + first missing key question in ONE message.
# ---------------------------------------------------------------------------


def test_choose_all_enables_token_guard_and_asks_first_kb_key(setup_plugin):
    wizard = setup_plugin.wizard
    registry = setup_plugin.registry
    wizard.start_wizard("chat1", "user1")
    reply = wizard.handle_message("chat1", "user1", "1")

    # token-guard needed no keys -- straight into memobase's first missing key
    assert registry.KEY_SPECS["CLOUDFLARE_ACCOUNT_ID"].metaphor in reply
    assert wizard.is_active("chat1")

    cfg = _load_cfg(setup_plugin)
    enabled = cfg.get("plugins", {}).get("enabled", [])
    assert "token-guard" in enabled
    assert "memobase" in enabled  # enabled up front, even before its keys are collected


def test_pick_single_plugin_token_guard_completes_instantly(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")  # pick one
    reply = wizard.handle_message("chat1", "user1", "1")  # token-guard (PLUGIN_ORDER[0])

    assert "Готово" in reply
    assert "перезапустите" in reply.lower()
    assert not wizard.is_active("chat1")

    cfg = _load_cfg(setup_plugin)
    assert cfg.get("plugins", {}).get("enabled") == ["token-guard"]


def test_pick_plugin_by_name(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    reply = wizard.handle_message("chat1", "user1", "token-guard")
    assert "Готово" in reply


# ---------------------------------------------------------------------------
# Wrong-key-type re-ask
# ---------------------------------------------------------------------------


def test_wrong_key_type_reask(setup_plugin, no_network):
    wizard = no_network.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    wizard.handle_message("chat1", "user1", "memobase")
    # first missing key is CLOUDFLARE_ACCOUNT_ID -- send a GEMINI-shaped value instead
    reply = wizard.handle_message("chat1", "user1", "AIza" + "x" * 30)
    assert "GEMINI_API_KEY" in reply  # names what it looks like
    assert "CLOUDFLARE_ACCOUNT_ID" in reply  # re-asks the SAME key
    assert wizard.is_active("chat1")

    entry = wizard._get_entry("chat1")
    assert entry["missing_keys"][entry["key_pos"]] == "CLOUDFLARE_ACCOUNT_ID"

    # a correctly-shaped value now advances past it
    reply2 = wizard.handle_message("chat1", "user1", "a" * 32)
    assert "CLOUDFLARE_ACCOUNT_ID сохранён" in reply2
    entry2 = wizard._get_entry("chat1")
    assert entry2["missing_keys"][entry2["key_pos"]] == "CLOUDFLARE_API_TOKEN"


def test_bad_format_reask_stays_same_key(setup_plugin, no_network):
    wizard = no_network.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    wizard.handle_message("chat1", "user1", "memobase")
    reply = wizard.handle_message("chat1", "user1", "too-short")  # not hex32
    assert "CLOUDFLARE_ACCOUNT_ID" in reply
    assert wizard.is_active("chat1")


# ---------------------------------------------------------------------------
# skip-already-set-key: a key already active in .env is never asked again.
# ---------------------------------------------------------------------------


def test_skip_already_set_key(setup_plugin, no_network):
    wizard = no_network.wizard
    envfile = no_network.envfile
    env_path = wizard._env_path()
    envfile.upsert_env_value(env_path, "CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    envfile.upsert_env_value(env_path, "CLOUDFLARE_API_TOKEN", "cf-token-value")

    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    reply = wizard.handle_message("chat1", "user1", "memobase")

    # the first two keys were already set -- the first QUESTION must be
    # about COHERE_API_KEY, not CLOUDFLARE_ACCOUNT_ID/TOKEN
    registry = no_network.registry
    assert registry.KEY_SPECS["COHERE_API_KEY"].metaphor in reply

    entry = wizard._get_entry("chat1")
    assert entry["missing_keys"] == [
        "COHERE_API_KEY", "GEMINI_API_KEY", "SCRAPECREATORS_API_KEY", "APIFY_TOKEN", "GROQ_API_KEY",
    ]


def test_user_typed_skip_word_skips_current_key(setup_plugin, no_network):
    wizard = no_network.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    wizard.handle_message("chat1", "user1", "memobase")
    reply = wizard.handle_message("chat1", "user1", "пропустить")
    assert "пропускаю CLOUDFLARE_ACCOUNT_ID" in reply
    entry = wizard._get_entry("chat1")
    assert entry["missing_keys"][entry["key_pos"]] == "CLOUDFLARE_API_TOKEN"
    # a skipped key must NOT be written to .env
    envfile = no_network.envfile
    assert envfile.has_active_value(wizard._env_path(), "CLOUDFLARE_ACCOUNT_ID") is False


# ---------------------------------------------------------------------------
# Full happy path: memobase end-to-end (7 keys), verifying masked output
# never leaks the raw value, and the .env file ends up correct.
# ---------------------------------------------------------------------------


_KB_KEY_VALUES = {
    "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
    "CLOUDFLARE_API_TOKEN": "cf-real-token-value",
    "COHERE_API_KEY": "cohere-real-token-value",
    "GEMINI_API_KEY": "AIza" + "z" * 30,
    "SCRAPECREATORS_API_KEY": "sc-real-token-value",
    "APIFY_TOKEN": "apify_real_token_value",
    "GROQ_API_KEY": "gsk_real_token_value",
}


def test_full_happy_path_memobase(setup_plugin, no_network):
    wizard = no_network.wizard
    envfile = no_network.envfile
    registry = no_network.registry

    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    wizard.handle_message("chat1", "user1", "memobase")

    reply = ""
    for key_name in registry.PLUGINS["memobase"].keys:
        value = _KB_KEY_VALUES[key_name]
        reply = wizard.handle_message("chat1", "user1", value)
        # the raw secret must never appear verbatim in the bot's reply
        assert value not in reply

    assert "Готово" in reply
    assert not wizard.is_active("chat1")

    env_path = wizard._env_path()
    status = envfile.scan_keys(env_path, list(_KB_KEY_VALUES))
    assert all(status.values())

    cfg = _load_cfg(setup_plugin)
    assert "memobase" in cfg.get("plugins", {}).get("enabled", [])


# ---------------------------------------------------------------------------
# memohood: memory.provider + memory.memohood config block
# ---------------------------------------------------------------------------


def test_memohood_sets_memory_provider_and_default_config(setup_plugin, no_network):
    wizard = no_network.wizard
    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    wizard.handle_message("chat1", "user1", "memohood")

    for key_name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "COHERE_API_KEY", "GEMINI_API_KEY"):
        value = "a" * 32 if key_name == "CLOUDFLARE_ACCOUNT_ID" else f"value-for-{key_name}"
        if key_name == "GEMINI_API_KEY":
            value = "AIza" + "w" * 30
        wizard.handle_message("chat1", "user1", value)

    cfg = _load_cfg(setup_plugin)
    assert cfg["memory"]["provider"] == "memohood"
    memohood_cfg = cfg["memory"]["memohood"]
    assert memohood_cfg["model"] == {"provider": "gemini", "model": "gemini-2.5-flash-lite"}
    assert memohood_cfg["embedder"] == {"provider": "cloudflare", "model": "@cf/baai/bge-m3", "dims": 1024}
    assert memohood_cfg["rerank"] == {"provider": "cohere", "enabled": True}
    assert memohood_cfg["monthly_ceiling_usd"] == {"cloudflare": 5, "cohere": 5, "gemini": 5}


def test_memohood_config_write_failure_does_not_crash_wizard(setup_plugin, monkeypatch):
    """set_config_value/save_config can sys.exit(1) on a managed-scope key
    (SystemExit is a BaseException) -- the wizard must survive that, not
    propagate it and take down the whole gateway process."""
    wizard = setup_plugin.wizard

    def boom(*a, **kw):
        raise SystemExit(1)

    monkeypatch.setattr("hermes_cli.config.load_config", boom)

    wizard.start_wizard("chat1", "user1")
    wizard.handle_message("chat1", "user1", "2")
    reply = wizard.handle_message("chat1", "user1", "memohood")
    assert "Не удалось включить автоматически" in reply
    assert wizard.is_active("chat1")  # still proceeds to ask for memohood's keys


# ---------------------------------------------------------------------------
# CLI `/setup` command
# ---------------------------------------------------------------------------


def test_cli_setup_no_args_starts_wizard(setup_plugin):
    wizard = setup_plugin.wizard
    reply = wizard.cli_setup_command("")
    assert "1 —" in reply
    assert wizard.is_active(wizard._CLI_CHAT_ID)


def test_cli_setup_full_flow_token_guard(setup_plugin):
    wizard = setup_plugin.wizard
    wizard.cli_setup_command("")
    wizard.cli_setup_command("2")
    reply = wizard.cli_setup_command("1")
    assert "Готово" in reply
    assert not wizard.is_active(wizard._CLI_CHAT_ID)


def test_cli_setup_reshow_current_question_on_empty_args(setup_plugin):
    wizard = setup_plugin.wizard
    first = wizard.cli_setup_command("")
    again = wizard.cli_setup_command("")
    assert first == again


def test_cli_setup_combined_first_call_with_args(setup_plugin):
    wizard = setup_plugin.wizard
    reply = wizard.cli_setup_command("2")
    assert "1 —" in reply  # menu greet
    assert "Какой плагин" in reply  # plus the choose_plugin question, folded in


# ---------------------------------------------------------------------------
# pre_gateway_dispatch hook
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, chat_id, user_id, platform="telegram"):
        self.chat_id = chat_id
        self.user_id = user_id
        self.platform = platform


class _FakeEvent:
    def __init__(self, text, chat_id="123", user_id="u1"):
        self.text = text
        self.source = _FakeSource(chat_id, user_id)


class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeGateway:
    def __init__(self):
        self.adapters = {"telegram": _FakeAdapter()}


def test_gateway_dispatch_triggers_wizard_and_replies(setup_plugin):
    wizard = setup_plugin.wizard
    gateway = _FakeGateway()
    event = _FakeEvent("/setup")

    async def _run():
        result = wizard.on_gateway_dispatch(event=event, gateway=gateway, session_store=None)
        await asyncio.sleep(0)
        return result

    result = asyncio.run(_run())
    assert result == {"action": "skip", "reason": "hermes_setup_wizard"}
    assert gateway.adapters["telegram"].sent
    chat_id, text = gateway.adapters["telegram"].sent[0]
    assert chat_id == "123"
    assert "1 —" in text
    assert wizard.is_active("123")


def test_gateway_dispatch_advances_active_wizard(setup_plugin):
    wizard = setup_plugin.wizard
    gateway = _FakeGateway()

    async def _run():
        wizard.on_gateway_dispatch(event=_FakeEvent("/setup"), gateway=gateway, session_store=None)
        await asyncio.sleep(0)
        wizard.on_gateway_dispatch(event=_FakeEvent("2"), gateway=gateway, session_store=None)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert len(gateway.adapters["telegram"].sent) == 2
    assert "Какой плагин" in gateway.adapters["telegram"].sent[-1][1]


def test_gateway_dispatch_ignores_unrelated_message_when_inactive(setup_plugin):
    wizard = setup_plugin.wizard
    gateway = _FakeGateway()
    result = wizard.on_gateway_dispatch(event=_FakeEvent("hello there"), gateway=gateway, session_store=None)
    assert result is None
    assert gateway.adapters["telegram"].sent == []


def test_gateway_dispatch_no_chat_id_is_noop(setup_plugin):
    wizard = setup_plugin.wizard
    event = _FakeEvent("/setup", chat_id=None)
    result = wizard.on_gateway_dispatch(event=event, gateway=_FakeGateway(), session_store=None)
    assert result is None


def test_gateway_dispatch_never_raises_on_broken_event(setup_plugin):
    wizard = setup_plugin.wizard
    # event with no .source at all
    class _BrokenEvent:
        text = "/setup"

    result = wizard.on_gateway_dispatch(event=_BrokenEvent(), gateway=_FakeGateway(), session_store=None)
    assert result is None


# ---------------------------------------------------------------------------
# register(ctx)
# ---------------------------------------------------------------------------


def test_register_wires_command_and_hook(setup_plugin, fake_ctx):
    setup_plugin.register(fake_ctx)
    assert "setup" in fake_ctx.commands
    assert fake_ctx.commands["setup"]["handler"] is setup_plugin.wizard.cli_setup_command
    assert "pre_gateway_dispatch" in fake_ctx.hooks
    assert setup_plugin.wizard.on_gateway_dispatch in fake_ctx.hooks["pre_gateway_dispatch"]
