"""token-guard report — RU text rendering for /cost and /tokenguard.

Pure presentation layer: reads ledger.py + cache_guard.py + toggles.py and
formats plain, monospace-friendly Russian text. Enriches with $ figures on
a best-effort basis from hermes_state.SessionDB (host-priced, authoritative)
and agent.usage_pricing (best-effort per-model estimate) — both imports are
deferred and wrapped, so a missing/renamed host API only degrades the
dollar column to "н/д", it never breaks the command.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

from . import cache_guard, ledger, toggles

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 7


def _safe(fn: Callable[..., str]) -> Callable[..., str]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.debug("token-guard: %s failed", fn.__name__, exc_info=True)
            return "token-guard: не удалось построить отчёт (см. debug-лог)."
    return wrapper


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def _fmt_usd(amount: Optional[float]) -> str:
    if amount is None:
        return "н/д"
    return f"${amount:.2f}"


def _parse_days(days_arg: Any) -> float:
    try:
        text = str(days_arg).strip()
        if not text:
            return DEFAULT_DAYS
        days = float(text)
        return days if days > 0 else DEFAULT_DAYS
    except Exception:
        return DEFAULT_DAYS


def _session_row(session_id: str) -> Dict[str, Any]:
    """Best-effort $ lookup from hermes_state.SessionDB (read-only, no write lock)."""
    try:
        db_path = get_hermes_home() / "state.db"
        if not db_path.exists():
            return {}
        from hermes_state import SessionDB
        db = SessionDB(db_path=db_path, read_only=True)
        row = db.get_session(session_id)
        return row or {}
    except Exception:
        logger.debug("token-guard: session cost lookup failed", exc_info=True)
        return {}


def _per_model_cost_estimate(model: str, usage: Dict[str, int]) -> Optional[float]:
    """Best-effort per-model $ estimate. None on any failure or unknown pricing."""
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        cu = CanonicalUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            reasoning_tokens=usage.get("reasoning_tokens", 0),
        )
        result = estimate_usage_cost(model, cu)
        amount = getattr(result, "amount_usd", None)
        return float(amount) if amount is not None else None
    except Exception:
        return None


@_safe
def render_cost(days_arg: Any = "") -> str:
    days = _parse_days(days_arg)
    rows = ledger.requests_in_window(days)
    errors = ledger.errors_in_window(days)

    lines: List[str] = [f"token-guard — расход за {days:g} дн."]

    if not rows:
        lines.append("Нет данных в журнале за этот период.")
    else:
        total_input = sum(r.get("input_tokens") or 0 for r in rows)
        total_output = sum(r.get("output_tokens") or 0 for r in rows)
        total_cache_read = sum(r.get("cache_read_tokens") or 0 for r in rows)
        total_cache_write = sum(r.get("cache_write_tokens") or 0 for r in rows)
        total_reasoning = sum(r.get("reasoning_tokens") or 0 for r in rows)

        lines.append(f"Запросов: {_fmt_int(len(rows))}")
        lines.append(
            "Токены — вход: {inp} | выход: {out} | кэш-чтение: {cr} | "
            "кэш-запись: {cw} | рассуждения: {rs}".format(
                inp=_fmt_int(total_input), out=_fmt_int(total_output),
                cr=_fmt_int(total_cache_read), cw=_fmt_int(total_cache_write),
                rs=_fmt_int(total_reasoning),
            )
        )

        per_model_tokens: Dict[str, int] = {}
        per_model_usage: Dict[str, Dict[str, int]] = {}
        for r in rows:
            model = r.get("model") or "unknown"
            tok = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
            per_model_tokens[model] = per_model_tokens.get(model, 0) + tok
            bucket = per_model_usage.setdefault(model, {
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "reasoning_tokens": 0,
            })
            for k in bucket:
                bucket[k] += r.get(k) or 0

        top_models = sorted(per_model_tokens.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines.append("")
        lines.append("Топ-5 моделей по токенам:")
        for model, tok in top_models:
            cost = _per_model_cost_estimate(model, per_model_usage[model])
            cost_str = f", ~{_fmt_usd(cost)}" if cost is not None else ""
            lines.append(f"  {model}: {_fmt_int(tok)}{cost_str}")

        per_session_tokens: Dict[str, int] = {}
        for r in rows:
            sid = r.get("session_id") or ""
            if not sid:
                continue
            per_session_tokens[sid] = per_session_tokens.get(sid, 0) + (
                (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
            )
        session_costs = []
        for sid, tok in per_session_tokens.items():
            info = _session_row(sid)
            cost = info.get("estimated_cost_usd") if info else None
            if cost is None and info:
                cost = info.get("actual_cost_usd")
            session_costs.append((sid, tok, cost))
        session_costs.sort(key=lambda t: (t[2] if t[2] is not None else -1.0, t[1]), reverse=True)

        lines.append("")
        lines.append("Топ-5 сессий:")
        for sid, tok, cost in session_costs[:5]:
            short = (sid[:12] + "…") if len(sid) > 12 else sid
            lines.append(f"  {short}: {_fmt_int(tok)} токенов, {_fmt_usd(cost)}")

    hit_stats = cache_guard.hit_rate_stats(days)
    overall = hit_stats.get("overall")
    overall_str = f"{overall * 100:.0f}%" if overall is not None else "н/д"
    lines.append("")
    lines.append(
        f"Hit-rate кэша: {overall_str} | сбросов кэша: {hit_stats.get('cache_bust_count', 0)}"
    )
    for w in hit_stats.get("warnings", []):
        sid = w.get("session_id", "")
        short = (sid[:12] + "…") if len(sid) > 12 else sid
        lines.append(
            f"  предупреждение: сессия {short} — hit-rate {w['hit_rate'] * 100:.0f}% "
            f"при {w['requests']} запросах"
        )

    retryable = sum(1 for e in errors if e.get("retryable"))
    lines.append("")
    lines.append(f"Ошибок: {_fmt_int(len(errors))} (с повтором: {_fmt_int(retryable)})")

    active = [name for name, enabled in toggles.status().items() if enabled is True]
    lines.append("")
    lines.append(f"Активные переключатели: {', '.join(active) if active else 'нет'}")

    return "\n".join(lines)


@_safe
def render_status() -> str:
    lines = ["token-guard — статус переключателей"]
    st = toggles.status()
    for name in sorted(toggles.TOGGLE_KEYS):
        state = "включён" if st.get(name) else "выключен"
        lines.append(f"  {name}: {state}")
    for name in sorted(toggles.RESERVED_TOGGLES):
        lines.append(f"  {name}: зарезервировано (появится позже)")

    cheap_model, cheap_provider = toggles.get_cheap_model()
    lines.append("")
    if cheap_model and cheap_provider:
        lines.append(f"Дешёвая модель: {cheap_provider}/{cheap_model}")
    else:
        lines.append("Дешёвая модель не задана: /tokenguard set-cheap-model <provider> <model>")
    return "\n".join(lines)


@_safe
def render_audit(findings: List[Dict[str, str]]) -> str:
    if not findings:
        return "token-guard audit: замечаний нет."
    icons = {"warning": "[!]", "info": "[i]"}
    lines = ["token-guard audit:"]
    for f in findings:
        lines.append("")
        lines.append(f"{icons.get(f.get('severity', 'info'), '[i]')} {f.get('title_ru', '')}")
        if f.get("detail_ru"):
            lines.append(f"    {f['detail_ru']}")
        if f.get("fix_hint_ru"):
            lines.append(f"    -> {f['fix_hint_ru']}")
    return "\n".join(lines)
