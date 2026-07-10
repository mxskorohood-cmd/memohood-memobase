"""hermes-setup onboarding wizard: `/setup` (CLI) and the Telegram/gateway
flow driven through `pre_gateway_dispatch`.

Both entry points share ONE state machine (`start_wizard` / `handle_message`
below), keyed by an opaque ``chat_id`` string. The gateway path keys it off
the real Telegram (or other platform) chat id; the CLI path (`cli_setup_
command`) keys it off a fixed pseudo id (`_CLI_CHAT_ID`) since
``register_command`` handlers get no chat/user identity at all
(API_CONTRACT_PLUGINS.md §2) — there is only ever one local operator per CLI
process, so a fixed key never collides with a real (numeric) gateway chat_id.

Design notes:
  * State is IN-MEMORY only (a plain module-level dict), not persisted to
    disk — a hermes restart mid-wizard just loses progress; the user reruns
    `/setup`. Contrast with MemoBase's own `/memobase setup` wizard, which
    persists to a JSON file for exactly the opposite reason (that one is
    meant to survive a restart mid-ingest).
  * STRICTLY one question per bot message: every reply this module returns
    ends in exactly one question (or is a pure narrative — the greeting,
    the cancel/timeout/done notices). Enabling a plugin with zero required
    keys (token-guard) never stalls on an empty "question" — it silently
    folds its "enabled" narrative into the very next question instead (see
    `_enter_plugin`'s recursion).
  * A ~15-minute inactivity timeout and a cancel word reset the wizard.
  * `pre_gateway_dispatch` hooks run SYNCHRONOUSLY (never `async def`); a
    reply is sent back via `asyncio.create_task(adapter.send(...))`, mirroring
    MemoBase's own `wizard.py` `_send_async` verbatim (API_CONTRACT_PLUGINS.md
    §2's documented pattern) — do not "fix" this into an `await`, it would be
    silently dropped by the host's plain synchronous hook invocation.
  * Config writes (`plugins.enabled` / `memory.provider` + `memory.memohood.*`)
    go through the real `hermes_cli.config.load_config`/`save_config` — never
    hand-rolled YAML — and are wrapped in `except (Exception, SystemExit)`
    because `save_config`/`set_config_value` can call `sys.exit(1)` for a
    managed-scope-locked key (`SystemExit` is a `BaseException`, not an
    `Exception` — see token-guard/toggles.py's identical guard for why this
    matters: an uncaught `SystemExit` here would kill the whole hermes
    process, not just this one plugin command).
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import envfile
from . import registry

logger = logging.getLogger("hermes_setup.wizard")

_WIZARD_TIMEOUT_SECONDS = 15 * 60
_CANCEL_WORDS = {"отмена", "стоп", "cancel", "stop", "выход", "exit"}
_SKIP_WORD = "пропустить"

_CLI_CHAT_ID = "cli"
_CLI_USER_ID = "cli"

# ---------------------------------------------------------------------------
# memohood's default `memory.memohood.*` config block — verbatim from
# D:/hermes-fable/plugins/memory-eve/DESIGN_v1.md's "Config (config.yaml
# memory.*)" sample. Duplicated here (rather than imported) because
# hermes-setup must not depend on another plugin's package — plugins are
# independent, separately-loaded units (API_CONTRACT_PLUGINS.md §1).
# ---------------------------------------------------------------------------
MEMOHOOD_DEFAULT_CONFIG: Dict[str, Any] = {
    "gate": {"backend": "pass"},
    "model": {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
    "embedder": {"provider": "cloudflare", "model": "@cf/baai/bge-m3", "dims": 1024},
    "rerank": {"provider": "cohere", "enabled": True},
    "auto_capture": True,
    "capture_threshold": 4.0,
    "monthly_ceiling_usd": {"cloudflare": 5, "cohere": 5, "gemini": 5},
}


# ---------------------------------------------------------------------------
# In-memory per-chat state
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_STATE: Dict[str, Dict[str, Any]] = {}


def _get_entry(chat_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _STATE.get(str(chat_id))


def _set_entry(chat_id: str, entry: Dict[str, Any]) -> None:
    with _LOCK:
        _STATE[str(chat_id)] = entry


def clear_wizard(chat_id: str) -> None:
    with _LOCK:
        _STATE.pop(str(chat_id), None)


def is_active(chat_id: str) -> bool:
    return _get_entry(chat_id) is not None


def _persist(chat_id: str, entry: Dict[str, Any], message: str) -> str:
    """Store *entry* (updating bookkeeping fields) and return *message* —
    the one place every step function funnels through, so `last_message`/
    `last_activity` are never forgotten in a new branch."""
    entry["last_message"] = message
    entry["last_activity"] = time.time()
    _set_entry(chat_id, entry)
    return message


def _is_cancel(text: str) -> bool:
    return (text or "").strip().strip(".,!").lower() in _CANCEL_WORDS


def _is_expired(entry: Dict[str, Any]) -> bool:
    last = entry.get("last_activity", 0.0)
    return (time.time() - last) > _WIZARD_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# hermes_home / .env paths
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _env_path() -> Path:
    return _hermes_home() / ".env"


# ---------------------------------------------------------------------------
# Config writers — real hermes_cli.config API only, never hand-rolled YAML.
# ---------------------------------------------------------------------------


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict: *base* with *override* merged in, recursively.
    Only dict values recurse; anything else in *override* replaces the base
    value outright. Never mutates either input."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _enable_standalone_plugin(slug: str) -> None:
    """Add *slug* to `plugins.enabled` in config.yaml (mirrors hermes_cli/
    plugins_cmd.py's own `_get_enabled_set`/`_save_enabled_set` pair — that
    module's helpers are private/CLI-console-coupled (rich.Console prompts),
    so this is a small, chat-safe reimplementation of the same list-append
    semantics rather than a reuse of those private functions)."""
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    plugins_cfg = cfg.get("plugins")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    enabled = plugins_cfg.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
    if slug not in enabled:
        enabled = enabled + [slug]
    plugins_cfg["enabled"] = enabled
    cfg["plugins"] = plugins_cfg
    save_config(cfg)


def _enable_memohood() -> None:
    """Set `memory.provider: memohood` and deep-merge `MEMOHOOD_DEFAULT_CONFIG`
    under `memory.memohood` — existing user values win over the defaults."""
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    memory_cfg = cfg.get("memory")
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}
    existing_memohood = memory_cfg.get("memohood")
    if not isinstance(existing_memohood, dict):
        existing_memohood = {}
    memory_cfg["memohood"] = _deep_merge(MEMOHOOD_DEFAULT_CONFIG, existing_memohood)
    memory_cfg["provider"] = "memohood"
    cfg["memory"] = memory_cfg
    save_config(cfg)


def _enable_plugin_config(slug: str) -> Tuple[bool, str]:
    """Enable *slug* in config.yaml. Returns ``(True, "")`` on success or
    ``(False, <reason>)`` — never raises and never lets a managed-scope
    `SystemExit` escape (see module docstring)."""
    spec = registry.PLUGINS[slug]
    try:
        if spec.is_memory_provider:
            _enable_memohood()
        else:
            _enable_standalone_plugin(slug)
        return True, ""
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - never let a config write kill the wizard
        logger.warning("hermes-setup wizard: failed to enable %s", slug, exc_info=True)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Chat copy
# ---------------------------------------------------------------------------

_MENU_TEXT = (
    "Привет! Я мастер настройки Hermes. Помогу включить и настроить "
    "остальные плагины: token-guard (счётчик расходов), MemoBase (личная "
    "библиотека) и MemoHood (память). Всё — по шагам, один вопрос за раз.\n\n"
    "Как настроим?\n"
    "1 — всё по порядку\n"
    "2 — выбрать один плагин\n\n"
    "Остановиться можно в любой момент словом «отмена»."
)

_DONE_MESSAGE = (
    "Готово! Всё, что вы выбрали, настроено.\n\n"
    "Чтобы изменения заработали, перезапустите Hermes: остановите процесс и "
    "запустите его заново."
)


def _choose_plugin_text() -> str:
    lines = ["Какой плагин настроить?"]
    for i, slug in enumerate(registry.PLUGIN_ORDER, start=1):
        lines.append(f"{i} — {registry.PLUGINS[slug].title}")
    return "\n".join(lines)


def _resolve_plugin_choice(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    for i, slug in enumerate(registry.PLUGIN_ORDER, start=1):
        if t == str(i):
            return slug
    for slug in registry.PLUGIN_ORDER:
        spec = registry.PLUGINS[slug]
        if t == slug.lower() or t == spec.title.lower():
            return slug
    return None


def _intro_text(slug: str) -> str:
    spec = registry.PLUGINS[slug]
    dirs = registry.discover_plugin_dirs(_hermes_home())
    if dirs.get(slug, False):
        return spec.description
    return (
        f"{spec.description}\n\n"
        "(Похоже, папка этого плагина ещё не скопирована в ~/.hermes/plugins — "
        "настройки всё равно сохранятся, доустановите папку файлами и "
        "перезапустите Hermes.)"
    )


def _question_for_key(key_name: str) -> str:
    spec = registry.KEY_SPECS.get(key_name)
    metaphor = spec.metaphor if spec else key_name
    hint = spec.format_hint if spec else "непустая строка"
    return (
        f"{metaphor}\n\n"
        f"Пришлите значение {key_name} отдельным сообщением (формат: {hint}).\n"
        "Если этого ключа сейчас нет — напишите «пропустить»."
    )


def _key_saved_note(key_name: str, value: str) -> str:
    masked = envfile.mask_key(value)
    ok, msg = registry.live_check_key(key_name, value)
    prefix = "✓ " if ok else ""
    return (
        f"Ключ {key_name} сохранён: {masked}. {prefix}{msg}\n\n"
        "Теперь удалите то сообщение с ключом из истории чата — так безопаснее."
    )


# ---------------------------------------------------------------------------
# Step machine
# ---------------------------------------------------------------------------


def _missing_keys_for(slug: str) -> List[str]:
    spec = registry.PLUGINS[slug]
    if not spec.keys:
        return []
    status = envfile.scan_keys(_env_path(), spec.keys)
    return [k for k in spec.keys if not status.get(k, False)]


def _enter_plugin(chat_id: str, entry: Dict[str, Any]) -> str:
    """Enable config for the CURRENT queue plugin and return either the
    question for its first missing key, or (recursively) fold straight
    into the next plugin / the done message when nothing is missing."""
    idx = entry.get("queue_idx", 0)
    queue = entry.get("queue", [])
    if idx >= len(queue):
        clear_wizard(chat_id)
        return _DONE_MESSAGE

    slug = queue[idx]
    ok, err = _enable_plugin_config(slug)
    intro = _intro_text(slug)
    if not ok:
        intro += (
            f"\n\nНе удалось включить автоматически ({err}). Ключи всё равно "
            "сохранятся — включите плагин вручную позже."
        )

    missing = _missing_keys_for(slug)
    entry["missing_keys"] = missing
    entry["key_pos"] = 0

    if missing:
        entry["step"] = "await_key"
        question = _question_for_key(missing[0])
        return _persist(chat_id, entry, f"{intro}\n\n{question}")

    entry["queue_idx"] = idx + 1
    nxt = _enter_plugin(chat_id, entry)
    return f"{intro}\n\n{nxt}"


def _advance_after_key(chat_id: str, entry: Dict[str, Any], note: str) -> str:
    entry["key_pos"] = entry.get("key_pos", 0) + 1
    missing = entry.get("missing_keys", [])
    if entry["key_pos"] < len(missing):
        key_name = missing[entry["key_pos"]]
        return _persist(chat_id, entry, f"{note}\n\n{_question_for_key(key_name)}")
    entry["queue_idx"] = entry.get("queue_idx", 0) + 1
    nxt = _enter_plugin(chat_id, entry)
    return f"{note}\n\n{nxt}"


def _advance_key(chat_id: str, entry: Dict[str, Any], value: str) -> str:
    missing = entry.get("missing_keys", [])
    pos = entry.get("key_pos", 0)
    if pos >= len(missing):
        # Defensive: state somehow desynced (e.g. resumed after an external
        # edit) — cleanly re-enter the current plugin's key loop.
        return _enter_plugin(chat_id, entry)
    key_name = missing[pos]

    text = (value or "").strip()
    if text.lower() == _SKIP_WORD:
        return _advance_after_key(chat_id, entry, f"Хорошо, пропускаю {key_name}.")

    ok, msg = registry.validate_key(key_name, text)
    if not ok:
        return _persist(chat_id, entry, f"{msg}\n\n{_question_for_key(key_name)}")

    try:
        envfile.upsert_env_value(_env_path(), key_name, text)
    except OSError as exc:
        return _persist(
            chat_id, entry,
            f"Не удалось сохранить ключ: {exc}. Попробуйте прислать его ещё раз.\n\n"
            f"{_question_for_key(key_name)}",
        )

    note = _key_saved_note(key_name, text)
    return _advance_after_key(chat_id, entry, note)


def start_wizard(chat_id: str, user_id: str) -> str:
    entry: Dict[str, Any] = {
        "user_id": user_id,
        "step": "menu",
        "queue": [],
        "queue_idx": 0,
        "missing_keys": [],
        "key_pos": 0,
    }
    return _persist(chat_id, entry, _MENU_TEXT)


def handle_message(chat_id: str, user_id: str, text: str) -> Optional[str]:
    """Advance the wizard for *chat_id* by one step given the raw incoming
    *text*. Returns ``None`` only when there is no active wizard for this
    chat, or a different identity tries to answer someone else's wizard —
    never while it is genuinely active (an unparseable answer just
    re-asks the same question)."""
    entry = _get_entry(chat_id)
    if entry is None:
        return None
    if str(entry.get("user_id")) != str(user_id):
        return None  # a different identity in the same chat -- don't hijack

    if _is_cancel(text):
        clear_wizard(chat_id)
        return "Настройка отменена. Когда захотите продолжить — наберите /setup."
    if _is_expired(entry):
        clear_wizard(chat_id)
        return (
            "Прошло больше 15 минут без ответа — я сбросил мастер настройки. "
            "Наберите /setup, чтобы начать заново."
        )

    step = entry.get("step")
    stripped = (text or "").strip()

    if step == "menu":
        choice = stripped[:1]
        if choice == "1":
            entry["queue"] = list(registry.PLUGIN_ORDER)
            entry["queue_idx"] = 0
            return _enter_plugin(chat_id, entry)
        if choice == "2":
            entry["step"] = "choose_plugin"
            return _persist(chat_id, entry, _choose_plugin_text())
        return _persist(chat_id, entry, "Не понял ответ — пришлите 1 или 2.\n\n" + _MENU_TEXT)

    if step == "choose_plugin":
        slug = _resolve_plugin_choice(stripped)
        if slug is None:
            return _persist(
                chat_id, entry,
                "Не понял ответ — пришлите номер или название плагина.\n\n" + _choose_plugin_text(),
            )
        entry["queue"] = [slug]
        entry["queue_idx"] = 0
        return _enter_plugin(chat_id, entry)

    if step == "await_key":
        return _advance_key(chat_id, entry, text)

    return None


# ---------------------------------------------------------------------------
# `/setup` CLI slash command
# ---------------------------------------------------------------------------


def _reshow_current_question(chat_id: str) -> str:
    entry = _get_entry(chat_id)
    if entry is None:
        return start_wizard(chat_id, _CLI_USER_ID)
    return entry.get("last_message", _MENU_TEXT)


def cli_setup_command(raw_args: str) -> str:
    """``/setup`` slash command — the CLI entry point (no gateway chat
    identity exists here at all, API_CONTRACT_PLUGINS.md §2). Drives the
    exact same state machine as the Telegram path under a fixed pseudo
    chat id: ``/setup`` with no args starts (or re-shows) the wizard;
    ``/setup <answer>`` supplies the next answer, e.g. ``/setup 1``,
    ``/setup AIza...``, ``/setup отмена``.
    """
    args = (raw_args or "").strip()
    if not is_active(_CLI_CHAT_ID):
        greet = start_wizard(_CLI_CHAT_ID, _CLI_USER_ID)
        if not args:
            return greet
        reply = handle_message(_CLI_CHAT_ID, _CLI_USER_ID, args)
        return f"{greet}\n\n{reply}" if reply else greet
    if not args:
        return _reshow_current_question(_CLI_CHAT_ID)
    reply = handle_message(_CLI_CHAT_ID, _CLI_USER_ID, args)
    return reply if reply is not None else "Настройка уже завершена. Наберите /setup, чтобы начать заново."


# ---------------------------------------------------------------------------
# `pre_gateway_dispatch` entry point (Telegram / other gateway platforms)
# ---------------------------------------------------------------------------

_SETUP_TRIGGER_RE = re.compile(r"^\s*/setup\b", re.IGNORECASE)


def _send_async(gateway: Any, platform: Any, chat_id: str, text: str) -> None:
    """Fire-and-forget reply — copied verbatim from MemoBase's (hermes-kb/)
    wizard.py's own helper (API_CONTRACT_PLUGINS.md §2's documented pattern
    for replying on a ``skip`` from a PLAIN, synchronous hook callback)."""
    try:
        import asyncio

        adapter = gateway.adapters[platform]
        asyncio.create_task(adapter.send(chat_id, text))
    except Exception:
        logger.warning("hermes-setup wizard: failed to send reply via gateway adapter", exc_info=True)


def on_gateway_dispatch(
    event: Any = None, gateway: Any = None, session_store: Any = None, **_kw: Any
) -> Optional[Dict[str, Any]]:
    """``pre_gateway_dispatch`` hook: intercepts ``/setup`` and any
    follow-up message while a wizard is active for this chat, BEFORE the
    LLM ever sees it. Returns ``{"action": "skip", ...}`` when handled,
    ``None`` otherwise (let the message flow through normally) — never
    raises."""
    if event is None:
        return None
    try:
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", None)
        user_id = getattr(source, "user_id", None)
        platform = getattr(source, "platform", None)
        text = getattr(event, "text", "") or ""
        if not chat_id:
            return None

        if _SETUP_TRIGGER_RE.match(text):
            reply = start_wizard(chat_id, user_id)
            _send_async(gateway, platform, chat_id, reply)
            return {"action": "skip", "reason": "hermes_setup_wizard"}

        if is_active(chat_id):
            reply = handle_message(chat_id, user_id, text)
            if reply is not None:
                _send_async(gateway, platform, chat_id, reply)
                return {"action": "skip", "reason": "hermes_setup_wizard_step"}
        return None
    except Exception:  # noqa: BLE001 - hook contract: never raise, never block dispatch
        logger.debug("hermes-setup wizard: pre_gateway_dispatch handling failed (non-fatal)", exc_info=True)
        return None


def register(ctx: Any) -> None:
    ctx.register_command(
        "setup",
        cli_setup_command,
        description="Мастер настройки плагинов Hermes (token-guard, MemoBase, MemoHood)",
        args_hint="[ответ]",
    )
    ctx.register_hook("pre_gateway_dispatch", on_gateway_dispatch)
