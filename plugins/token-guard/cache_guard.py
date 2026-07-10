"""token-guard cache guard — cache-bust detection + hit-rate reporting.

Cache busts happen when the model/provider changes mid-session: Anthropic
(and most providers) key the prompt cache on the exact model id, so any
switch throws away the accumulated cache-read discount for that session.
This module watches ``post_api_request`` for that pattern and logs an
``events`` row (kind="cache_bust"); ``hit_rate_stats`` is the report-time
read side used by report.py / audit.py.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Tuple

from . import ledger

logger = logging.getLogger(__name__)

# In-memory (model, provider) per session — fast path, avoids a ledger read
# on every request. Falls back to a ledger lookup on first sight of a
# session in this process (e.g. after a restart).
_lock = threading.Lock()
_last_seen: Dict[str, Tuple[str, str]] = {}

# A session needs at least this many requests before a low hit-rate is
# worth flagging (single/first requests always show 0% and are noise).
MIN_REQUESTS_FOR_WARNING = 5
LOW_HIT_RATE_THRESHOLD = 0.3


def on_post_api_request(session_id: str = "", model: str = "", provider: str = "", **_: Any) -> None:
    """post_api_request hook callback — plain def, fast, never raises."""
    try:
        if not session_id:
            return
        current = (model or "", provider or "")
        with _lock:
            previous = _last_seen.get(session_id)
            if previous is None:
                previous = ledger.last_request_model(session_id)
            _last_seen[session_id] = current

        if previous and previous != ("", "") and previous != current:
            old_model = previous[0] or "?"
            new_model = current[0] or "?"
            ledger.record_event(
                session_id=session_id,
                kind="cache_bust",
                detail=f"{old_model} -> {new_model}",
            )
    except Exception:
        logger.debug("token-guard: cache_guard hook failed", exc_info=True)


def reset_for_tests() -> None:
    with _lock:
        _last_seen.clear()


def hit_rate_stats(days: float = 7) -> Dict[str, Any]:
    """Aggregate cache hit-rate over a window: overall, per-model, and
    per-session warnings for sessions with >=5 requests and hit-rate < 30%.

    Returns:
        {
            "overall": float | None,          # None when no input tokens recorded
            "per_model": {model: float, ...},
            "warnings": [{"session_id": ..., "hit_rate": ..., "requests": ...}, ...],
            "cache_bust_count": int,
        }
    """
    rows = ledger.requests_in_window(days)
    overall_read = 0
    overall_input = 0
    per_model_read: Dict[str, int] = {}
    per_model_input: Dict[str, int] = {}
    per_session_read: Dict[str, int] = {}
    per_session_input: Dict[str, int] = {}
    per_session_count: Dict[str, int] = {}

    for row in rows:
        model = row.get("model") or "unknown"
        session_id = row.get("session_id") or ""
        read = row.get("cache_read_tokens") or 0
        inp = row.get("input_tokens") or 0

        overall_read += read
        overall_input += inp
        per_model_read[model] = per_model_read.get(model, 0) + read
        per_model_input[model] = per_model_input.get(model, 0) + inp
        if session_id:
            per_session_read[session_id] = per_session_read.get(session_id, 0) + read
            per_session_input[session_id] = per_session_input.get(session_id, 0) + inp
            per_session_count[session_id] = per_session_count.get(session_id, 0) + 1

    overall = (overall_read / overall_input) if overall_input else None
    per_model = {
        m: (per_model_read[m] / per_model_input[m]) if per_model_input[m] else 0.0
        for m in per_model_input
    }

    warnings: List[Dict[str, Any]] = []
    for session_id, count in per_session_count.items():
        if count < MIN_REQUESTS_FOR_WARNING:
            continue
        total_input = per_session_input.get(session_id, 0)
        if not total_input:
            continue
        rate = per_session_read.get(session_id, 0) / total_input
        if rate < LOW_HIT_RATE_THRESHOLD:
            warnings.append({"session_id": session_id, "hit_rate": rate, "requests": count})

    cache_bust_count = len(ledger.events_in_window(days, kind="cache_bust"))

    return {
        "overall": overall,
        "per_model": per_model,
        "warnings": warnings,
        "cache_bust_count": cache_bust_count,
    }
