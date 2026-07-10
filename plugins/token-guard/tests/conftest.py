"""Shared fixtures for token-guard plugin tests.

Adds the local hermes-agent checkout to ``sys.path`` (so ``hermes_constants``,
``hermes_cli.config``, ``hermes_state``, ``plugins.plugin_utils``, ``utils``,
``model_tools``, ``agent.*`` are importable) and isolates ``HERMES_HOME`` to a
fresh tmp dir per test, matching the pattern in
``tests/plugins/test_disk_cleanup_plugin.py`` of the local checkout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Point at your local hermes-agent checkout. Default assumes the standard
# per-user location; override with the HERMES_AGENT_CHECKOUT env var if it moves.
_DEFAULT_CHECKOUT = os.path.expanduser(
    os.path.join(os.environ.get("LOCALAPPDATA", "~"), "hermes", "hermes-agent")
)
HERMES_CHECKOUT = Path(os.environ.get("HERMES_AGENT_CHECKOUT", _DEFAULT_CHECKOUT))

if str(HERMES_CHECKOUT) not in sys.path:
    sys.path.insert(0, str(HERMES_CHECKOUT))


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a fresh per-test tmp dir.

    Each test re-imports the plugin package from scratch (see
    ``_load_plugin`` in test_plugin.py), so module-level singletons
    (the ledger connection, the cache-guard in-memory dict) are rebuilt
    against this fresh HERMES_HOME rather than leaking across tests.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Never let a managed-mode marker from a developer's real machine leak in.
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    yield home
