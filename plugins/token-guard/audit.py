"""token-guard audit — pure config/ledger checks, no side effects.

``run_audit()`` never mutates config and never auto-applies anything: it
only returns a list of Finding dicts for report.py to render. Every check
degrades gracefully (severity="info" + a "проверьте вручную" note) when
the data it wants (ledger history, host model-metadata lookups) isn't
available yet — this plugin must never crash the ``/tokenguard audit``
command just because a host-internal API changed shape.

Finding = {"severity": "info"|"warning", "title_ru": str, "detail_ru": str,
           "fix_hint_ru": str}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import ledger, toggles

logger = logging.getLogger(__name__)

Finding = Dict[str, str]

MIN_HISTORY_DAYS_FOR_TOOLSET_CHECK = 14


def _finding(severity: str, title_ru: str, detail_ru: str, fix_hint_ru: str = "") -> Finding:
    return {
        "severity": severity,
        "title_ru": title_ru,
        "detail_ru": detail_ru,
        "fix_hint_ru": fix_hint_ru,
    }


def _load_cfg() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:
        logger.debug("token-guard: audit could not load config", exc_info=True)
        return {}


def _cfg_get(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    try:
        from hermes_cli.config import cfg_get
        return cfg_get(cfg, *keys, default=default)
    except Exception:
        node = cfg
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def _check_cache_ttl(cfg: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    ttl = _cfg_get(cfg, "prompt_caching", "cache_ttl", default="5m")
    # Heuristic for "gateway sessions exist": no platform column is kept in
    # the ledger (see DESIGN.md schema), so we approximate "long-running /
    # multi-session usage" by more than one distinct session recorded.
    try:
        rows = ledger.requests_in_window(90)
        distinct_sessions = {r.get("session_id") for r in rows if r.get("session_id")}
    except Exception:
        distinct_sessions = set()

    if (not ttl or ttl == "5m") and len(distinct_sessions) > 1:
        findings.append(_finding(
            "warning",
            "Кэш промптов живёт всего 5 минут",
            "prompt_caching.cache_ttl не задан или равен «5m», а сессий несколько — "
            "между сообщениями кэш успевает протухнуть, и каждый раз он пишется заново "
            "по полной цене.",
            "Включите тумблер cache_1h: /tokenguard enable cache_1h",
        ))
    return findings


def _check_compression_model(cfg: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    model = _cfg_get(cfg, "auxiliary", "compression", "model", default="")
    if not model:
        findings.append(_finding(
            "info",
            "Сжатие контекста идёт основной моделью",
            "auxiliary.compression.model не задан — суммаризация большого контекста "
            "выполняется той же (обычно дорогой) моделью, что и основной диалог.",
            "Задайте дешёвую модель и включите cheap_aux: "
            "/tokenguard set-cheap-model <provider> <model>, затем /tokenguard enable cheap_aux",
        ))
        return findings

    main_model = _cfg_get(cfg, "model", "default", default="") or _cfg_get(cfg, "model", "name", default="")
    if not main_model:
        return findings

    try:
        from agent.model_metadata import get_model_context_length
        comp_ctx = get_model_context_length(model)
        main_ctx = get_model_context_length(main_model)
        if comp_ctx and main_ctx and comp_ctx < main_ctx:
            findings.append(_finding(
                "warning",
                "Окно модели сжатия меньше основной модели",
                f"Модель сжатия «{model}» (~{comp_ctx} токенов) уже основной модели "
                f"«{main_model}» (~{main_ctx} токенов) — при сжатии середина диалога "
                "может молча выпасть.",
                "Выберите для auxiliary.compression модель с окном не меньше основной.",
            ))
    except Exception:
        logger.debug("token-guard: context-window lookup unavailable", exc_info=True)
        findings.append(_finding(
            "info",
            "Не удалось сравнить окна моделей автоматически",
            f"Не получилось проверить размер контекстного окна для «{model}» "
            f"относительно основной модели.",
            "Проверьте вручную: окно auxiliary.compression должно быть не меньше основной модели.",
        ))
    return findings


def _check_delegation_model(cfg: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    model = _cfg_get(cfg, "delegation", "model", default="")
    if not model:
        findings.append(_finding(
            "info",
            "Делегирование идёт на основной модели",
            "delegation.model не задан — сабагенты (поиск, сбор информации) используют "
            "ту же модель и провайдера, что и основной диалог.",
            "Задайте дешёвую модель и включите cheap_delegation: "
            "/tokenguard set-cheap-model <provider> <model>, затем /tokenguard enable cheap_delegation",
        ))
    return findings


def _check_unused_toolsets(cfg: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    history_days = ledger.history_span_days()
    if history_days < MIN_HISTORY_DAYS_FOR_TOOLSET_CHECK:
        return findings

    try:
        import model_tools
        toolsets_info = model_tools.get_available_toolsets()
    except Exception:
        logger.debug("token-guard: toolset inventory unavailable", exc_info=True)
        return findings

    try:
        tool_calls = ledger.tool_calls_in_window(history_days)
        used_toolsets = set()
        for call in tool_calls:
            tool_name = call.get("tool_name") or ""
            try:
                ts = model_tools.get_toolset_for_tool(tool_name)
            except Exception:
                ts = None
            if ts:
                used_toolsets.add(ts)
    except Exception:
        logger.debug("token-guard: tool_calls -> toolset mapping failed", exc_info=True)
        return findings

    for name, info in toolsets_info.items():
        if not isinstance(info, dict) or not info.get("available"):
            continue
        if name not in used_toolsets:
            findings.append(_finding(
                "info",
                f"Набор инструментов «{name}» включён, но не используется",
                f"За последние {int(history_days)} дн. ни один инструмент из «{name}» "
                "не вызывался ни разу.",
                "Кандидат на отключение — уменьшит размер схем в каждом запросе. "
                "token-guard это не делает автоматически.",
            ))
    return findings


def _check_backup_consistency() -> List[Finding]:
    findings: List[Finding] = []
    try:
        backups = toggles.load_backup_for_audit()
    except Exception:
        logger.debug("token-guard: backup consistency check failed", exc_info=True)
        return findings

    for name in backups:
        if name not in toggles.TOGGLE_KEYS:
            findings.append(_finding(
                "info",
                "Есть резервная копия для неизвестного переключателя",
                f"В config_backup.json найдена запись «{name}», которой нет в текущем "
                "реестре переключателей token-guard.",
                "Проверьте вручную содержимое config_backup.json.",
            ))
    return findings


def run_audit() -> List[Finding]:
    """Run every config/ledger audit rule. Never raises."""
    findings: List[Finding] = []
    cfg = _load_cfg()
    for check in (
        _check_cache_ttl,
        _check_compression_model,
        _check_delegation_model,
        _check_unused_toolsets,
    ):
        try:
            findings.extend(check(cfg))
        except Exception:
            logger.debug("token-guard: audit check %s failed", check.__name__, exc_info=True)
    try:
        findings.extend(_check_backup_consistency())
    except Exception:
        logger.debug("token-guard: backup consistency check failed", exc_info=True)
    return findings
