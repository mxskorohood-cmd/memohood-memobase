"""Shared pytest fixtures for hermes-setup's test suite.

Isolation pattern (mirrors ``plugins/hermes-kb/tests/conftest.py``, which
mirrors ``tests/plugins/test_disk_cleanup_plugin.py`` in the hermes-agent
checkout): monkeypatch ``HERMES_HOME`` to a per-test tmp dir, and load the
plugin's modules via a synthetic ``hermes_plugins_test_<id>.hermes_setup``
namespace package so relative imports (``from . import registry``, etc.)
resolve exactly as they would under the real ``PluginManager`` loader.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

# Captured at conftest IMPORT time -- i.e. before any test's monkeypatch has
# run -- so this always reflects the REAL ambient hermes installation, not a
# test's isolated tmp HERMES_HOME. Used only by the session-wide guard below.
REAL_HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
REAL_CONFIG_PATH = REAL_HERMES_HOME / "config.yaml"
REAL_ENV_PATH = REAL_HERMES_HOME / ".env"

# `--import-mode=importlib` (pytest.ini) deliberately does NOT add the tests
# directory to sys.path -- add it explicitly so a future `from _helpers
# import ...` would resolve without needing tests/__init__.py (which would
# turn this into a package and complicate the synthetic-namespace trick
# below for no benefit). Not currently used, kept for parity with hermes-kb.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def load_plugin_package() -> Any:
    """Import a FRESH copy of the entire hermes-setup plugin package
    (__init__ + every submodule reachable from it) under a unique synthetic
    package name, and return the __init__ module object.

    All submodules become available as attributes of the returned object
    (``pkg.registry``, ``pkg.envfile``, ``pkg.wizard``) -- a plain
    consequence of Python's import system always setting an imported
    submodule as an attribute of its parent package.
    """
    suffix = uuid.uuid4().hex[:10]
    pkg_name = f"hermes_plugins_test_{suffix}"
    full_pkg = f"{pkg_name}.hermes_setup"

    ns = types.ModuleType(pkg_name)
    ns.__path__ = []
    sys.modules[pkg_name] = ns

    spec = importlib.util.spec_from_file_location(
        full_pkg,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = full_pkg
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[full_pkg] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def setup_plugin(tmp_path, monkeypatch):
    """The single fixture almost every test uses: isolates HERMES_HOME to a
    fresh tmp dir AND returns a freshly-imported copy of the whole plugin
    package. Depend on this (not a bare ``tmp_path``) whenever a test touches
    config.yaml / .env / anything that reads ``HERMES_HOME``.
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # HERMES_HOME_OVERRIDE / profile machinery some hermes-core helpers check;
    # unset so get_hermes_home() takes the plain HERMES_HOME env path.
    monkeypatch.delenv("HERMES_HOME_OVERRIDE", raising=False)
    pkg = load_plugin_package()
    pkg._hermes_home_for_test = hermes_home  # convenience for tests that want the Path directly
    # Every wizard test must start from a clean in-memory state table --
    # the synthetic-namespace reload above already guarantees a fresh
    # `wizard._STATE` module dict per test, so no explicit reset is needed,
    # but assert it here as a canary in case that ever stops being true.
    assert pkg.wizard._STATE == {}
    return pkg


class FakeCtx:
    """Minimal stand-in for ``hermes_cli.plugins.PluginContext`` -- just
    enough surface for ``__init__.py``'s ``register(ctx)`` to run against
    (mirrors API_CONTRACT_PLUGINS.md §2's documented signatures exactly,
    and hermes-kb/tests/conftest.py's own FakeCtx)."""

    def __init__(self) -> None:
        self.hooks: Dict[str, list] = {}
        self.commands: Dict[str, Dict[str, Any]] = {}

    def register_hook(self, event: str, callback):
        self.hooks.setdefault(event, []).append(callback)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}


@pytest.fixture()
def fake_ctx():
    return FakeCtx()


# ---------------------------------------------------------------------------
# Session-wide guard: the REAL HERMES_HOME's config.yaml/.env must never be
# touched by this suite -- every test isolates HERMES_HOME via the
# `setup_plugin` fixture above. Mirrors hermes-kb/tests/conftest.py's
# `_guard_real_hermes_home` fixture.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _guard_real_hermes_home():
    config_mtime_before = REAL_CONFIG_PATH.stat().st_mtime if REAL_CONFIG_PATH.exists() else None
    env_mtime_before = REAL_ENV_PATH.stat().st_mtime if REAL_ENV_PATH.exists() else None

    yield

    config_mtime_after = REAL_CONFIG_PATH.stat().st_mtime if REAL_CONFIG_PATH.exists() else None
    assert config_mtime_after == config_mtime_before, (
        f"REAL HERMES_HOME/config.yaml ({REAL_CONFIG_PATH}) was modified during the test run -- "
        "a test must have called save_config()/set_config_value() against the real config "
        "instead of the isolated tmp HERMES_HOME."
    )
    env_mtime_after = REAL_ENV_PATH.stat().st_mtime if REAL_ENV_PATH.exists() else None
    assert env_mtime_after == env_mtime_before, (
        f"REAL HERMES_HOME/.env ({REAL_ENV_PATH}) was modified during the test run -- "
        "a test must have written a key against the real .env instead of the isolated tmp HERMES_HOME."
    )
