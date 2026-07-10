"""Tests for envfile.py: upsert_env_value (replace/uncomment/append),
mask_key, has_active_value, scan_keys.

These don't need HERMES_HOME isolation (envfile.py takes an explicit
``env_path`` argument, it never reads HERMES_HOME itself) -- plain
``tmp_path`` is enough. Still routed through the ``setup_plugin`` fixture so
every test uses the exact same freshly-imported module the real loader
would hand back, per this suite's convention.
"""

from __future__ import annotations


def test_mask_key_first_four_chars_plus_ellipsis(setup_plugin):
    envfile = setup_plugin.envfile
    assert envfile.mask_key("AIzaSyD1234567890") == "AIza…"
    assert envfile.mask_key("gsk_abcdef") == "gsk_…"


def test_mask_key_empty_value_yields_empty(setup_plugin):
    envfile = setup_plugin.envfile
    assert envfile.mask_key("") == ""


def test_mask_key_never_exposes_full_short_value(setup_plugin):
    envfile = setup_plugin.envfile
    # Even a value shorter than 4 chars must not leak in full -- masked
    # output should still just be "<value>…", never the literal value alone.
    masked = envfile.mask_key("ab")
    assert masked == "ab…"
    assert masked != "ab"


def test_upsert_appends_when_key_absent(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    action = envfile.upsert_env_value(env_path, "GEMINI_API_KEY", "AIzaTest123")
    assert action == "appended"
    text = env_path.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=AIzaTest123" in text


def test_upsert_creates_missing_parent_dir(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / "nested" / "dir" / ".env"
    envfile.upsert_env_value(env_path, "GROQ_API_KEY", "gsk_abc")
    assert env_path.exists()


def test_upsert_replaces_active_value(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("GEMINI_API_KEY=old_value\nOTHER=1\n", encoding="utf-8")
    action = envfile.upsert_env_value(env_path, "GEMINI_API_KEY", "new_value")
    assert action == "replaced"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "GEMINI_API_KEY=new_value" in lines
    assert "OTHER=1" in lines
    # exactly one GEMINI_API_KEY line -- not appended as a duplicate
    assert sum(1 for l in lines if l.startswith("GEMINI_API_KEY=")) == 1


def test_upsert_uncomments_commented_placeholder(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("# GEMINI_API_KEY=\nOTHER=1\n", encoding="utf-8")
    action = envfile.upsert_env_value(env_path, "GEMINI_API_KEY", "filled_in")
    assert action == "uncommented"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "GEMINI_API_KEY=filled_in" in lines
    assert not any(l.strip().startswith("#") and "GEMINI_API_KEY" in l for l in lines)


def test_upsert_uncomments_no_space_after_hash(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("#GEMINI_API_KEY=\n", encoding="utf-8")
    action = envfile.upsert_env_value(env_path, "GEMINI_API_KEY", "value1")
    assert action == "uncommented"
    assert "GEMINI_API_KEY=value1" in env_path.read_text(encoding="utf-8")


def test_upsert_preserves_position_on_replace(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\nGEMINI_API_KEY=old\nB=2\n", encoding="utf-8")
    envfile.upsert_env_value(env_path, "GEMINI_API_KEY", "new")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["A=1", "GEMINI_API_KEY=new", "B=2"]


def test_upsert_appends_without_extra_blank_line(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n", encoding="utf-8")
    envfile.upsert_env_value(env_path, "B", "2")
    text = env_path.read_text(encoding="utf-8")
    assert text == "A=1\nB=2\n"


def test_upsert_on_missing_file_creates_it(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    assert not env_path.exists()
    envfile.upsert_env_value(env_path, "A", "1")
    assert env_path.read_text(encoding="utf-8") == "A=1\n"


def test_has_active_value_true_for_nonempty_uncommented(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n", encoding="utf-8")
    assert envfile.has_active_value(env_path, "A") is True


def test_has_active_value_false_for_commented(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("# A=1\n", encoding="utf-8")
    assert envfile.has_active_value(env_path, "A") is False


def test_has_active_value_false_for_empty_value(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("A=\n", encoding="utf-8")
    assert envfile.has_active_value(env_path, "A") is False


def test_has_active_value_false_for_missing_file(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    assert envfile.has_active_value(tmp_path / "nope.env", "A") is False


def test_scan_keys_bulk(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n# B=2\nC=\n", encoding="utf-8")
    result = envfile.scan_keys(env_path, ["A", "B", "C", "D"])
    assert result == {"A": True, "B": False, "C": False, "D": False}


def test_scan_keys_missing_file_all_false(setup_plugin, tmp_path):
    envfile = setup_plugin.envfile
    result = envfile.scan_keys(tmp_path / "nope.env", ["A", "B"])
    assert result == {"A": False, "B": False}
