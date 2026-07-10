#Requires -Version 5.1
<#
.SYNOPSIS
    Installs MemoBase's Python dependencies INTO the hermes-agent venv.

.DESCRIPTION
    General hermes plugins have NO lazy-install / pip_dependencies support
    (API_CONTRACT_PLUGINS.md §1) -- this script is the one-time step an
    operator runs after copying plugins/memobase/ into ~/.hermes/plugins/.

    Package list (DESIGN_v1.md "install script" section) -- all MIT/BSD/
    Apache, no torch, no pymupdf (AGPL), no docling:
        sqlite-vec pdfplumber pypdf mammoth "trafilatura>=1.8" ftfy
        py3langid PyStemmer requests

    ``requests`` is already a hermes-core dependency in practice, but is
    listed explicitly so this script is self-sufficient even against a bare
    venv.

.NOTES
    Never installs into the REPO's own dev venv used for this project's test
    suite (see tests/README section in the operator checklist) -- always
    targets the resolved HERMES venv python.
#>

[CmdletBinding()]
param(
    # Explicit override -- pass the full path to the hermes venv's python.exe
    # if auto-detection below can't find it (e.g. non-standard install).
    [string]$PythonPath,

    # ALSO install the local embedder (fastembed, ONNX, no PyTorch) and
    # pre-download multilingual-e5-large (~2.2 GB), so no CLOUDFLARE_* keys
    # are needed for embeddings.
    [switch]$Local
)

$ErrorActionPreference = "Stop"

function Resolve-HermesVenvPython {
    param([string]$Override)

    if ($Override) {
        if (Test-Path $Override) { return (Resolve-Path $Override).Path }
        throw "Указанный -PythonPath не существует: $Override"
    }

    if ($env:HERMES_VENV_PYTHON -and (Test-Path $env:HERMES_VENV_PYTHON)) {
        return (Resolve-Path $env:HERMES_VENV_PYTHON).Path
    }

    # `hermes` on PATH is normally a shim/exe inside the venv's Scripts/ dir --
    # its sibling python.exe is exactly the interpreter every plugin runs under.
    $hermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($hermesCmd) {
        $scriptsDir = Split-Path -Parent $hermesCmd.Source
        $candidate = Join-Path $scriptsDir "python.exe"
        if (Test-Path $candidate) { return $candidate }
    }

    # Fall back to the conventional HERMES_HOME/hermes-agent/venv layout.
    $hermesHome = $env:HERMES_HOME
    if (-not $hermesHome) { $hermesHome = Join-Path $env:LOCALAPPDATA "hermes" }
    $candidate = Join-Path $hermesHome "hermes-agent\venv\Scripts\python.exe"
    if (Test-Path $candidate) { return $candidate }

    throw (
        "Не удалось найти python интерпретатор hermes-agent venv автоматически. " +
        "Укажите его явно: .\install.ps1 -PythonPath 'C:\path\to\hermes-agent\venv\Scripts\python.exe' " +
        "или задайте переменную окружения HERMES_VENV_PYTHON."
    )
}

$python = Resolve-HermesVenvPython -Override $PythonPath
Write-Host "MemoBase: устанавливаю зависимости в $python" -ForegroundColor Cyan

$packages = @(
    "sqlite-vec",
    "pdfplumber",
    "pypdf",
    "mammoth",
    "trafilatura>=1.8",
    "ftfy",
    "py3langid",
    "PyStemmer",
    "requests"
)

& $python -m pip install --upgrade @packages
if ($LASTEXITCODE -ne 0) {
    Write-Host "MemoBase: установка зависимостей не удалась (см. вывод pip выше)." -ForegroundColor Red
    exit $LASTEXITCODE
}

if ($Local) {
    Write-Host ""
    Write-Host "MemoBase: ставлю локальный эмбеддер (fastembed - ONNX Runtime, без PyTorch)..." -ForegroundColor Cyan
    & $python -m pip install --upgrade fastembed
    if ($LASTEXITCODE -ne 0) {
        Write-Host "MemoBase: установка fastembed не удалась (см. вывод pip выше)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "MemoBase: скачиваю модель intfloat/multilingual-e5-large (~2.2 ГБ, один раз)..." -ForegroundColor Cyan
    & $python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='intfloat/multilingual-e5-large'); print('  локальная модель готова к работе')"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "MemoBase: не удалось скачать локальную модель (см. вывод выше)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "MemoBase: локальный режим установлен. Включите его в config.yaml (memobase.*):" -ForegroundColor Green
    Write-Host "    memobase:"
    Write-Host "      embedder: { provider: local, model: intfloat/multilingual-e5-large, dims: 1024 }"
    Write-Host "  (для памяти MemoHood - те же ключи под memory.memohood.embedder)"
}

Write-Host ""
Write-Host "MemoBase: зависимости установлены успешно." -ForegroundColor Green
Write-Host "Дальше:"
Write-Host "  1. Убедитесь, что memobase скопирован в <HERMES_HOME>\plugins\memobase\"
Write-Host "  2. Включите плагин: hermes plugins enable memobase  (или добавьте 'memobase' в plugins.enabled в config.yaml)"
Write-Host "  3. Перезапустите hermes"
Write-Host "  4. Проверьте: /memobase status"
