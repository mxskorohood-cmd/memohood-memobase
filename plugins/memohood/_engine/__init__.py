"""Vendored hybrid-search engine for the memohood memory provider.

Every module in this package is a COPY (not a shared import) of the
corresponding module in ``C:/Users/admin/AppData/Local/hermes/plugins/memobase/``
(memobase v0.1.0, verified 2026-07-06), adapted to memohood's schema:

  * ``chunks``/``chunks_fts`` (per-collection, joined to ``documents``)
    -> ``captures``/``captures_fts`` (single global corpus, no per-collection
    join -- ``captures`` already carries all its own metadata columns).
  * ``chunk_id`` -> ``capture_id``; ``text``/``text_stem`` -> ``content``/
    ``content_stem``.
  * per-collection ``vec_c{collection_id}`` vec0 tables -> one global
    ``captures_vec`` table (memory has no "collections" concept).
  * ``tombstoned_at``/``superseded_at`` (KB) -> ``invalidated_at`` (memohood's
    bi-temporal ``valid_from``/``invalidated_at`` columns, DESIGN_v1.md).

Per HERMES_UPGRADES.md §1.3's accepted decision ("переиспользуем уже
собранный и протестированный движок retrieve/embed/rerank из hermes-kb —
вендорим копией в плагин памяти, без общей зависимости"), this package has
ZERO import dependency on the ``hermes-kb`` plugin -- it is a standalone
copy that memohood's ``db.py``/``capture.py``/``provider.py`` import via
``from ._engine import stem, security, embed, rerank, retrieve, ledger``.

Where a module's tested logic did not need to change for the schema rename
(``rerank.py``, ``stem.py``, ``security.py`` -- none of these reference
chunk/capture table names directly), it is vendored verbatim (only the
module docstring/logger name were touched for provenance, per the task's
"keep the code's tested logic intact").
"""

from __future__ import annotations
