"""Tests for registry.py: validate_key per key format, wrong-key-type
classify()/cross-detection, live_check_key with registry._http_get
monkeypatched (never a real network call), and discover_plugin_dirs.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


def test_classify_cloudflare_account_id(setup_plugin):
    registry = setup_plugin.registry
    assert registry.classify("a" * 32) == "CLOUDFLARE_ACCOUNT_ID"
    assert registry.classify("0123456789abcdef0123456789ABCDEF") == "CLOUDFLARE_ACCOUNT_ID"


def test_classify_gemini(setup_plugin):
    registry = setup_plugin.registry
    assert registry.classify("AIza" + "x" * 30) == "GEMINI_API_KEY"


def test_classify_groq(setup_plugin):
    registry = setup_plugin.registry
    assert registry.classify("gsk_abcdefghij") == "GROQ_API_KEY"


def test_classify_apify(setup_plugin):
    registry = setup_plugin.registry
    assert registry.classify("apify_abcdefghij") == "APIFY_TOKEN"


def test_classify_no_match_returns_none(setup_plugin):
    registry = setup_plugin.registry
    assert registry.classify("just some random token") is None
    assert registry.classify("") is None


# ---------------------------------------------------------------------------
# validate_key -- one happy-path format check per key
# ---------------------------------------------------------------------------


def test_validate_cloudflare_account_id_ok(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    assert ok is True
    assert msg == ""


def test_validate_cloudflare_account_id_bad_length(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("CLOUDFLARE_ACCOUNT_ID", "a" * 10)
    assert ok is False
    assert "CLOUDFLARE_ACCOUNT_ID" in msg


def test_validate_gemini_ok(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GEMINI_API_KEY", "AIza" + "x" * 30)
    assert ok is True


def test_validate_gemini_bad_prefix(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GEMINI_API_KEY", "notAKey1234567890123456789012")
    assert ok is False
    assert "GEMINI_API_KEY" in msg


def test_validate_groq_ok(setup_plugin):
    registry = setup_plugin.registry
    ok, _ = registry.validate_key("GROQ_API_KEY", "gsk_abcdefghij")
    assert ok is True


def test_validate_groq_bad_prefix(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GROQ_API_KEY", "wrong_prefix_123")
    assert ok is False


def test_validate_apify_ok(setup_plugin):
    registry = setup_plugin.registry
    ok, _ = registry.validate_key("APIFY_TOKEN", "apify_abcdefghij")
    assert ok is True


def test_validate_apify_bad_prefix(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("APIFY_TOKEN", "not_apify_shaped")
    assert ok is False


def test_validate_cohere_generic_nonempty_ok(setup_plugin):
    registry = setup_plugin.registry
    ok, _ = registry.validate_key("COHERE_API_KEY", "some-token-value")
    assert ok is True


def test_validate_generic_rejects_whitespace(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("COHERE_API_KEY", "has a space")
    assert ok is False


def test_validate_empty_value_rejected(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GEMINI_API_KEY", "")
    assert ok is False
    assert "Пустое" in msg


def test_validate_unknown_key_name_falls_back_to_generic(setup_plugin):
    registry = setup_plugin.registry
    ok, _ = registry.validate_key("SOME_UNKNOWN_KEY", "no-spaces-value")
    assert ok is True
    ok2, msg2 = registry.validate_key("SOME_UNKNOWN_KEY", "has space")
    assert ok2 is False


# ---------------------------------------------------------------------------
# wrong-key-type detection: a distinctively-shaped value submitted for a
# DIFFERENT key name must be rejected with a "looks like X" message, not a
# generic format-mismatch message.
# ---------------------------------------------------------------------------


def test_validate_wrong_key_type_gemini_for_groq(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GROQ_API_KEY", "AIza" + "x" * 30)
    assert ok is False
    assert "GEMINI_API_KEY" in msg  # names what it actually looks like
    assert "GROQ_API_KEY" in msg  # and what was expected


def test_validate_wrong_key_type_apify_for_gemini(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GEMINI_API_KEY", "apify_abcdefghij")
    assert ok is False
    assert "APIFY_TOKEN" in msg


def test_validate_wrong_key_type_cloudflare_id_for_groq(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GROQ_API_KEY", "a" * 32)
    assert ok is False
    assert "CLOUDFLARE_ACCOUNT_ID" in msg


def test_validate_same_key_distinctive_format_not_flagged_as_wrong_type(setup_plugin):
    # A GEMINI-shaped value submitted for GEMINI_API_KEY itself must succeed,
    # not get treated as "looks like a different key".
    registry = setup_plugin.registry
    ok, msg = registry.validate_key("GEMINI_API_KEY", "AIza" + "y" * 30)
    assert ok is True
    assert msg == ""


# ---------------------------------------------------------------------------
# live_check_key -- registry._http_get monkeypatched, NEVER real network.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data


def test_live_check_key_no_check_registered(setup_plugin):
    registry = setup_plugin.registry
    ok, msg = registry.live_check_key("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    assert ok is False
    assert "нет живой проверки" in msg


def test_live_check_gemini_success(setup_plugin, monkeypatch):
    registry = setup_plugin.registry

    def fake_get(url, headers=None):
        assert "generativelanguage.googleapis.com" in url
        return _FakeResponse(200)

    monkeypatch.setattr(registry, "_http_get", fake_get)
    ok, msg = registry.live_check_key("GEMINI_API_KEY", "AIzaTestKey1234567890123456")
    assert ok is True
    assert "подтвердил" in msg


def test_live_check_gemini_failure_status(setup_plugin, monkeypatch):
    registry = setup_plugin.registry
    monkeypatch.setattr(registry, "_http_get", lambda url, headers=None: _FakeResponse(401))
    ok, msg = registry.live_check_key("GEMINI_API_KEY", "AIzaBadKey")
    assert ok is False
    assert "401" in msg


def test_live_check_groq_success(setup_plugin, monkeypatch):
    registry = setup_plugin.registry
    monkeypatch.setattr(registry, "_http_get", lambda url, headers=None: _FakeResponse(200))
    ok, msg = registry.live_check_key("GROQ_API_KEY", "gsk_test")
    assert ok is True


def test_live_check_cohere_success(setup_plugin, monkeypatch):
    registry = setup_plugin.registry
    monkeypatch.setattr(registry, "_http_get", lambda url, headers=None: _FakeResponse(200))
    ok, msg = registry.live_check_key("COHERE_API_KEY", "cohere-test-token")
    assert ok is True


def test_live_check_apify_success(setup_plugin, monkeypatch):
    registry = setup_plugin.registry
    monkeypatch.setattr(registry, "_http_get", lambda url, headers=None: _FakeResponse(200))
    ok, msg = registry.live_check_key("APIFY_TOKEN", "apify_test")
    assert ok is True


def test_live_check_cloudflare_api_token_success(setup_plugin, monkeypatch):
    registry = setup_plugin.registry
    monkeypatch.setattr(
        registry, "_http_get", lambda url, headers=None: _FakeResponse(200, {"success": True})
    )
    ok, msg = registry.live_check_key("CLOUDFLARE_API_TOKEN", "cf-test-token")
    assert ok is True


def test_live_check_cloudflare_api_token_success_false(setup_plugin, monkeypatch):
    registry = setup_plugin.registry
    monkeypatch.setattr(
        registry, "_http_get", lambda url, headers=None: _FakeResponse(200, {"success": False})
    )
    ok, msg = registry.live_check_key("CLOUDFLARE_API_TOKEN", "cf-test-token")
    assert ok is False


def test_live_check_network_exception_never_raises(setup_plugin, monkeypatch):
    registry = setup_plugin.registry

    def raising_get(url, headers=None):
        raise ConnectionError("boom")

    monkeypatch.setattr(registry, "_http_get", raising_get)
    ok, msg = registry.live_check_key("GEMINI_API_KEY", "AIzaTestKey1234567890123456")
    assert ok is False
    assert "таймаут" in msg or "boom" in msg


def test_http_get_sends_browser_user_agent(setup_plugin, monkeypatch):
    """Cloudflare-class WAFs 403/1010 a bare/no User-Agent -- every live
    check must send a realistic browser UA (module docstring's hard
    constraint)."""
    registry = setup_plugin.registry
    captured = {}

    class _FakeRequests:
        @staticmethod
        def get(url, headers=None, timeout=None):
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _FakeResponse(200)

    import sys

    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)
    resp = registry._http_get("https://example.com/api")
    assert resp.status_code == 200
    assert "User-Agent" in captured["headers"]
    assert "Mozilla" in captured["headers"]["User-Agent"]
    assert captured["timeout"] == registry.LIVE_CHECK_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# discover_plugin_dirs
# ---------------------------------------------------------------------------


def test_discover_plugin_dirs_all_missing(setup_plugin, tmp_path):
    registry = setup_plugin.registry
    result = registry.discover_plugin_dirs(tmp_path)
    assert result == {slug: False for slug in registry.PLUGIN_ORDER}


def test_discover_plugin_dirs_finds_existing_folder(setup_plugin, tmp_path):
    registry = setup_plugin.registry
    (tmp_path / "plugins" / "token-guard").mkdir(parents=True)
    result = registry.discover_plugin_dirs(tmp_path)
    assert result["token-guard"] is True
    assert result["memobase"] is False
    assert result["memohood"] is False


def test_discover_plugin_dirs_never_raises_on_bad_path(setup_plugin):
    registry = setup_plugin.registry
    # A path that can't sensibly be a directory root (e.g. a null byte would
    # raise on some platforms) -- discover_plugin_dirs must swallow errors
    # per its own docstring contract, not propagate them.
    result = registry.discover_plugin_dirs("\x00bad\x00path")
    assert set(result.keys()) == set(registry.PLUGIN_ORDER)
    assert all(v is False for v in result.values())
