"""MemoHood external-spend ledger (mirrors hermes-kb's, per HERMES_UPGRADES.md
§1.3: "captures эмбеддятся через Cloudflare -> внешняя зависимость в горячем
пути памяти -> на память распространяются KB-леджер трат и месячный
потолок").

VENDORED from ``hermes-kb/ledger.py`` (v0.1.0, 2026-07-06). Cloudflare
(embed) and Cohere (rerank) calls happen entirely outside hermes-core's own
token-guard/cost hooks (they are plain HTTP calls this plugin makes, not LLM
API calls hermes tracks) — so this module is the ONLY place their dollar
cost is recorded and capped for the MEMORY side (a separate ledger instance
from hermes-kb's — memohood's ``spend`` table lives in ``memory.db``, not
``kb.db``; the two plugins do not share a database or a monthly ceiling).

ONE adaptation from the original: the ``from . import db`` import becomes
``from .. import db`` — memohood's ``db.py`` (with the identical
``record_spend``/``monthly_spend`` function signatures, per DESIGN_v1.md's
schema spec explicitly calling out ``spend(...) -- same as kb ledger``)
lives at the plugin root (``plugins/memohood/db.py``), one level up from
this ``_engine/`` package, whereas hermes-kb's ``db.py`` was a same-package
sibling of its ``ledger.py``.

Public surface (used by ``_engine/embed.py``, ``_engine/rerank.py``, and
eventually ``capture.py``):

    record_call(conn, provider, op, units=None, est_usd=None) -> int
    estimate_cost_usd(provider, op, units) -> float
    check_monthly_ceiling(conn, provider, memohood_cfg) -> (within_ceiling, spent_so_far, ceiling)
    ensure_within_ceiling(conn, provider, memohood_cfg)  # raises LedgerError if already over

Pricing is a best-effort estimate (API_CONTRACT_PLUGINS.md §3 precedent:
"best-effort" cost estimation is the accepted standard here too) — these
constants should be reviewed periodically against the providers' current
published pricing, not treated as billing-accurate.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional, Tuple

from .. import db

logger = logging.getLogger("memohood.ledger")


class LedgerError(RuntimeError):
    """Raised by :func:`ensure_within_ceiling` when a provider's monthly
    spend ceiling has already been reached — caller must not start the job."""


# (provider, op) -> $ per billing unit. Unit meaning is documented at each
# call site (embed.py passes "thousands of approx tokens" for cloudflare
# embed calls; rerank.py passes "number of rerank search-units" for cohere
# rerank calls).
#
# Cloudflare Workers AI @cf/baai/bge-m3: priced per-token, on the order of
# ~$0.012 / 1M input tokens as of Cloudflare's public Workers AI pricing
# page — expressed here as $ per 1K tokens.
# Cohere rerank-v3.5: priced per "search unit" (roughly: 1 query x up to 100
# documents), historically ~$2.00 / 1000 search units.
_PRICING_USD: dict = {
    ("cloudflare", "embed"): 0.000012,   # $ per 1,000 approx-tokens
    ("cohere", "rerank"): 0.002,         # $ per rerank search-unit
    ("gemini", "extract"): 0.0,          # flash-lite generous free tier; see extract_llm.py
}

_DEFAULT_FALLBACK_PRICE = 0.0  # unknown (provider, op): estimate as free rather than guess wildly


def estimate_cost_usd(provider: str, op: str, units: float) -> float:
    """Best-effort dollar estimate for *units* of (*provider*, *op*).

    Returns 0.0 for an unrecognized (provider, op) pair rather than raising
    — callers should not let a pricing-table gap block an otherwise-valid
    operation, but SHOULD log/surface that the estimate is unknown (spend is
    still recorded with ``est_usd=0.0`` and remains visible in the ledger for
    manual review).
    """
    price = _PRICING_USD.get((provider, op))
    if price is None:
        logger.warning("ledger: no pricing entry for (%s, %s); estimating $0.00", provider, op)
        price = _DEFAULT_FALLBACK_PRICE
    return max(0.0, float(units)) * price


def record_call(
    conn: sqlite3.Connection,
    *,
    provider: str,
    op: str,
    units: Optional[float] = None,
    est_usd: Optional[float] = None,
) -> int:
    """Append one row to the ``spend`` table. Thin, single-call-site wrapper
    around :func:`db.record_spend` so every module (embed.py/rerank.py/
    extract_llm.py) records spend the same way. If ``est_usd`` is not
    supplied, it is computed via :func:`estimate_cost_usd` (requires
    ``units``).
    """
    if est_usd is None and units is not None:
        est_usd = estimate_cost_usd(provider, op, units)
    return db.record_spend(conn, provider=provider, op=op, units=units, est_usd=est_usd)


def check_monthly_ceiling(
    conn: sqlite3.Connection, provider: str, memohood_cfg: dict
) -> Tuple[bool, float, float]:
    """Return ``(within_ceiling, spent_so_far_usd, ceiling_usd)`` for
    *provider* over the trailing 30 days, per
    ``memory.memohood.monthly_ceiling_usd.<provider>`` (DESIGN_v1.md config
    block).

    A provider with no configured ceiling (or a ceiling of 0/None, meaning
    "not tracked") is always reported ``within_ceiling=True`` — callers that
    want a hard "0 = forbidden" semantics should check ``ceiling_usd`` itself
    rather than relying on this function to enforce it.
    """
    ceiling = (memohood_cfg.get("monthly_ceiling_usd") or {}).get(provider)
    spent = db.monthly_spend(conn, provider)
    if ceiling is None:
        return True, spent, float("inf")
    ceiling = float(ceiling)
    return (spent < ceiling), spent, ceiling


def ensure_within_ceiling(conn: sqlite3.Connection, provider: str, memohood_cfg: dict) -> None:
    """Raise :class:`LedgerError` if *provider*'s trailing-30-day spend has
    already reached/exceeded its configured monthly ceiling. Callers should
    call this BEFORE spending more, not after — it does not itself prevent
    the in-flight call that triggered it from completing.
    """
    within, spent, ceiling = check_monthly_ceiling(conn, provider, memohood_cfg)
    if not within:
        raise LedgerError(
            f"{provider} monthly spend ceiling reached: ${spent:.4f} spent >= ${ceiling:.4f} ceiling "
            f"(memory.memohood.monthly_ceiling_usd.{provider}); job refused"
        )
