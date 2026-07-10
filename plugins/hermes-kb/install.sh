#!/usr/bin/env bash
# Installs MemoBase's Python dependencies INTO the hermes-agent venv.
#
# General hermes plugins have NO lazy-install / pip_dependencies support
# (API_CONTRACT_PLUGINS.md §1) -- this script is the one-time step an
# operator runs after copying plugins/memobase/ into ~/.hermes/plugins/.
#
# Package list (DESIGN_v1.md "install script" section) -- all MIT/BSD/
# Apache, no torch, no pymupdf (AGPL), no docling:
#   sqlite-vec pdfplumber pypdf mammoth "trafilatura>=1.8" ftfy py3langid
#   PyStemmer requests
#
# Usage:
#   ./install.sh                       # auto-detect the hermes venv python
#   ./install.sh /path/to/venv/bin/python
#   HERMES_VENV_PYTHON=/path/to/python ./install.sh

set -euo pipefail

PYTHON_OVERRIDE="${1:-}"

resolve_hermes_venv_python() {
    if [ -n "$PYTHON_OVERRIDE" ]; then
        if [ -x "$PYTHON_OVERRIDE" ]; then
            echo "$PYTHON_OVERRIDE"
            return 0
        fi
        echo "Указанный путь к python не существует или не исполняемый: $PYTHON_OVERRIDE" >&2
        return 1
    fi

    if [ -n "${HERMES_VENV_PYTHON:-}" ] && [ -x "${HERMES_VENV_PYTHON}" ]; then
        echo "$HERMES_VENV_PYTHON"
        return 0
    fi

    # `hermes` on PATH is normally a shim inside the venv's bin/ dir -- its
    # sibling python is exactly the interpreter every plugin runs under.
    if command -v hermes >/dev/null 2>&1; then
        local hermes_bin
        hermes_bin="$(command -v hermes)"
        local scripts_dir
        scripts_dir="$(dirname "$hermes_bin")"
        if [ -x "$scripts_dir/python" ]; then
            echo "$scripts_dir/python"
            return 0
        fi
        if [ -x "$scripts_dir/python3" ]; then
            echo "$scripts_dir/python3"
            return 0
        fi
    fi

    # Fall back to the conventional HERMES_HOME/hermes-agent/venv layout.
    local hermes_home="${HERMES_HOME:-$HOME/.hermes}"
    local candidate="$hermes_home/hermes-agent/venv/bin/python"
    if [ -x "$candidate" ]; then
        echo "$candidate"
        return 0
    fi

    echo "Не удалось найти python интерпретатор hermes-agent venv автоматически." >&2
    echo "Укажите его явно: ./install.sh /path/to/hermes-agent/venv/bin/python" >&2
    echo "или задайте переменную окружения HERMES_VENV_PYTHON." >&2
    return 1
}

PYTHON="$(resolve_hermes_venv_python)"
echo "MemoBase: устанавливаю зависимости в $PYTHON"

"$PYTHON" -m pip install --upgrade \
    sqlite-vec \
    pdfplumber \
    pypdf \
    mammoth \
    "trafilatura>=1.8" \
    ftfy \
    py3langid \
    PyStemmer \
    requests

echo ""
echo "MemoBase: зависимости установлены успешно."
echo "Дальше:"
echo "  1. Убедитесь, что memobase скопирован в <HERMES_HOME>/plugins/memobase/"
echo "  2. Включите плагин: hermes plugins enable memobase  (или добавьте 'memobase' в plugins.enabled в config.yaml)"
echo "  3. Перезапустите hermes"
echo "  4. Проверьте: /memobase status"
