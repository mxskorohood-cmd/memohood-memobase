"""hermes-setup registry — static knowledge about OUR OWN plugins.

Pure, hermes-independent knowledge module: which folder name each plugin
uses under ``~/.hermes/plugins/<slug>/``, which ``.env`` keys each one
needs (metaphors included, for the wizard's chat copy), what a valid key
looks like, and how to (optionally) verify a key with one live HTTP call.

Nothing here imports ``hermes_cli`` or touches ``config.yaml`` — that is
wizard.py's job (API_CONTRACT_PLUGINS.md §2: config access goes through
``hermes_cli.config``, never hand-rolled YAML). This module only knows
filesystem facts (does a plugin folder exist) and .env-key-shape facts.

Values are NEVER logged or included in exceptions raised from this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Browser User-Agent for live key checks.
#
# Cloudflare (and some other edges) reject bare/no User-Agent requests
# outright, so every live_check below (not just Cloudflare's) sends the same
# realistic browser UA for consistency and to avoid being edge-blocked.
# ---------------------------------------------------------------------------

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

LIVE_CHECK_TIMEOUT_SECONDS = 15


def _http_get(url: str, headers: Optional[Dict[str, str]] = None):
    """Single GET, one attempt, no retries. Deferred ``requests`` import per
    the plugin API contract (heavy imports stay out of module-load time).

    Tests monkeypatch this exact function — never call ``requests`` directly
    from a ``live_check`` implementation.
    """
    import requests

    merged = {"User-Agent": BROWSER_USER_AGENT}
    merged.update(headers or {})
    return requests.get(url, headers=merged, timeout=LIVE_CHECK_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Key format validators
# ---------------------------------------------------------------------------

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_WHITESPACE_RE = re.compile(r"\s")


def _validate_cloudflare_account_id(v: str) -> bool:
    return bool(_HEX32_RE.match(v))


def _validate_gemini_key(v: str) -> bool:
    return v.startswith("AIza") and len(v) >= 30 and not _WHITESPACE_RE.search(v)


def _validate_groq_key(v: str) -> bool:
    return v.startswith("gsk_") and len(v) >= 10 and not _WHITESPACE_RE.search(v)


def _validate_apify_token(v: str) -> bool:
    return v.startswith("apify_") and len(v) >= 10 and not _WHITESPACE_RE.search(v)


def _validate_generic_nonempty(v: str) -> bool:
    return bool(v) and not _WHITESPACE_RE.search(v)


# ---------------------------------------------------------------------------
# Live checks — each takes the key's value and returns (ok, message).
# Never raises; always wraps the HTTP call in try/except.
# ---------------------------------------------------------------------------


def _live_check_cloudflare_api_token(value: str) -> Tuple[bool, str]:
    try:
        resp = _http_get(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            {"Authorization": f"Bearer {value}"},
        )
    except Exception as exc:
        return False, f"Не удалось проверить (сеть или таймаут): {exc}"
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return False, "Cloudflare ответил, но тело ответа не разобрать."
        if data.get("success"):
            return True, "Cloudflare подтвердил: токен активен."
        return False, "Cloudflare ответил, но токен не прошёл проверку."
    return False, f"Cloudflare вернул код {resp.status_code}."


def _live_check_gemini_key(value: str) -> Tuple[bool, str]:
    try:
        resp = _http_get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={value}"
        )
    except Exception as exc:
        return False, f"Не удалось проверить (сеть или таймаут): {exc}"
    if resp.status_code == 200:
        return True, "Gemini подтвердил: ключ действителен."
    return False, f"Gemini вернул код {resp.status_code}."


def _live_check_groq_key(value: str) -> Tuple[bool, str]:
    try:
        resp = _http_get(
            "https://api.groq.com/openai/v1/models",
            {"Authorization": f"Bearer {value}"},
        )
    except Exception as exc:
        return False, f"Не удалось проверить (сеть или таймаут): {exc}"
    if resp.status_code == 200:
        return True, "Groq подтвердил: ключ действителен."
    return False, f"Groq вернул код {resp.status_code}."


def _live_check_cohere_key(value: str) -> Tuple[bool, str]:
    try:
        resp = _http_get(
            "https://api.cohere.com/v1/models",
            {"Authorization": f"Bearer {value}"},
        )
    except Exception as exc:
        return False, f"Не удалось проверить (сеть или таймаут): {exc}"
    if resp.status_code == 200:
        return True, "Cohere подтвердил: ключ действителен."
    return False, f"Cohere вернул код {resp.status_code}."


def _live_check_apify_token(value: str) -> Tuple[bool, str]:
    try:
        resp = _http_get(f"https://api.apify.com/v2/users/me?token={value}")
    except Exception as exc:
        return False, f"Не удалось проверить (сеть или таймаут): {exc}"
    if resp.status_code == 200:
        return True, "Apify подтвердил: токен действителен."
    return False, f"Apify вернул код {resp.status_code}."


# ---------------------------------------------------------------------------
# KeySpec — one .env key our plugins care about
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    name: str
    metaphor: str
    format_hint: str
    validate: Callable[[str], bool]
    live_check: Optional[Callable[[str], Tuple[bool, str]]] = None


KEY_SPECS: Dict[str, KeySpec] = {
    "CLOUDFLARE_ACCOUNT_ID": KeySpec(
        name="CLOUDFLARE_ACCOUNT_ID",
        metaphor=(
            "Cloudflare — переводчик текста в числа-смыслы (эмбеддинги), у него "
            "бесплатного лимита с запасом. Это ID вашего аккаунта Cloudflare."
        ),
        format_hint="32 символа, только цифры и буквы a-f (шестнадцатеричный ID)",
        validate=_validate_cloudflare_account_id,
        live_check=None,
    ),
    "CLOUDFLARE_API_TOKEN": KeySpec(
        name="CLOUDFLARE_API_TOKEN",
        metaphor=(
            "Второй ключ от того же Cloudflare — токен доступа к API "
            "(переводчику текста в числа-смыслы)."
        ),
        format_hint="непустая строка без пробелов",
        validate=_validate_generic_nonempty,
        live_check=_live_check_cloudflare_api_token,
    ),
    "COHERE_API_KEY": KeySpec(
        name="COHERE_API_KEY",
        metaphor="Cohere — строгий редактор: из найденного отбирает самое нужное наверх.",
        format_hint="непустая строка без пробелов",
        validate=_validate_generic_nonempty,
        live_check=_live_check_cohere_key,
    ),
    "GEMINI_API_KEY": KeySpec(
        name="GEMINI_API_KEY",
        metaphor="Gemini — младший редактор, разруливает спорные факты.",
        format_hint="начинается с AIza…",
        validate=_validate_gemini_key,
        live_check=_live_check_gemini_key,
    ),
    "SCRAPECREATORS_API_KEY": KeySpec(
        name="SCRAPECREATORS_API_KEY",
        metaphor="ScrapeCreators — достаёт субтитры и списки видео с YouTube.",
        format_hint="непустая строка без пробелов",
        validate=_validate_generic_nonempty,
        live_check=None,
    ),
    "APIFY_TOKEN": KeySpec(
        name="APIFY_TOKEN",
        metaphor="Apify — запасной путь: скачивает аудио, если субтитров нет.",
        format_hint="начинается с apify_…",
        validate=_validate_apify_token,
        live_check=_live_check_apify_token,
    ),
    "GROQ_API_KEY": KeySpec(
        name="GROQ_API_KEY",
        metaphor="Groq — быстро расшифровывает аудио в текст (Whisper).",
        format_hint="начинается с gsk_…",
        validate=_validate_groq_key,
        live_check=_live_check_groq_key,
    ),
}


# Distinctive-format keys, used to detect "this looks like a DIFFERENT key"
# (checked in this order — first match wins). Keys with only a generic
# non-empty/no-whitespace rule (COHERE_API_KEY, CLOUDFLARE_API_TOKEN,
# SCRAPECREATORS_API_KEY) are deliberately excluded: their format is
# indistinguishable from "any random token", so they can never be the
# target of a cross-key mismatch detection, only the ones below can.
_DISTINCTIVE_CHECKS: List[Tuple[str, Callable[[str], bool]]] = [
    ("CLOUDFLARE_ACCOUNT_ID", _validate_cloudflare_account_id),
    ("GEMINI_API_KEY", _validate_gemini_key),
    ("GROQ_API_KEY", _validate_groq_key),
    ("APIFY_TOKEN", _validate_apify_token),
]


def classify(value: str) -> Optional[str]:
    """Return the KEY_SPECS name *value* looks like, based on distinctive
    formats only (hex32 / AIza… / gsk_… / apify_…), or None if it matches
    none of them."""
    v = (value or "").strip()
    for key_name, check in _DISTINCTIVE_CHECKS:
        if check(v):
            return key_name
    return None


def validate_key(key_name: str, value: str) -> Tuple[bool, str]:
    """Validate *value* as a candidate for *key_name*.

    Returns ``(True, "")`` on success, or ``(False, <RU explanation>)`` on
    failure — including a polite "this looks like key X" note when the
    value matches a different key's distinctive format.
    """
    v = (value or "").strip()
    if not v:
        return False, "Пустое значение. Пришлите ключ ещё раз или напишите «пропустить»."

    looks_like = classify(v)
    if looks_like is not None and looks_like != key_name:
        return False, (
            f"Это похоже на {looks_like}, а не на {key_name}. "
            f"Пришлите, пожалуйста, именно {key_name} (или «пропустить»)."
        )

    spec = KEY_SPECS.get(key_name)
    if spec is None:
        ok = _validate_generic_nonempty(v)
        return ok, ("" if ok else "Значение не должно содержать пробелов. Пришлите ещё раз.")

    if not spec.validate(v):
        return False, (
            f"Не похоже на {key_name} (ожидается: {spec.format_hint}). "
            f"Пришлите ещё раз или напишите «пропустить»."
        )
    return True, ""


def live_check_key(key_name: str, value: str) -> Tuple[bool, str]:
    """Run the one-shot live check for *key_name*, if one is registered.

    Returns ``(False, "<why not available>")`` when no live check exists for
    this key, rather than raising — callers should treat that as "nothing to
    report", not an error.
    """
    spec = KEY_SPECS.get(key_name)
    if spec is None or spec.live_check is None:
        return False, "Для этого ключа нет живой проверки."
    try:
        return spec.live_check(value)
    except Exception as exc:  # live_check impls already guard themselves; belt & suspenders
        return False, f"Проверка не удалась: {exc}"


# ---------------------------------------------------------------------------
# PluginSpec — one of OUR plugins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginSpec:
    slug: str  # folder name under ~/.hermes/plugins/<slug>/
    title: str
    description: str
    is_memory_provider: bool = False  # memohood: activated via memory.provider, not plugins.enabled
    keys: List[str] = field(default_factory=list)  # ordered KEY_SPECS names


# Order matters: this is the default "настроить всё по порядку" sequence.
PLUGINS: Dict[str, PluginSpec] = {
    "token-guard": PluginSpec(
        slug="token-guard",
        title="token-guard",
        description=(
            "token-guard — счётчик расходов: следит, сколько токенов и денег "
            "уходит на каждый запрос. Ключи не нужны — только включение."
        ),
        keys=[],
    ),
    "memobase": PluginSpec(
        slug="memobase",
        title="MemoBase",
        description=(
            "MemoBase — личная библиотека: загружаете документы и видео, "
            "плагин находит нужный фрагмент и отвечает с цитатой."
        ),
        keys=[
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_API_TOKEN",
            "COHERE_API_KEY",
            "GEMINI_API_KEY",
            "SCRAPECREATORS_API_KEY",
            "APIFY_TOKEN",
            "GROQ_API_KEY",
        ],
    ),
    "memohood": PluginSpec(
        slug="memohood",
        title="MemoHood (память)",
        description=(
            "MemoHood — личный дневник агента: сам запоминает важное из разговора "
            "и сам подсказывает это в следующий раз."
        ),
        is_memory_provider=True,
        keys=[
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_API_TOKEN",
            "COHERE_API_KEY",
            "GEMINI_API_KEY",
        ],
    ),
}

# Stable order for "настроить всё по порядку" and for numbering the menu.
PLUGIN_ORDER: List[str] = ["token-guard", "memobase", "memohood"]


# ---------------------------------------------------------------------------
# Filesystem scan — pure, no hermes_cli import (registry.py stays
# hermes-independent; config.yaml/memory.provider reads happen in wizard.py
# via hermes_cli.config, per API_CONTRACT_PLUGINS.md §2).
# ---------------------------------------------------------------------------


def discover_plugin_dirs(hermes_home: Path) -> Dict[str, bool]:
    """Return ``{slug: folder_exists}`` for every known plugin under
    ``<hermes_home>/plugins/<slug>/``. Never raises — a missing/unreadable
    ``plugins`` dir just means every slug maps to False."""
    result: Dict[str, bool] = {}
    plugins_dir = Path(hermes_home) / "plugins"
    for slug in PLUGIN_ORDER:
        try:
            result[slug] = (plugins_dir / slug).is_dir()
        except Exception:
            result[slug] = False
    return result
