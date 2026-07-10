"""hermes-setup envfile — upsert-writer for ``HERMES_HOME/.env``.

Three cases per ``KEY=VALUE`` line, in priority order:

1. An active (uncommented) ``KEY=...`` line already exists -> its value is
   replaced in place (first match wins if there happen to be duplicates).
2. No active line, but a commented-out placeholder (``# KEY=`` / ``#KEY=``,
   any amount of whitespace) exists -> it is uncommented and filled in,
   preserving its original position in the file.
3. Neither exists -> a new ``KEY=VALUE`` line is appended at EOF.

Always UTF-8. Never logs or raises with the key's value in the message.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable

# `#` or `# ` (any amount of whitespace) prefix, then the key, then `=`.
_LINE_RE = re.compile(r"^(?P<indent>\s*)(?P<hash>#+\s*)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<rest>.*)$")


def mask_key(value: str) -> str:
    """Return a chat-safe stand-in for *value*: first 4 characters + '…'.

    Never returns enough of the value to reconstruct it. Empty input yields
    an empty string (nothing to mask).
    """
    if not value:
        return ""
    return f"{value[:4]}…"


def upsert_env_value(env_path: Path, key: str, value: str) -> str:
    """Upsert ``key=value`` into the .env file at *env_path*.

    Creates the file (and its parent directory) if missing. Returns one of
    ``"replaced"``, ``"uncommented"``, ``"appended"`` describing which case
    fired, for callers that want to log/report the action (never the value).
    """
    env_path = Path(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)

    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = text.split("\n")

    active_idx = None
    commented_idx = None
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if not m or m.group("key") != key:
            continue
        if m.group("hash"):
            if commented_idx is None:
                commented_idx = i
        else:
            if active_idx is None:
                active_idx = i

    new_line = f"{key}={value}"

    if active_idx is not None:
        lines[active_idx] = new_line
        action = "replaced"
    elif commented_idx is not None:
        lines[commented_idx] = new_line
        action = "uncommented"
    else:
        # Append. `text.split("\n")` on a file that ends with a trailing
        # newline yields a final "" element -- reuse that slot instead of
        # creating a blank line before the new one.
        if lines and lines[-1] == "":
            lines[-1] = new_line
        elif lines == [""]:
            lines = [new_line]
        else:
            lines.append(new_line)
        action = "appended"

    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    env_path.write_text(out, encoding="utf-8")
    return action


def has_active_value(env_path: Path, key: str) -> bool:
    """True if *key* has a non-empty, uncommented value in the .env file."""
    env_path = Path(env_path)
    if not env_path.exists():
        return False
    text = env_path.read_text(encoding="utf-8")
    for line in text.split("\n"):
        m = _LINE_RE.match(line)
        if m and m.group("key") == key and not m.group("hash"):
            if m.group("rest").strip():
                return True
    return False


def scan_keys(env_path: Path, keys: Iterable[str]) -> Dict[str, bool]:
    """Bulk convenience: ``{key: has_active_value(env_path, key)}`` for every
    key in *keys*, reading the file only once."""
    env_path = Path(env_path)
    result = {k: False for k in keys}
    if not env_path.exists():
        return result
    text = env_path.read_text(encoding="utf-8")
    for line in text.split("\n"):
        m = _LINE_RE.match(line)
        if not m or m.group("hash"):
            continue
        k = m.group("key")
        if k in result and m.group("rest").strip():
            result[k] = True
    return result
