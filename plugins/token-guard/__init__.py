"""token-guard plugin — register(ctx) only. Thin glue, no logic.

Part 1 (always on when the plugin is enabled): observation-only ledger,
cache-bust detection and config audit — never changes model behaviour.
Part 2 (opt-in): cheap_aux / cheap_delegation / cache_1h toggles behind an
explicit two-step confirm. See DESIGN.md for the full spec.

No heavy/host-internal imports at module top level — only stdlib and this
plugin's own sibling modules (which themselves defer host imports into
function bodies). Everything a hook callback does is wrapped in try/except
so a bug here can never break the host agent loop.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any, Optional

from . import audit, cache_guard, ledger, report, toggles

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook callbacks — plain def, fast, single insert max, never raise.
# ---------------------------------------------------------------------------

def _on_post_api_request(
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    api_duration: Optional[float] = None,
    finish_reason: str = "",
    usage: Optional[dict] = None,
    **_: Any,
) -> None:
    # Cache-bust check first, so it compares against the ledger's prior
    # request row before this one gets inserted below.
    try:
        cache_guard.on_post_api_request(session_id=session_id, model=model, provider=provider)
    except Exception:
        logger.debug("token-guard: cache_guard hook failed", exc_info=True)

    try:
        duration_ms = api_duration * 1000.0 if isinstance(api_duration, (int, float)) else None
        ledger.record_request(
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            model=model,
            provider=provider,
            api_mode=api_mode,
            duration_ms=duration_ms,
            usage=usage,
            finish_reason=finish_reason,
        )
    except Exception:
        logger.debug("token-guard: record_request hook failed", exc_info=True)


def _on_api_request_error(
    session_id: str = "",
    model: str = "",
    error_type: str = "",
    status_code: Optional[int] = None,
    retry_count: Optional[int] = None,
    retryable: Optional[bool] = None,
    **_: Any,
) -> None:
    try:
        ledger.record_error(
            session_id=session_id,
            model=model,
            error_type=error_type,
            status_code=status_code,
            retry_count=retry_count,
            retryable=retryable,
        )
    except Exception:
        logger.debug("token-guard: record_error hook failed", exc_info=True)


def _on_post_tool_call(
    tool_name: str = "",
    duration_ms: Optional[float] = None,
    session_id: str = "",
    **_: Any,
) -> None:
    try:
        ledger.record_tool_call(session_id=session_id, tool_name=tool_name, duration_ms=duration_ms)
    except Exception:
        logger.debug("token-guard: record_tool_call hook failed", exc_info=True)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

_TOKENGUARD_HELP = (
    "/tokenguard — управление token-guard\n"
    "  status                                   статус переключателей\n"
    "  audit                                     проверка конфигурации\n"
    "  enable <toggle> [confirm]                 включить переключатель\n"
    "  disable <toggle>                          отключить переключатель\n"
    "  set-cheap-model <provider> <model>        задать дешёвую модель\n"
    "Переключатели: cheap_aux, cheap_delegation, cache_1h"
)


def _split_args(raw_args: str) -> list:
    if not raw_args:
        return []
    try:
        return shlex.split(raw_args)
    except ValueError:
        return raw_args.split()


def _handle_cost(raw_args: str) -> str:
    try:
        return report.render_cost((raw_args or "").strip())
    except Exception:
        logger.debug("token-guard: /cost handler failed", exc_info=True)
        return "token-guard: не удалось построить отчёт /cost."


def _handle_tokenguard(raw_args: str) -> str:
    try:
        argv = _split_args(raw_args)
        if not argv:
            return _TOKENGUARD_HELP

        sub = argv[0].lower()
        rest = argv[1:]

        if sub == "status":
            return report.render_status()
        if sub == "audit":
            return report.render_audit(audit.run_audit())
        if sub == "enable":
            if not rest:
                return "Использование: /tokenguard enable <toggle> [confirm]"
            confirm = len(rest) > 1 and rest[1].lower() == "confirm"
            return toggles.enable_flow(rest[0], confirm)
        if sub == "disable":
            if not rest:
                return "Использование: /tokenguard disable <toggle>"
            return toggles.disable(rest[0])
        if sub == "set-cheap-model":
            if len(rest) < 2:
                return "Использование: /tokenguard set-cheap-model <provider> <model>"
            return toggles.set_cheap_model(rest[0], rest[1])

        return f"Неизвестная подкоманда: {sub}\n\n{_TOKENGUARD_HELP}"
    except Exception:
        logger.debug("token-guard: /tokenguard handler failed", exc_info=True)
        return "token-guard: команда не выполнена (см. debug-лог)."


# ---------------------------------------------------------------------------
# CLI command — mirrors the slash-command subcommands as `hermes token-guard ...`
# ---------------------------------------------------------------------------

def _cli_setup(subparser) -> None:
    sub = subparser.add_subparsers(dest="tg_command")

    cost_p = sub.add_parser("cost", help="Расход токенов за период")
    cost_p.add_argument("days", nargs="?", default="7")

    sub.add_parser("status", help="Статус переключателей token-guard")
    sub.add_parser("audit", help="Аудит конфигурации")

    enable_p = sub.add_parser("enable", help="Включить переключатель")
    enable_p.add_argument("toggle")
    enable_p.add_argument("confirm", nargs="?", default="")

    disable_p = sub.add_parser("disable", help="Отключить переключатель")
    disable_p.add_argument("toggle")

    setcm_p = sub.add_parser("set-cheap-model", help="Задать дешёвую модель для тумблеров")
    setcm_p.add_argument("provider")
    setcm_p.add_argument("model")


def _cli_handler(args: Any) -> None:
    cmd = getattr(args, "tg_command", None)
    if cmd == "cost":
        print(report.render_cost(getattr(args, "days", "7")))
    elif cmd == "status":
        print(report.render_status())
    elif cmd == "audit":
        print(report.render_audit(audit.run_audit()))
    elif cmd == "enable":
        confirm = (getattr(args, "confirm", "") or "").lower() == "confirm"
        print(toggles.enable_flow(args.toggle, confirm))
    elif cmd == "disable":
        print(toggles.disable(args.toggle))
    elif cmd == "set-cheap-model":
        print(toggles.set_cheap_model(args.provider, args.model))
    else:
        print("Использование: hermes token-guard <cost|status|audit|enable|disable|set-cheap-model>")


# ---------------------------------------------------------------------------
# register(ctx)
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

    ctx.register_command(
        "cost",
        handler=_handle_cost,
        description="Расход токенов за период (по умолчанию 7 дней).",
        args_hint="дней",
    )
    ctx.register_command(
        "tokenguard",
        handler=_handle_tokenguard,
        description="Статус, аудит и переключатели token-guard.",
        args_hint="status|audit|enable|disable|set-cheap-model",
    )

    ctx.register_cli_command(
        "token-guard",
        help="Наблюдение за расходом токенов и переключатели экономии",
        setup_fn=_cli_setup,
        handler_fn=_cli_handler,
        description="token-guard: журнал запросов, аудит конфигурации, тумблеры экономии.",
    )
