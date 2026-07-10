"""token-guard ledger — SQLite-backed request/error/tool_call/event log.

Pure observation layer: records what happened, computes no dollars (host's
``hermes_state.SessionDB`` is the authoritative $ source — see report.py).
Every public function here is best-effort: on any failure it logs at debug
level and returns a safe empty/zero value instead of raising, so a broken
ledger can never take down the host agent loop.

Stdlib only. Connection is a thread-safe lazy singleton (see plugin_utils);
tests that need a fresh HERMES_HOME must reload this module so the closure
resets (see tests/test_plugin.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from plugins.plugin_utils import lazy_singleton

logger = logging.getLogger(__name__)

_STATE_SUBDIR = "token-guard"
_DB_FILENAME = "ledger.db"
_RETENTION_SECONDS = 90 * 24 * 3600

_prune_lock = threading.Lock()
_pruned_this_process = False


def _db_path() -> Path:
    return get_hermes_home() / _STATE_SUBDIR / _DB_FILENAME


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    session_id TEXT,
    task_id TEXT,
    turn_id TEXT,
    api_request_id TEXT,
    model TEXT,
    provider TEXT,
    api_mode TEXT,
    duration_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    finish_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    session_id TEXT,
    model TEXT,
    error_type TEXT,
    status_code INTEGER,
    retry_count INTEGER,
    retryable INTEGER
);
CREATE INDEX IF NOT EXISTS idx_errors_ts ON errors(ts);
CREATE INDEX IF NOT EXISTS idx_errors_session ON errors(session_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    session_id TEXT,
    tool_name TEXT,
    duration_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    session_id TEXT,
    kind TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
"""


def _init_conn() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


@lazy_singleton
def _get_conn() -> sqlite3.Connection:
    return _init_conn()


def _maybe_prune(conn: sqlite3.Connection) -> None:
    """Delete rows older than the retention window. At most once per process."""
    global _pruned_this_process
    if _pruned_this_process:
        return
    with _prune_lock:
        if _pruned_this_process:
            return
        cutoff = time.time() - _RETENTION_SECONDS
        try:
            for table in ("requests", "errors", "tool_calls", "events"):
                conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            conn.commit()
        except Exception:
            logger.debug("token-guard: prune failed", exc_info=True)
        finally:
            _pruned_this_process = True


# ---------------------------------------------------------------------------
# Writers — called straight from hook callbacks. Single INSERT, never raise.
# ---------------------------------------------------------------------------

def record_request(
    *,
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    duration_ms: Optional[float] = None,
    usage: Optional[Dict[str, Any]] = None,
    finish_reason: str = "",
) -> None:
    try:
        usage = usage or {}
        conn = _get_conn()
        conn.execute(
            "INSERT INTO requests (ts, session_id, task_id, turn_id, api_request_id, "
            "model, provider, api_mode, duration_ms, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, reasoning_tokens, finish_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), session_id, task_id, turn_id, api_request_id,
                model, provider, api_mode, duration_ms,
                usage.get("input_tokens") or 0,
                usage.get("output_tokens") or 0,
                usage.get("cache_read_tokens") or 0,
                usage.get("cache_write_tokens") or 0,
                usage.get("reasoning_tokens") or 0,
                finish_reason,
            ),
        )
        conn.commit()
        _maybe_prune(conn)
    except Exception:
        logger.debug("token-guard: record_request failed", exc_info=True)


def record_error(
    *,
    session_id: str = "",
    model: str = "",
    error_type: str = "",
    status_code: Optional[int] = None,
    retry_count: Optional[int] = None,
    retryable: Optional[bool] = None,
) -> None:
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO errors (ts, session_id, model, error_type, status_code, "
            "retry_count, retryable) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), session_id, model, error_type, status_code,
                retry_count, 1 if retryable else 0,
            ),
        )
        conn.commit()
    except Exception:
        logger.debug("token-guard: record_error failed", exc_info=True)


def record_tool_call(*, session_id: str = "", tool_name: str = "", duration_ms: Optional[float] = None) -> None:
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO tool_calls (ts, session_id, tool_name, duration_ms) VALUES (?, ?, ?, ?)",
            (time.time(), session_id, tool_name, duration_ms),
        )
        conn.commit()
    except Exception:
        logger.debug("token-guard: record_tool_call failed", exc_info=True)


def record_event(*, session_id: str = "", kind: str = "", detail: str = "") -> None:
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO events (ts, session_id, kind, detail) VALUES (?, ?, ?, ?)",
            (time.time(), session_id, kind, detail),
        )
        conn.commit()
    except Exception:
        logger.debug("token-guard: record_event failed", exc_info=True)


# ---------------------------------------------------------------------------
# Readers — used by cache_guard.py, audit.py, report.py. Never raise.
# ---------------------------------------------------------------------------

def last_request_model(session_id: str) -> Optional[Tuple[str, str]]:
    """Return (model, provider) of the most recent recorded request for a session."""
    if not session_id:
        return None
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT model, provider FROM requests WHERE session_id = ? ORDER BY ts DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return (row["model"] or "", row["provider"] or "")
    except Exception:
        logger.debug("token-guard: last_request_model failed", exc_info=True)
        return None


def requests_in_window(days: float) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT * FROM requests WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("token-guard: requests_in_window failed", exc_info=True)
        return []


def errors_in_window(days: float) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT * FROM errors WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("token-guard: errors_in_window failed", exc_info=True)
        return []


def tool_calls_in_window(days: float) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT * FROM tool_calls WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("token-guard: tool_calls_in_window failed", exc_info=True)
        return []


def events_in_window(days: float, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        if kind:
            rows = conn.execute(
                "SELECT * FROM events WHERE ts >= ? AND kind = ? ORDER BY ts ASC",
                (cutoff, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("token-guard: events_in_window failed", exc_info=True)
        return []


def history_span_days() -> float:
    """Return how many days of request history the ledger currently holds."""
    try:
        conn = _get_conn()
        row = conn.execute("SELECT MIN(ts) AS oldest FROM requests").fetchone()
        if row is None or row["oldest"] is None:
            return 0.0
        return max(0.0, (time.time() - float(row["oldest"])) / 86400)
    except Exception:
        logger.debug("token-guard: history_span_days failed", exc_info=True)
        return 0.0


def reset_for_tests() -> None:
    """Test-only helper: drop the cached connection so a new HERMES_HOME takes effect."""
    try:
        _get_conn.reset()
    except Exception:
        pass
    global _pruned_this_process
    _pruned_this_process = False
