# CLAUDE.md — MemoHood + MemoBase

## Что это
Набор плагинов к стороннему AI-агенту **hermes-agent** (Python), оформленных как штатные плагины **без единой правки ядра**. Два ключевых улучшения — долговременная память диалога и приватная база знаний по документам — плюс мастер настройки и учёт токенов.

Витрина репо — [`README.md`](README.md) (EN) / [`README.ru.md`](README.ru.md) (RU). Лицензия — MIT ([`LICENSE`](LICENSE)).

## Карта
- `plugins/hermes-kb/` → плагин **MemoBase**: локальная база знаний (RAG по PDF/YouTube/Obsidian/аудио, FTS5 + вектор + RRF), ответ строго по источникам с цитатой или честным отказом
- `plugins/memohood/` → плагин **MemoHood**: диалоговая память (авто-recall перед ходом, авто-извлечение фактов, SUPERSEDE вместо затирания), memory-provider
- `plugins/hermes-setup/` → мастер настройки прочих плагинов из чата/CLI (`/setup`), пишет ключи в `.env`
- `plugins/token-guard/` → учёт токенов/стоимости (SQLite-леджер), детект сброса кэша, опц. тумблеры экономии
- `HERMES_UPGRADES.md` → главный proposal: обоснование и архитектура всех 4 плагинов
- `EVE_MEMORY_ARCHITECTURE_AND_PORTING.md` → референс архитектуры памяти (источник порта MemoHood)
- `API_CONTRACT_PLUGINS.md` → верифицированный контракт API загрузчика плагинов hermes (истина для `plugin.yaml`)
- `docs/PLUGINS.md` → детальная карта плагинов (level 2)
- `docs/MINDMAPS.md` → две mermaid-майнд-карты архитектуры (MemoBase / MemoHood)
- `pytest.ini` → корневой конфиг pytest (`--import-mode=importlib` ради пакета token-guard)

## Команды
У каждого плагина своя `tests/`:
- `pytest plugins/token-guard` — тесты одного плагина (аналогично hermes-kb / memohood / hermes-setup)

Тестам нужен установленный хост-пакет `hermes-agent` (checkout добавляется в `sys.path` через `conftest.py`; путь переопределяется env-переменной `HERMES_AGENT_CHECKOUT`).

## Правила
- **Плагины не патчат ядро hermes** — только легальные точки расширения (`register_tool`/`register_hook`/`register_command`/`register_cli_command`/`register_memory_provider`).
- **Хуки синхронные:** `async def`-коллбэк тихо не сработает.
- **Секреты** — только в `.env` (имена переменных — в `plugin.yaml.requires_env`); в код/доки писать имена, не значения.
- Контракт с ядром сверять по `API_CONTRACT_PLUGINS.md` при каждом апдейте hermes.

## Детали (level 2)
Плагины (модули, пайплайны, discovery, контракт API) — [`docs/PLUGINS.md`](docs/PLUGINS.md).
