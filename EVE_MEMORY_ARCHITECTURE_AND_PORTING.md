# Архитектура памяти EVE и перенос в другой Hermes

status: draft
last_updated: 2026-07-06
audience: сопровождающие Hermes/EVE, операторы, будущие агенты Codex/OpenCode

## Назначение

Этот документ описывает, как устроена память в EVE, и что нужно сделать, чтобы воспроизвести тот же механизм в другом Hermes/Eve-Agent.

Документ написан как переносимый runbook: читатель должен уметь определить runtime-root, подключить provider `eve`, создать и проверить SQLite-хранилища, понять путь recall/writeback и диагностировать типовые поломки без скрытого контекста из чата.

## Короткий Вывод

Память EVE состоит не из одного файла и не из одной БД.

В текущей реализации есть четыре слоя:

1. `state.db` — канон сессий и сообщений.
2. `memory.db` — companion DB для FTS-индекса, captures, tags, links, signals и archive refs.
3. `MEMORY.md` / `USER.md` — legacy snapshot-память, если включена в config.
4. `sessions.system_prompt` в `state.db` — cached prompt конкретной сессии, который может продолжать нести старую память после изменения файлов/config.

Ключевое правило: прошлый контекст не записывается в system prompt на каждом ходе. Recall делается перед turn, заворачивается в `<memory-injection>` и добавляется только к текущему API user message. Исходная история сообщений в `state.db` при этом не мутируется.

## Референсная Реализация

Текущая проверенная реализация:

- Код: `I:\EVE`
- Windows runtime root: `C:\Users\mcFax\AppData\Local\eve`
- Config: `C:\Users\mcFax\AppData\Local\eve\config.yaml`
- Канон сессий: `C:\Users\mcFax\AppData\Local\eve\state.db`
- Companion memory DB: `C:\Users\mcFax\AppData\Local\eve\memory.db`
- Legacy memory files: `C:\Users\mcFax\AppData\Local\eve\memories\MEMORY.md`, `USER.md`

Проверенный live-config на 2026-07-06:

```yaml
memory:
  gate:
    backend: model2vec
    confidence_threshold: 0.5
    model_path: I:\EVE\models\gate_model2vec.joblib
    tech_pass: true
  memory_char_limit: 2200
  memory_enabled: true
  provider: eve
  user_char_limit: 1375
  user_profile_enabled: true
```

Live-состояние БД на 2026-07-06:

- `state.db`: 372 сессии, 26 587 сообщений, 26 587 строк в `messages_fts`.
- `memory.db`: 17 083 строки в `messages_fts`, 578 captures, 212 tagged sessions, 112 session links, 308 signals, 0 persistent embeddings.
- `memory.db._meta`: `schema_version=2`, `last_indexed_message_id=35052`.

Эти числа не являются инвариантами. На новой установке они будут другими.

## Карта Потока Данных

```text
Пользовательский turn
  |
  v
MemoryGate
  |
  | pass / skip
  v
EveMemoryProvider.prefetch(query)
  |
  v
memory.db: FTS/captures/signals/tags/links
  |
  | при необходимости msg:N достаётся из state.db
  v
<memory-injection> добавляется к текущему API user message

После ответа:
  |
  v
sync_turn()
  |
  v
signals -> session_tags -> session_links -> captures
```

## Runtime Root

Runtime-root выбирается через `eve_constants.get_eve_home()`.

На Windows дефолт:

```text
%LOCALAPPDATA%\eve
```

На текущей машине:

```text
C:\Users\mcFax\AppData\Local\eve
```

На Linux/macOS обычно:

```text
~/.eve
```

Правила переноса:

- Не хардкодить `~/.eve`.
- В provider использовать `eve_home`, который передаётся в `MemoryProvider.initialize()`.
- Для профилей, cron и gateway учитывать context-local override, если он есть.
- Не смешивать runtime roots EVE и старого Hermes без явной миграции.

Релевантные файлы:

- `eve_constants.py`
- `agent/agent_init.py`

## `state.db`: Канон Сессий

`state.db` — главный источник истины для сессий и сообщений.

Основные таблицы:

```text
sessions
messages
messages_fts
messages_fts_trigram
state_meta
compression_locks
```

Важные поля `sessions`:

```text
id
source
user_id
model
model_config
system_prompt
parent_session_id
started_at
ended_at
title
cwd
archived
```

Важные поля `messages`:

```text
id
session_id
role
content
timestamp
```

Практический смысл:

- Полный текст сообщений живёт в `state.db.messages`.
- State-level FTS живёт в `state.db.messages_fts`.
- Cached prompt живёт в `state.db.sessions.system_prompt`.
- Если memory prefetch отдаёт `msg:12345`, полный текст нужно доставать из `state.db`, а не из `memory.db`.

Релевантные файлы:

- `eve_state.py`
- `eve_state.py:SessionDB`
- `eve_state.py:search_messages()`
- `eve_state.py:search_sessions()`

## `memory.db`: Companion Memory DB

`memory.db` создаётся рядом с `state.db` в активном `eve_home`.

Назначение:

- быстрый memory-specific FTS;
- durable captures;
- session tags;
- typed links между сессиями;
- autopilot signals;
- optional embeddings;
- archive refs для больших external artifacts.

Это не замена `state.db`. Это companion-слой поверх канонической истории.

Релевантные файлы:

- `memory_engine.py`
- `memory_engine_prefetch.py`
- `agent/memory_eve.py`

Основная схема:

```sql
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content,
  session_id UNINDEXED,
  role UNINDEXED,
  timestamp UNINDEXED,
  tokenize='unicode61'
);

CREATE TABLE session_tags (
  session_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  source TEXT DEFAULT 'auto',
  created_at REAL NOT NULL,
  PRIMARY KEY (session_id, tag)
);

CREATE TABLE session_links (
  from_session_id TEXT NOT NULL,
  to_session_id TEXT NOT NULL,
  relationship TEXT NOT NULL DEFAULT 'related',
  label TEXT DEFAULT '',
  weight REAL DEFAULT 1.0,
  created_at REAL NOT NULL,
  PRIMARY KEY (from_session_id, to_session_id, relationship)
);

CREATE TABLE message_embeddings (
  message_id INTEGER NOT NULL,
  embedding BLOB,
  model TEXT NOT NULL DEFAULT 'unknown',
  dim INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  PRIMARY KEY (message_id)
);

CREATE TABLE captures (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  tags TEXT DEFAULT '[]',
  source TEXT DEFAULT '',
  session_id TEXT,
  message_id INTEGER,
  importance REAL DEFAULT 0.0,
  kind TEXT DEFAULT 'note',
  status TEXT DEFAULT 'active',
  supersedes TEXT DEFAULT '',
  confidence REAL DEFAULT 0.5,
  last_seen_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL
);

CREATE VIRTUAL TABLE captures_fts USING fts5(
  content,
  capture_id UNINDEXED,
  kind UNINDEXED,
  status UNINDEXED,
  created_at UNINDEXED,
  updated_at UNINDEXED,
  tokenize='unicode61'
);

CREATE TABLE signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  signal_type TEXT,
  score REAL DEFAULT 0.0,
  content TEXT DEFAULT '',
  message_id INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE _meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
```

Возможные archive-таблицы:

```text
context_items
context_chunks
context_chunks_porter
context_chunks_trigram
```

Инварианты:

- `captures` — канон durable notes внутри `memory.db`.
- `captures_fts` — rebuildable index.
- `messages_fts` в `memory.db` — индекс по сообщениям, но не канон текста.
- `message_embeddings` может существовать пустой; в текущем live-состоянии persistent embeddings не заполнены.

## Legacy `MEMORY.md` И `USER.md`

EVE всё ещё поддерживает legacy memory snapshots:

```text
memories\MEMORY.md
memories\USER.md
```

Они включаются через:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
```

При включении `agent_init` создаёт `MemoryStore`, грузит файлы с диска, а `system_prompt.py` включает их в volatile tier system prompt.

Варианты для другого Hermes:

1. Оставить legacy snapshots включёнными для совместимости.
2. Отключить их и сделать `memory.provider: eve` единственным durable memory source.
3. Оставить `USER.md`, но отключить `MEMORY.md`, если профиль пользователя нужен в prompt, а facts должны идти через DB captures.

Риск:

Legacy snapshot попадает в cached system prompt. Если он устарел, модель может продолжать следовать старым правилам даже при исправном `memory.db`.

## Cached `sessions.system_prompt`

System prompt строится один раз на сессию и кешируется.

Слои prompt:

1. `stable` — identity, protocol, tool guidance, skills, environment, platform.
2. `context` — explicit system message и найденные context files.
3. `volatile` — legacy memory, USER profile, external memory provider block, timestamp/session/model/provider line.

Cached prompt хранится в:

```text
state.db:sessions.system_prompt
```

Операционный вывод:

- Изменение `config.yaml` не гарантирует, что старая сессия сразу увидит новую память.
- Изменение `MEMORY.md` / `USER.md` не гарантирует, что active/resumed сессия перестанет видеть старый snapshot.
- При recovery нужно проверять SQLite, а не только файлы.

Минимальная проверка:

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Users\mcFax\AppData\Local\eve'); c=sqlite3.connect(root/'state.db'); print(c.execute('select id, source, started_at, ended_at, length(system_prompt) from sessions order by started_at desc limit 10').fetchall())"
```

## Кодовая Карта

| Область | Файлы | Роль |
|---|---|---|
| Runtime root | `eve_constants.py` | Выбор `eve_home`. |
| Session DB | `eve_state.py` | `state.db`, sessions/messages/FTS. |
| Provider ABI | `agent/memory_provider.py` | Абстрактный контракт memory provider. |
| Orchestration | `agent/memory_manager.py` | Регистрация provider, gate, merge context, routing tools/hooks. |
| Recall gate | `agent/memory_gate.py` | Model2Vec/pass-through gate. |
| EVE provider | `agent/memory_eve.py` | DB-backed provider implementation. |
| Companion DB | `memory_engine.py` | Schema, indexing, search, captures, fetch. |
| Prefetch helpers | `memory_engine_prefetch.py` | Text/token helpers. |
| PostRecall | `agent/post_recall/*` | Semantic rerank, dedup, MMR, cluster, format. |
| Prompt | `agent/system_prompt.py`, `agent/prompt_builder.py` | Stable/context/volatile tiers. |
| Turn loop | `agent/conversation_loop.py` | Prefetch, injection, post-turn sync. |
| Compression | `agent/conversation_compression.py` | Pre-compression provider hook. |
| Legacy bridge | `agent/agent_runtime_helpers.py` | `memory add/replace` -> provider `on_memory_write`. |
| Plugin discovery | `plugins/memory/__init__.py` | Загрузка provider из `memory.provider`. |
| EVE plugin descriptor | `plugins/memory/eve/plugin.yaml` | Имя provider и hooks. |
| CLI | `eve_cli/subcommands/memory.py` | Настройка memory provider. |

## Контракт Memory Provider

Базовый интерфейс: `agent.memory_provider.MemoryProvider`.

Обязательные/ключевые методы:

```python
name
is_available()
initialize(session_id, **kwargs)
system_prompt_block()
prefetch(query, session_id="")
queue_prefetch(query, session_id="")
sync_turn(user_content, assistant_content, session_id="", messages=None)
get_tool_schemas()
handle_tool_call(tool_name, args, **kwargs)
shutdown()
```

Optional hooks:

```python
on_turn_start(turn_number, message, **kwargs)
on_session_end(messages)
on_session_switch(new_session_id, **kwargs)
on_pre_compress(messages)
on_memory_write(action, target, content, metadata=None)
on_delegation(task, result, child_session_id="", **kwargs)
```

Правило переноса:

Нельзя перенести только `memory_engine.py`. Нужно подключить полный lifecycle: init, prompt, prefetch, tool routing, sync, compression, shutdown.

## Инициализация

На старте агента:

1. `agent_init.init_agent()` читает `config.yaml`.
2. Если включены `memory_enabled` или `user_profile_enabled`, создаёт `MemoryStore`.
3. `MemoryStore` грузит `MEMORY.md` / `USER.md`.
4. Читается `memory.provider`.
5. Если provider непустой, создаётся `MemoryManager(gate_config=memory)`.
6. `MemoryManager` создаёт `MemoryGate`.
7. `plugins.memory.load_memory_provider(name)` загружает provider.
8. Provider регистрируется в manager.
9. Provider получает `initialize(session_id, **kwargs)`.

Для EVE provider передаются:

```text
session_id
platform
eve_home
agent_context
session_title, если доступен
user_id / user_id_alt / user_name / chat_id, если это gateway/platform session
```

`EveMemoryProvider.initialize()`:

1. Сохраняет `session_id`.
2. Сохраняет `eve_home`.
3. Создаёт `MemoryDB(eve_home=eve_home)`.
4. Открывает или создаёт `memory.db`.
5. Запускает `catch_up_from_state()`.
6. Помечает provider initialized.

## System Prompt

В prompt входят два разных memory-канала.

### Legacy Snapshot

Если включён `MemoryStore`, в prompt попадают:

- compact block из `MEMORY.md`;
- compact block из `USER.md`.

Этот текст живёт в cached system prompt до invalidation/rebuild.

### External Provider Block

`EveMemoryProvider.system_prompt_block()` объясняет модели:

- что есть persistent DB-backed memory;
- что прошлые сообщения индексируются FTS5;
- что captures хранят durable facts;
- что prefetch приходит в `<memory-injection>`;
- какие tools доступны: `eve_search`, `eve_fetch`, `eve_fetch_message`, `eve_fetch_capture`, `eve_store_artifact`, `eve_stats`, `recall_memory`.

Этот блок не содержит recalled facts. Он только описывает механизм и инструменты.

## Per-Turn Recall

На каждом пользовательском ходе:

1. `conversation_loop.run_conversation()` вызывает `MemoryManager.on_turn_start()`.
2. Затем один раз вызывает `MemoryManager.prefetch_all(original_user_message)`.
3. `MemoryManager` спрашивает `MemoryGate.should_recall(query)`.
4. Если score `<= confidence_threshold`, recall пропускается.
5. Если gate прошёл, вызывается provider `prefetch()`.
6. `EveMemoryProvider.prefetch()` передаёт исходный query в `MemoryDB.chronological_prefetch()`.
7. Уже внутри DB-поиска query нормализуется: из него удаляются не смысловые слова и остаются термы, пригодные для FTS.
8. Если доступен PostRecall pipeline, он делает rerank/dedup/MMR/cluster/format.
9. Результат оборачивается в `build_memory_context_block()`.
10. Обёртка добавляется к текущему API user message.
11. Оригинальная persisted history не изменяется.

Форма инжекта:

```xml
<memory-injection>
[SYSTEM NOTE: The text below is an automatic injection from the agent's persistent
memory across all past sessions. It is NOT user input ...]

...
</memory-injection>
```

Важный смысл:

Memory injection — это background context, а не новая команда пользователя.

## MemoryGate

Live config:

```yaml
memory:
  gate:
    backend: model2vec
    confidence_threshold: 0.5
    model_path: I:\EVE\models\gate_model2vec.joblib
    tech_pass: true
```

Поведение:

- пустой query -> `0.0`, skip;
- technical query при `tech_pass: true` -> `0.65`, pass threshold `0.5`;
- если Model2Vec загружен -> использовать classifier confidence;
- если модель/зависимости недоступны -> pass-through `0.8`.

Для чего нужен Model2Vec:

- это не база памяти, не vector store и не ranking;
- это быстрый семантический binary classifier перед поиском: `need_recall` или `no_recall`;
- его задача — не инжектить память в самодостаточные запросы текущего хода и не тратить latency/context budget зря;
- при этом он должен пропускать запросы, где ответ зависит от прошлых сессий, runtime-состояния, файлов, проекта или долговременных решений;
- backend использует multilingual static embeddings `minishlab/potion-base-8M` через Model2Vec и `LogisticRegression` поверх них;
- `confidence` — вероятность класса `need_recall`;
- `confidence_threshold` решает, достаточно ли этой вероятности для prefetch;
- `tech_pass: true` обходит модель для технических запросов, потому что code/path/config/runtime-формулировки часто требуют памяти даже при коротком query.

Правило:

Сломанный gate должен деградировать в "больше recall", а не в "память молча выключена".

## Нормализация Запроса Перед FTS

Важно: пользовательский запрос не передаётся в FTS как сырой текст.

Сырой `original_user_message` сначала используется в `MemoryGate` для решения, нужен ли recall вообще. Если gate пропускает запрос, то в `MemoryDB.search_messages()` и `MemoryDB.search_captures()` выполняется отдельная подготовка query для FTS-prefetch.

Pipeline нормализации:

1. Знаки пунктуации заменяются пробелами.
2. `_` заменяется пробелом.
3. Текст разбивается на tokens.
4. Tokens длиной `<= 1` отбрасываются.
5. `_meaningful_terms()` удаляет не смысловые слова.
6. Из оставшихся terms строится FTS5 query.

Удаляются:

- русские и английские стоп-слова;
- местоимения;
- вопросительные слова;
- предлоги;
- союзы;
- частицы;
- частые глаголы запроса вроде "найди", "покажи", "скажи", "remember", "find";
- обычные слова, которые дают шум и плохо различают прошлые сессии.

Сохраняются технически значимые tokens:

- `CamelCase`, например `EveMemoryProvider`;
- `UPPER_SNAKE` и uppercase names, например `EVE`, `FTS5`;
- tokens с цифрами, например `v2`, `model2vec`, `session123`;
- dotted names, пути, file-like names и расширения;
- длинные редкие термы, которые не являются stop words.

Если после первой фильтрации survived terms больше 4, движок делает второй, более строгий pass и оставляет прежде всего technical terms. Это нужно, чтобы длинный естественный запрос не утопил главный ключ вроде `MemoryGate`, `state.db`, `EveMemoryProvider` или `session-native`.

FTS-поведение:

- для `messages_fts` длинные terms (`len > 5`) превращаются в prefix search: term обрезается на 2 символа и получает `*`;
- для `captures_fts` terms длиной `>= 3` получают `*`;
- сначала пробуется `AND`;
- если длинный message query ничего не нашёл, выбираются самые редкие terms и делается fallback;
- затем используется `OR` fallback;
- default roles для message search: `user`, `assistant`, `memory_write`, `delegation_result`;
- raw tool dumps по умолчанию не являются нормальной recall-поверхностью.

Пример:

```text
Raw query:
А помнишь, как мы чинили EVE memory provider в session-native архитектуре?

Meaningful/technical terms, концептуально:
EVE, memory, provider, session, native, архитектуре

FTS terms, концептуально:
EVE memory provider session native архитекту*
```

Вывод для переноса:

При воспроизведении EVE-памяти в другом Hermes нельзя заменить этот слой простым `WHERE text MATCH raw_user_query`. Нужно перенести или эквивалентно повторить `_meaningful_terms()` и fallback-логику, иначе prefetch станет шумным, хрупким к пунктуации и хуже будет находить технические воспоминания.

## Retrieval И Ranking

`MemoryDB.chronological_prefetch()`:

1. Принимает raw query от provider.
2. Нормализует query в meaningful/technical FTS terms.
3. Ищет сообщения через `memory.db.messages_fts`.
4. Ищет captures через `captures_fts`.
5. Убирает exact/near duplicates.
6. Сортирует messages по дню, session id, timestamp.
7. Добавляет captures по importance и recency.
8. Укладывает результат в token budget.

`EveMemoryProvider.prefetch()`:

1. Запрашивает широкий candidate set.
2. Считает adaptive output budget.
3. Пробует PostRecall pipeline:
   - semantic rerank;
   - dedup;
   - MMR;
   - cluster;
   - format.
4. Если pipeline недоступен, использует chronological fallback.

В live logs PostRecall использовал:

```text
BAAI/bge-small-en-v1.5
dim=384
```

Важно:

Таблица `message_embeddings` существует, но в проверенном live-состоянии была пустой. Текущий рабочий путь — FTS + semantic reranker, а не полноценная persistent vector DB.

## Stable References

Prefetch/search может вернуть ссылки:

```text
msg:<id>
cap:<id>
ctx:<id>
```

Смысл:

- `msg:<id>` — полный original message в `state.db.messages`.
- `cap:<id>` — durable capture в `memory.db.captures`.
- `ctx:<id>` — archived external artifact в context archive tables.

Методы:

```python
MemoryDB.fetch_ref(ref_id, before=0, after=0, max_chars=4000)
MemoryDB.fetch_message_context(message_id, before=2, after=2)
MemoryDB.fetch_capture(capture_id)
```

Инструменты модели:

```text
eve_fetch
eve_fetch_message
eve_fetch_capture
```

Инвариант переноса:

Если snippets показывают `msg:N` или `cap:ID`, другой агент должен иметь возможность получить полный источник через fetch.

## Writeback После Turn

После успешного ответа:

1. `conversation_loop` вызывает `_sync_external_memory_for_turn()`.
2. Provider получает user content, assistant content, session id и messages.
3. `sync_turn()` ищет durable signals.
4. Signals пишутся в `memory.db.signals`.
5. Session получает tags.
6. На первом turn может создаваться session link.
7. Если score выше threshold, создаётся capture.

Константы reference implementation:

```python
DEFAULT_AUTO_CAPTURE = True
AUTOPILOT_CAPTURE_THRESHOLD = 4.0
SIGNAL_CORRECTION = 5.0
SIGNAL_DECISION = 4.0
SIGNAL_PREFERENCE = 4.0
```

Пользовательские signal hints:

```text
correction: не так, wrong, incorrect, нет,, actually
preference: я хочу, предпочитаю, не надо, запомни, always, never
decision: делаем, будем использовать, начинаем с, go with, choose
url: http://...
config/path: C:\...\*.py, *.yaml, *.json, *.md, *.env
```

Assistant signal hints:

```text
remember:
note:
important:
key insight
key takeaway
root cause
запомни:
важно:
ключевой инсайт
ключевой вывод
корневая причина
исправлено
```

Следствие live threshold `4.0`:

- одна correction создаёт capture;
- одна decision создаёт capture;
- одна preference создаёт capture;
- weak/noisy text обычно только индексируется как обычная session history.

## Pre-Compression Hook

Перед сжатием контекста:

1. `conversation_compression.compress_context()` вызывает `MemoryManager.on_pre_compress(messages)`.
2. `EveMemoryProvider.on_pre_compress()` сканирует сообщения.
3. При достаточном score создаёт `pre_compress` capture.
4. Может вернуть compressed insights для compressor prompt.

Зачем это нужно:

Если long conversation будет сжат, важные preferences/decisions/corrections должны попасть в durable memory до потери подробной истории.

## Инструменты Provider

`EveMemoryProvider.get_tool_schemas()` экспонирует:

| Tool | Назначение |
|---|---|
| `eve_stats` | Статистика provider и gate report. |
| `eve_store_artifact` | Архивирует большой external artifact, возвращает `ctx:<id>`. |
| `eve_search` | Unified search по messages и captures. |
| `eve_fetch` | Fetch для `msg:`, `cap:`, `ctx:`. |
| `eve_fetch_message` | Полное сообщение + соседние сообщения той же сессии. |
| `eve_fetch_capture` | Полный capture by id. |
| `recall_memory` | Emergency recall, если auto-injection отсутствует. |

Legacy:

- `memory(add/replace/remove)` остаётся через `tools.memory_tool`.
- `memory add/replace` уведомляет active provider через `on_memory_write()`.

Проверка при переносе:

Provider может успешно стартовать, но быть бесполезным, если его tools не попали в valid tool schemas модели.

## Maintenance

В live logs есть daily cron:

```text
memory-db-maintenance
```

Рекомендуемые maintenance tasks:

- catch-up `memory.db.messages_fts` из `state.db`;
- rebuild `captures_fts` после schema changes;
- health/stats checks;
- FTS optimize/vacuum при росте DB;
- backup перед repair.

Минимальная read-only проверка:

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Users\mcFax\AppData\Local\eve'); c=sqlite3.connect(root/'memory.db'); print('captures', c.execute('select count(*) from captures').fetchone()[0]); print('messages_fts', c.execute('select count(*) from messages_fts').fetchone()[0]); print('meta', c.execute('select key,value from _meta order by key').fetchall())"
```

## Перенос В Другой Hermes

### Предпосылки

Целевой Hermes должен иметь:

- Python runtime;
- session DB с таблицами sessions/messages или совместимым аналогом;
- conversation loop;
- tool schema registration;
- system prompt builder;
- желательно context compression hook;
- желательно gateway/session identity plumbing.

Если в целевом Hermes нет `state.db`, сначала переносится session persistence, потом memory provider.

### Шаг 1. Определить Runtime Root

Windows:

```text
%LOCALAPPDATA%\hermes
```

или для EVE fork:

```text
%LOCALAPPDATA%\eve
```

Linux/macOS:

```text
~/.hermes
```

или:

```text
~/.eve
```

В runtime root должны быть:

```text
config.yaml
state.db
memory.db
logs\
memories\        # если legacy включён
```

### Шаг 2. Перенести Модули

Минимальный набор:

```text
agent/memory_provider.py
agent/memory_manager.py
agent/memory_gate.py
agent/memory_eve.py
agent/post_recall/
memory_engine.py
memory_engine_prefetch.py
plugins/memory/__init__.py
plugins/memory/eve/__init__.py
plugins/memory/eve/plugin.yaml
```

Точки интеграции:

```text
agent/agent_init.py
agent/system_prompt.py
agent/conversation_loop.py
agent/conversation_compression.py
agent/agent_runtime_helpers.py
eve_state.py или hermes_state.py
eve_constants.py или hermes_constants.py
```

Если имена файлов в целевом Hermes другие, сохранять нужно поведение, а не буквальные пути.

### Шаг 3. Добавить Config

Минимальный pass-through вариант:

```yaml
memory:
  provider: eve
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
  gate:
    backend: pass
    confidence_threshold: 0.5
    tech_pass: true
```

Вариант с Model2Vec:

```yaml
memory:
  provider: eve
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
  gate:
    backend: model2vec
    model_path: C:\path\to\gate_model2vec.joblib
    confidence_threshold: 0.5
    tech_pass: true
```

Если модели gate нет, начинать с `backend: pass`.

`backend: pass` полезен для первого запуска: он проверяет provider, DB, FTS и prompt injection без риска, что classifier отрежет recall. `backend: model2vec` включать после этого, когда есть файл модели и понятно, что `need_recall/no_recall` gate работает. Даже при Model2Vec FTS-поиск всё равно использует отдельную очистку query через meaningful/technical terms.

### Шаг 4. Создать `memory.db`

Первый старт provider должен автоматически создать `memory.db`.

Ожидаемые таблицы:

```text
_meta
messages_fts
session_tags
session_links
message_embeddings
captures
captures_fts
signals
```

Если включён archive layer:

```text
context_items
context_chunks
context_chunks_porter
context_chunks_trigram
```

### Шаг 5. Catch-Up Из `state.db`

При init EVE вызывает:

```python
MemoryDB.catch_up_from_state()
```

Проверка:

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Path\To\HermesRuntime'); m=sqlite3.connect(root/'memory.db'); s=sqlite3.connect(root/'state.db'); print('memory messages_fts', m.execute('select count(*) from messages_fts').fetchone()[0]); print('state messages', s.execute('select count(*) from messages').fetchone()[0]); print('meta', m.execute('select key,value from _meta order by key').fetchall())"
```

Ноль в `memory.messages_fts` после успешного старта — ошибка.

Разные counts между `state.db.messages` и `memory.db.messages_fts` не всегда ошибка: могли быть pruned/deleted rows, watermark по id, архивные фильтры или восстановление из разных backup.

### Шаг 6. Подключить System Prompt

Prompt builder должен:

1. загрузить legacy `MEMORY.md` / `USER.md`, если включены;
2. вызвать `agent._memory_manager.build_system_prompt()`;
3. включить provider block в volatile tier;
4. кешировать prompt per session;
5. иметь invalidation path после compression/config/memory changes.

Запрещено:

- класть per-turn recall snippets в cached system prompt.

### Шаг 7. Подключить Per-Turn Injection

Conversation loop должен:

1. вызвать `on_turn_start()`;
2. вызвать `prefetch_all(original_user_message)` один раз до tool loop;
3. завернуть результат в `build_memory_context_block()`;
4. добавить wrapper только к текущему API user message;
5. не мутировать persisted `messages`.

### Шаг 8. Подключить Post-Turn Sync

После ответа:

```python
agent._sync_external_memory_for_turn(
    original_user_message=original_user_message,
    final_response=final_response,
    interrupted=interrupted,
    messages=messages,
)
```

Если turn interrupted/error, лучше не писать partial transcript в durable memory.

### Шаг 9. Подключить Tool Routing

Нужно:

1. собрать provider tool schemas;
2. добавить их в valid model tools;
3. маршрутизировать tool calls в `MemoryManager.handle_tool_call()`;
4. вернуть результат модели.

Smoke-test tools:

```text
eve_stats
eve_search
eve_fetch
recall_memory
```

### Шаг 10. Подключить Compression Hook

Перед сжатием:

```python
agent._memory_manager.on_pre_compress(messages)
```

Если compressor поддерживает extra context, вернуть insights в compression prompt.

### Шаг 11. Подключить Shutdown И Session Switch

На clean shutdown:

```python
agent._memory_manager.on_session_end(messages)
agent._memory_manager.shutdown_all()
```

При смене session id:

```python
agent._memory_manager.on_session_switch(new_session_id, ...)
```

Иначе captures/links могут писаться под старый session id.

## Проверка Свежей Установки

### 1. Config

Ожидается:

```text
memory.provider = eve
memory.gate.backend = pass или model2vec
```

### 2. Startup Logs

Ожидаемые строки:

```text
MemoryDB ready at <runtime>\memory.db
EVE Memory v4 initialized (session=<id>, db=<runtime>\memory.db)
```

Если используется gate model:

```text
Memory gate model loaded: <path>
```

### 3. Schema

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Path\To\Runtime'); c=sqlite3.connect(root/'memory.db'); print([r[0] for r in c.execute(\"select name from sqlite_master where type='table' order by name\")])"
```

Должны быть:

```text
_meta
captures
captures_fts
message_embeddings
messages_fts
session_links
session_tags
signals
```

### 4. Recall Gate

Задать технический вопрос, который явно зависит от прошлого контекста.

Ожидается:

- gate passes;
- появляются prefetch/pipeline logs;
- при наличии совпадений модель получает `<memory-injection>`.

### 5. Manual Recall

Попросить модель вызвать:

```text
recall_memory(query="<старая тема>", limit=5)
```

Ожидается:

- chronological snippets;
- или честное `No relevant past context found.`

### 6. Fetch

Для найденного `msg:N`:

```text
eve_fetch(ref_id="msg:N", before=1, after=1)
```

Ожидается:

- target message;
- before/after context;
- session id.

### 7. Capture

Отправить явный durable signal:

```text
Запомни: для теста памяти Hermes использует provider eve.
```

После turn:

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Path\To\Runtime'); c=sqlite3.connect(root/'memory.db'); print('captures', c.execute('select count(*) from captures').fetchone()[0]); print('signals', c.execute('select signal_type, score from signals order by id desc limit 5').fetchall())"
```

Ожидается:

- появился signal;
- capture count вырос, если threshold достигнут.

### 8. Prompt Cache

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Path\To\Runtime'); c=sqlite3.connect(root/'state.db'); print(c.execute('select id, source, length(system_prompt) from sessions order by started_at desc limit 10').fetchall())"
```

Ожидается:

- новые сессии могут иметь `NULL`, пока prompt ещё не построен;
- active/resumed сессии могут иметь non-null prompt;
- после изменения config старый prompt нужно invalidated/rebuilt явно.

## Миграция Из Старой Memory Wiki

Старая схема:

```text
.md files -> отдельный FTS/index -> prompt/context
```

Целевая EVE-схема:

```text
state.db messages -> memory.db messages_fts -> prefetch
durable facts -> memory.db captures/captures_fts
session relations -> memory.db session_tags/session_links
```

Рекомендуемые фазы:

1. Включить `memory.provider: eve` с пустым `memory.db`.
2. Проиндексировать существующий `state.db`.
3. Оставить legacy `MEMORY.md` / `USER.md` на один переходный период.
4. Перенести durable facts из wiki в `captures`, а не в prompt-only text.
5. Проверить recall quality.
6. Отключить legacy snapshots только после ручного подтверждения, что важные facts не потеряны.

Не импортировать:

- secrets;
- raw OAuth URLs;
- cookies;
- private keys;
- raw chat logs без фильтрации.

## Backup И Restore

Минимальный backup set:

```text
config.yaml
state.db
state.db-wal
state.db-shm
memory.db
memory.db-wal
memory.db-shm
memories\MEMORY.md
memories\USER.md
logs\agent.log
logs\gateway.log
```

Для live SQLite безопаснее остановить Hermes/gateway или использовать SQLite online backup.

Restore порядок:

1. Остановить Hermes/gateway.
2. Восстановить `config.yaml`.
3. Восстановить `state.db` вместе с WAL/SHM, если backup снят live.
4. Восстановить `memory.db` вместе с WAL/SHM, если backup снят live.
5. Восстановить legacy memory files, если используются.
6. Запустить Hermes.
7. Проверить provider init logs.
8. Проверить latest `sessions.system_prompt`.

## Troubleshooting

### Memory Не Инжектится

Проверить:

- `memory.provider` задан;
- provider initialized в logs;
- gate не отсекает query как `no_recall`;
- Model2Vec model path существует, если включён `backend: model2vec`;
- `tech_pass` включён для технических workflow, где короткие path/code/config queries должны проходить;
- `memory.db.messages_fts` не пустой;
- после `_meaningful_terms()` в query остаются terms для FTS;
- текущий runtime не обходит normal conversation loop.

Команда:

```powershell
python -c "import sqlite3, pathlib; root=pathlib.Path(r'C:\Path\To\Runtime'); c=sqlite3.connect(root/'memory.db'); print(c.execute('select count(*) from messages_fts').fetchone()[0]); print(c.execute('select count(*) from captures').fetchone()[0])"
```

### Модель Видит Старую Память

Вероятная причина:

```text
state.db:sessions.system_prompt
```

Проверить prompt lengths и при необходимости стартовать новую сессию, invalidate prompt или после backup очистить affected `sessions.system_prompt`.

### `memory.db` Меньше `state.db`

Возможные причины:

- watermark `last_indexed_message_id` ушёл дальше deleted/pruned rows;
- `state.db` заменили после построения `memory.db`;
- `memory.db` восстановлен из старого backup;
- catch-up пропустил malformed rows;
- фильтры/архивные сессии.

Проверить:

- `_meta.last_indexed_message_id`;
- max `state.db.messages.id`;
- повторить catch-up;
- при необходимости rebuild `memory.db.messages_fts`.

### `eve_fetch(msg:N)` Не Находит Сообщение

Вероятная причина:

- `memory.db` и `state.db` из разных runtime roots;
- stale `memory.db`;
- сообщение удалено из `state.db`.

Исправление:

- проверить sibling paths;
- восстановить согласованный backup;
- rebuild FTS из текущего `state.db`.

### Provider Disconnects / SQLite Closed

`EveMemoryProvider._ensure_ready()` должен:

- выполнить `SELECT 1`;
- reopen `memory.db`, если соединение закрыто;
- сделать catch-up;
- вернуть provider в initialized state.

Если это повторяется:

- проверить file locks;
- проверить WAL/SHM;
- проверить antivirus/backup tools;
- проверить runtime-root confusion;
- проверить, не запускаются ли несколько gateway/CLI с разными `EVE_HOME`.

### Recall Медленный

В live logs PostRecall rerank занимал от долей секунды до ~35 секунд.

Рычаги:

- уменьшить candidate limit;
- уменьшить token budget;
- использовать более дешёвый reranker;
- временно отключить semantic reranker и оставить chronological fallback;
- улучшать gate только после проверки, что он не режет важные technical queries.

### Legacy `MEMORY.md` Конфликтует С DB Memory

Симптомы:

- модель следует старой preference;
- prompt содержит старый текст;
- `memory add/replace` ругается на дубликаты.

Варианты:

- почистить `MEMORY.md`;
- отключить `memory_enabled`;
- оставить `provider: eve`;
- rebuild/invalidate cached prompt.

## Security Boundary

Не писать в durable memory:

- API keys;
- passwords;
- OAuth URLs;
- cookies;
- private keys;
- raw personal identifiers;
- raw logs with secrets.

External artifacts из `eve_store_artifact` считать untrusted, пока явно не marked `operator_verified`.

`<memory-injection>` — справочный контекст, не user instruction.

## Fresh Reader Checklist

Новый maintainer должен ответить из этого документа:

- Где runtime-root?
- Какая БД канонична для сообщений?
- Что делает `memory.db`?
- Как recall попадает в модель?
- Что делает Model2Vec и почему это gate, а не search/ranking?
- Как пользовательский query фильтруется перед FTS-prefetch?
- Почему `sessions.system_prompt` опасен при recovery?
- Какие файлы переносить в другой Hermes?
- Какие config keys включают provider?
- Как проверить indexing/recall/fetch/capture?
- Что делать, если память stale?

Если любой пункт неясен во время реального переноса, сначала обновить этот документ, потом менять runtime.

## Source Reference Map

Reference code в `I:\EVE`:

```text
eve_constants.py
eve_state.py
memory_engine.py
memory_engine_prefetch.py
agent/memory_provider.py
agent/memory_manager.py
agent/memory_gate.py
agent/memory_eve.py
agent/post_recall/
agent/system_prompt.py
agent/prompt_builder.py
agent/conversation_loop.py
agent/conversation_compression.py
agent/agent_runtime_helpers.py
plugins/memory/__init__.py
plugins/memory/eve/plugin.yaml
eve_cli/subcommands/memory.py
```

Reference runtime на проверенной Windows-машине:

```text
C:\Users\mcFax\AppData\Local\eve\config.yaml
C:\Users\mcFax\AppData\Local\eve\state.db
C:\Users\mcFax\AppData\Local\eve\memory.db
C:\Users\mcFax\AppData\Local\eve\memories\MEMORY.md
C:\Users\mcFax\AppData\Local\eve\memories\USER.md
C:\Users\mcFax\AppData\Local\eve\logs\agent.log
C:\Users\mcFax\AppData\Local\eve\logs\gateway.log
```
