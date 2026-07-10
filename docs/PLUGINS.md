# Плагины hermes-agent

Четыре плагина для стороннего AI-агента **hermes-agent** — два улучшения (память + учёт токенов),
оформленные как штатные плагины **без единой правки ядра**. Спецификация и обоснование — в
[`HERMES_UPGRADES.md`](../HERMES_UPGRADES.md) (proposal v1). Все написаны на Python.

## plugins/hermes-kb — плагин «memobase»
Локальная база знаний по документам («библиотекарь», не память диалога — работает параллельно с memory-провайдером).
- **Что делает:** принимает PDF/DOCX/HTML/MD/TXT/CSV, YouTube (видео/каналы), аудио (STT), Obsidian-vault; гибридный поиск FTS5 + вектор; отвечает **строго по базе** с цитатами или честным отказом.
- **Структура:** `__init__.py` (`register(ctx)`, тонкая склейка) → `tools.py` (8+ MULTIUSER-тулов: `memobase_ingest/query/ask/list/delete/status/selfcheck/map` + admin `create_for/share/quarantine`), `commands.py` (`/memobase`), `cli.py` (`hermes memobase ...`), `wizard.py` (онбординг через хук `pre_gateway_dispatch`), `backup.py`. Пайплайн: `extract`→`chunk`→`stem`/`normalize`→`embed`→`enrich`→`db`; поиск `retrieve`+`rerank`; ответ `answer`; безопасность `security`; источники `youtube`/`stt`/`obsidian`.
- **Манифест:** `plugin.yaml`, `kind: standalone`. requires_env: `CLOUDFLARE_*`, `COHERE_API_KEY`, `SCRAPECREATORS_API_KEY`, `APIFY_TOKEN`, `GROQ_API_KEY`, `GEMINI_API_KEY`.
- **Зависимости:** stdlib + sqlite-vec/FTS5, сознательно без torch/pymupdf/docling (лицензии). Установка pip-зависимостей в venv самого hermes через `install.ps1`/`install.sh` (general-плагины не имеют lazy-install через манифест).
- **Заявленное покрытие:** 285 тестов.

## plugins/memohood — плагин «memohood»
Диалоговая память hermes — это **memory provider** (ABI `MemoryProvider`), не general-плагин.
- **Что делает:** авто-recall перед каждым ходом, авто-извлечение фактов (correction/decision/preference), гибридный поиск (FTS5 RU-стемминг + вектор Cloudflare BGE-M3 + RRF), pinned-факты, SUPERSEDE вместо затирания истории.
- **Структура:** `__init__.py` → `ctx.register_memory_provider(MemoHoodMemoryProvider())` из `provider.py` + `cli.py`. Логика: `capture`, `consolidate`, `extract_llm`, `gate` (Model2Vec-гейт), `graph_rerank`, `post_recall`, `query_norm`, `setup_wizard`, `tools` (`memohood_search/fetch/recall/stats/capture/recall_all`). Движок retrieve/embed/rerank **вендорен** в `_engine/` (копия из hermes-kb, без общей зависимости между плагинами).
- **Манифест:** `plugin.yaml`, `kind: exclusive`, `pip_dependencies: [sqlite-vec, PyStemmer, ftfy, requests, model2vec]` — **единственный** тип плагина, для которого хост реально читает `pip_dependencies` (лениво).
- **Нюанс discovery:** memory-провайдеры ищет отдельный сканер хоста `plugins/memory/__init__.py` по пути `$HERMES_HOME/plugins/<name>/` напрямую (без вложенного `memory/` для юзер-инсталляций) — иначе установка «молча» не находится.
- **Два бага буквального порта EVE** (исправлены): тег `<memory-context>` → должен быть `<memory-injection>`; kwarg `hermes_home` → `eve_home`. Референс — [`EVE_MEMORY_ARCHITECTURE_AND_PORTING.md`](../EVE_MEMORY_ARCHITECTURE_AND_PORTING.md).
- **Заявленное покрытие:** 180 тестов.

## plugins/hermes-setup
Мастер первичной настройки прочих плагинов (token-guard, MemoBase, MemoHood) прямо из чата (`/setup`) или CLI.
- **Что делает:** пошаговый онбординг с проверкой ключей перед записью в `.env`; поведение модели не меняет.
- **Структура:** `__init__.py` → `wizard.py` (вся логика, хук `pre_gateway_dispatch`), `envfile.py` (безопасная запись `.env` через `upsert_env_value`), `registry.py`. Манифест `kind: standalone`, `requires_env: []`, `provides_tools: []`, `provides_hooks: [pre_gateway_dispatch]`.
- **Зависимости:** чистый stdlib. **Покрытие:** 77 тестов.

## plugins/token-guard
Наблюдение за расходом токенов/стоимости и опциональные рычаги экономии.
- **Что делает:** SQLite-леджер per-request, детектор сброса промпт-кэша при смене модели/тулсета внутри сессии, аудит конфига. Часть 1 — только наблюдение; часть 2 — тумблеры (`cheap_aux`/`cheap_delegation`/`cron_cascade`/`context_editing`), по умолчанию выключены, требуют явного подтверждения.
- **Структура:** `__init__.py` (thin хук-коллбэки) → `ledger.py`, `cache_guard.py`, `audit.py`, `report.py`, `toggles.py`. Манифест `kind: standalone`, `provides_hooks: [post_api_request, api_request_error, post_tool_call]`.
- **Зависимости:** stdlib (тяжёлые импорты — внутри функций). **Покрытие:** 33/33 теста.
- `plugins/token-guard.zip` — упакованная копия директории целиком (снапшот для дистрибуции, включает `.pytest_cache`/`__pycache__`), не отдельный билд-артефакт.

## Как плагины подключаются к hermes
Контракт-истина — [`API_CONTRACT_PLUGINS.md`](../API_CONTRACT_PLUGINS.md) (сверен построчно с живой инсталляцией hermes-agent v0.18.0; сверять при каждом апдейте ядра).
- **Манифест** `plugins/<name>/plugin.yaml`; хост читает только: `name, version, description, author, requires_env` (informational), `provides_tools, provides_hooks, kind`. `pip_dependencies` поддержан **только** для загрузчика memory-provider.
- **Точка входа** — `__init__.py` c `register(ctx)`, грузится через importlib как `hermes_plugins.<slug>`.
- **Discovery:** bundled `plugins/` → `~/.hermes/plugins/` → `./.hermes/plugins/` → pip entry points. При коллизии имени побеждает последний (пользовательский перекрывает встроенный).
- **Gating:** standalone включаются через `plugins.enabled` в `config.yaml` (`plugins.disabled` всегда в приоритете).
- **PluginContext API:** `register_tool` (схема+хендлер, попадает в toolset `delegate_task`), `register_hook` (23 события, **синхронные** коллбэки — `async def` тихо не сработает), `register_command` (slash), `register_cli_command` (`hermes <name> ...`), `register_auxiliary_task` (своя дешёвая модель), `ctx.llm.complete()`/`complete_structured()`, `ctx.inject_message()`.
- **Жёсткое правило:** плагины **не патчат core** — только легальные точки расширения.
- Реально используемые хуки: `pre_gateway_dispatch` (hermes-kb, hermes-setup), `subagent_start` (hermes-kb), `post_api_request`/`api_request_error`/`post_tool_call` (token-guard).

## Тесты плагинов
`pytest`, у каждого плагина своя `tests/` с `conftest.py` (обычно monkeypatch `HERMES_HOME` в tmp). Корневой `pytest.ini` держит `rootdir` выше `plugins/token-guard/` с `--import-mode=importlib` (иначе конфликт с `plugins/token-guard/__init__.py` как реальным пакетом). Загрузка плагина в тестах — `importlib.util.spec_from_file_location` + регистрация namespace `hermes_plugins`.

```bash
pytest plugins/token-guard      # тесты одного плагина (аналогично hermes-kb / memohood / hermes-setup)
```

## Корневые доки
- `HERMES_UPGRADES.md` (~130KB) — главный proposal: обоснование обоих улучшений как плагинов, построчный разбор стока hermes, критика/фикс порта EVE, архитектура hermes-kb и token-guard, экономика токенов, порядок внедрения.
- `EVE_MEMORY_ARCHITECTURE_AND_PORTING.md` (~46KB) — референс-runbook по памяти исходного проекта EVE: 4 слоя памяти, потоки recall/writeback, контракт MemoryProvider, чек-листы переноса. Источник порта memohood.
- `API_CONTRACT_PLUGINS.md` (~10KB) — верифицированный контракт API загрузчика плагинов (сигнатуры PluginContext, валидные хуки, доступ к cost/usage). На него ссылаются все 4 `plugin.yaml`.
