"""token-guard toggles — Part 2: optional, off-by-default config mutations.

Every toggle is a small set of dotted config.yaml keys. Enabling backs up
the pre-existing value of each key (or records that it was absent) to
``config_backup.json`` *before* writing anything; disabling replays that
backup, restoring a value with ``set_config_value`` or deleting a key that
didn't exist before (host has no key-removal primitive, so that path
manipulates the raw YAML dict directly and rewrites it — see
``_delete_nested_raw``/``_write_raw``).

Host-internal imports (``hermes_cli.config``, ``utils.atomic_yaml_write``)
are all deferred into function bodies per the plugin API contract.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_STATE_SUBDIR = "token-guard"
_BACKUP_FILENAME = "config_backup.json"

# toggle -> ordered list of dotted config.yaml keys it writes.
TOGGLE_KEYS: Dict[str, List[str]] = {
    "cheap_aux": [
        "auxiliary.compression.model",
        "auxiliary.compression.provider",
        "auxiliary.title_generation.model",
        "auxiliary.title_generation.provider",
    ],
    "cheap_delegation": [
        "delegation.model",
        "delegation.provider",
    ],
    "cache_1h": [
        "prompt_caching.cache_ttl",
    ],
}

# Recognised but not implemented in v1 — reply with a fixed message.
RESERVED_TOGGLES = {"cron_cascade", "context_editing"}

RISK_CARDS: Dict[str, str] = {
    "cheap_aux": (
        "cheap_aux — дешёвая модель для служебных задач (сжатие контекста, заголовки).\n"
        "Экономия: суммаризация/заголовки/поиск по сессиям — самые частые служебные вызовы.\n"
        "Риск: слабое резюме при сжатии теряет детали навсегда; модель с коротким окном "
        "молча выкидывает середину диалога.\n"
        "Страховка: ставьте длинноконтекстную «флеш»-модель; /tokenguard audit проверяет "
        "размер окна; откат одной командой — /tokenguard disable cheap_aux."
    ),
    "cheap_delegation": (
        "cheap_delegation — дешёвая модель для сабагентов (делегирование).\n"
        "Экономия: сабагенты (поиск, сбор информации) на дешёвой модели — до −50% "
        "на делегированиях.\n"
        "Риск: глубокие рассуждения в сабагенте могут просесть по качеству.\n"
        "Страховка: тяжёлые задачи не делегируйте или отключите тумблер; откат одной "
        "командой — /tokenguard disable cheap_delegation."
    ),
    "cache_1h": (
        "cache_1h — кэш промптов на 1 час вместо 5 минут.\n"
        "Экономия: чтение кэша стоит ~10% обычной цены; час выгоден, если между "
        "сообщениями бывают паузы.\n"
        "Риск: запись в кэш на 1 час дороже (2× вместо 1.25×) — при очень редких "
        "сообщениях экономия может выйти в ноль.\n"
        "Страховка: /cost покажет hit-rate; откат одной командой — /tokenguard disable cache_1h."
    ),
}


# ---------------------------------------------------------------------------
# Backup file
# ---------------------------------------------------------------------------

def _backup_path() -> Path:
    return get_hermes_home() / _STATE_SUBDIR / _BACKUP_FILENAME


def _load_backup() -> Dict[str, Dict[str, Any]]:
    path = _backup_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("token-guard: could not read config_backup.json", exc_info=True)
        return {}


def _save_backup(data: Dict[str, Dict[str, Any]]) -> None:
    path = _backup_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("token-guard: could not write config_backup.json", exc_info=True)


def load_backup_for_audit() -> Dict[str, Dict[str, Any]]:
    """Read-only accessor for audit.py's consistency check."""
    return _load_backup()


def is_enabled(toggle: str) -> bool:
    return toggle in _load_backup()


def status() -> Dict[str, Any]:
    """Return {toggle: enabled_bool} for every known + reserved toggle."""
    backups = _load_backup()
    result: Dict[str, Any] = {name: (name in backups) for name in TOGGLE_KEYS}
    for name in RESERVED_TOGGLES:
        result[name] = "reserved"
    return result


# ---------------------------------------------------------------------------
# Raw config helpers (only used for the "key was absent before" restore path
# — set_config_value has no delete primitive, see DESIGN.md Part 2 mechanics)
# ---------------------------------------------------------------------------

def _get_nested(cfg: Dict[str, Any], dotted_key: str) -> Tuple[bool, Any]:
    node: Any = cfg
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _delete_nested_raw(node: Dict[str, Any], dotted_key: str) -> None:
    """Delete a dotted key from a raw config dict, pruning now-empty parents."""
    parts = dotted_key.split(".")

    def _recurse(current: Dict[str, Any], remaining: List[str]) -> None:
        if not remaining or not isinstance(current, dict):
            return
        key = remaining[0]
        if len(remaining) == 1:
            current.pop(key, None)
            return
        child = current.get(key)
        if isinstance(child, dict):
            _recurse(child, remaining[1:])
            if not child:
                current.pop(key, None)

    _recurse(node, parts)


def _read_raw_config_safe() -> Dict[str, Any]:
    try:
        from hermes_cli.config import read_raw_config
        return read_raw_config() or {}
    except Exception:
        logger.debug("token-guard: read_raw_config failed", exc_info=True)
        return {}


def _write_raw(cfg: Dict[str, Any]) -> None:
    from hermes_cli.config import get_config_path, ensure_hermes_home
    from utils import atomic_yaml_write

    ensure_hermes_home()
    atomic_yaml_write(get_config_path(), cfg, sort_keys=False)


# ---------------------------------------------------------------------------
# Cheap-model self-section
# ---------------------------------------------------------------------------

def get_cheap_model() -> Tuple[str, str]:
    """Return (model, provider) from token_guard.cheap_model/cheap_provider."""
    try:
        from hermes_cli.config import load_config_readonly, cfg_get
        cfg = load_config_readonly()
        model = cfg_get(cfg, "token_guard", "cheap_model", default="") or ""
        provider = cfg_get(cfg, "token_guard", "cheap_provider", default="") or ""
        return model, provider
    except Exception:
        logger.debug("token-guard: get_cheap_model failed", exc_info=True)
        return "", ""


def set_cheap_model(provider: str, model: str) -> str:
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider or not model:
        return "Использование: /tokenguard set-cheap-model <provider> <model>"
    try:
        from hermes_cli.config import set_config_value
        set_config_value("token_guard.cheap_provider", provider)
        set_config_value("token_guard.cheap_model", model)
    except (Exception, SystemExit) as exc:
        # See the comment in _apply_enable: set_config_value can sys.exit(1)
        # on a managed-scope key; SystemExit must never escape a handler.
        logger.debug("token-guard: set_cheap_model failed", exc_info=True)
        return f"Не удалось сохранить дешёвую модель: {exc}"
    return f"Дешёвая модель сохранена: {provider}/{model}. Теперь можно включать cheap_aux / cheap_delegation."


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def _backup_keys_if_needed(toggle: str, keys: List[str]) -> None:
    backups = _load_backup()
    if toggle in backups:
        return  # already enabled (or a stale backup) — never clobber the original snapshot
    raw = _read_raw_config_safe()
    snapshot: Dict[str, Any] = {}
    for key in keys:
        exists, value = _get_nested(raw, key)
        snapshot[key] = value if exists else None
    backups[toggle] = snapshot
    _save_backup(backups)


def _apply_enable(toggle: str) -> str:
    keys = TOGGLE_KEYS[toggle]
    cheap_model, cheap_provider = get_cheap_model()
    if toggle in ("cheap_aux", "cheap_delegation") and (not cheap_model or not cheap_provider):
        return (
            "Сначала укажите дешёвую модель: "
            "/tokenguard set-cheap-model <provider> <model>"
        )

    _backup_keys_if_needed(toggle, keys)

    try:
        from hermes_cli.config import set_config_value
        if toggle == "cheap_aux":
            set_config_value("auxiliary.compression.model", cheap_model)
            set_config_value("auxiliary.compression.provider", cheap_provider)
            set_config_value("auxiliary.title_generation.model", cheap_model)
            set_config_value("auxiliary.title_generation.provider", cheap_provider)
            return (
                f"cheap_aux включён: сжатие контекста и генерация заголовков теперь "
                f"используют {cheap_provider}/{cheap_model}. Откат: /tokenguard disable cheap_aux"
            )
        if toggle == "cheap_delegation":
            set_config_value("delegation.model", cheap_model)
            set_config_value("delegation.provider", cheap_provider)
            return (
                f"cheap_delegation включён: делегирование теперь использует "
                f"{cheap_provider}/{cheap_model}. Откат: /tokenguard disable cheap_delegation"
            )
        if toggle == "cache_1h":
            set_config_value("prompt_caching.cache_ttl", "1h")
            return "cache_1h включён: кэш промптов теперь живёт 1 час. Откат: /tokenguard disable cache_1h"
    except (Exception, SystemExit) as exc:
        # SystemExit included on purpose: hermes_cli.config.set_config_value
        # calls sys.exit(1) for a config key locked by the managed scope
        # (NixOS/managed installs). SystemExit is a BaseException, not an
        # Exception, so it would otherwise blow straight through this
        # handler *and* the host CLI's own `except Exception` around plugin
        # command dispatch (cli.py) and kill the whole process/session —
        # exactly what a plugin must never do.
        logger.debug("token-guard: enable %s failed", toggle, exc_info=True)
        return f"Не удалось применить переключатель «{toggle}»: {exc}"

    return f"Неизвестный переключатель: {toggle}"


def enable_flow(toggle: str, confirm: bool) -> str:
    """Full /tokenguard enable flow: risk card gate, then apply."""
    toggle = (toggle or "").strip()
    if not toggle:
        return "Использование: /tokenguard enable <toggle> [confirm]"
    if toggle in RESERVED_TOGGLES:
        return f"«{toggle}» зарезервировано, появится в следующей версии."
    if toggle not in TOGGLE_KEYS:
        return f"Неизвестный переключатель: {toggle}. Доступны: {', '.join(sorted(TOGGLE_KEYS))}"
    if is_enabled(toggle):
        return f"«{toggle}» уже включён. Откат: /tokenguard disable {toggle}"
    if not confirm:
        card = RISK_CARDS.get(toggle, "")
        return f"{card}\n\nПовторите: /tokenguard enable {toggle} confirm"
    return _apply_enable(toggle)


def disable(toggle: str) -> str:
    toggle = (toggle or "").strip()
    if not toggle:
        return "Использование: /tokenguard disable <toggle>"
    if toggle in RESERVED_TOGGLES:
        return f"«{toggle}» зарезервировано, появится в следующей версии."
    if toggle not in TOGGLE_KEYS:
        return f"Неизвестный переключатель: {toggle}."

    backups = _load_backup()
    snapshot = backups.get(toggle)
    if snapshot is None:
        return f"«{toggle}» не включён — нечего откатывать."

    try:
        from hermes_cli.config import set_config_value
        for key, old_value in snapshot.items():
            if old_value is None:
                raw = _read_raw_config_safe()
                _delete_nested_raw(raw, key)
                _write_raw(raw)
            else:
                set_config_value(key, str(old_value))
        backups.pop(toggle, None)
        _save_backup(backups)
    except (Exception, SystemExit) as exc:
        # See the matching comment in _apply_enable: set_config_value can
        # sys.exit(1) on a managed-scope key, and SystemExit must never
        # escape a plugin command handler.
        logger.debug("token-guard: disable %s failed", toggle, exc_info=True)
        return f"Не удалось откатить переключатель «{toggle}»: {exc}"

    return f"«{toggle}» отключён, прежние настройки восстановлены."
