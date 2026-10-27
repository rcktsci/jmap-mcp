# Бриф: фаза 1 — каркас jmap-mcp (вертикальный срез whoami)

Дата: 2026-10-27. Роль: **разработчик**. Репо: `rcktsci/jmap-mcp`, корень = рабочий каталог.

## Что это

MCP-сервер: **stateless credential-transparent proxy** «MCP → JMAP». Клиент (opencode) шлёт
`Authorization: Bearer <Stalwart API Key>`; сервер проксирует этот заголовок в JMAP-вызовы к Stalwart
и **ничего не хранит** (ни в БД, ни на диске, ни между запросами). Стек: Python 3.14 + `mcp` SDK 2.x
(`MCPServer`) + `httpx`. **Без БД, без UI, без health, без ORM, без миграций.** Сессий MCP нет:
stateless Streamable HTTP.

## Файлы для чтения (обязательно)

| Файл | Что берём |
|---|---|
| `docs/superpowers/specs/2026-10-27-jmap-mcp-design.md` | §3 стек, §5 безопасность, §6 JMAP-детали, §8 структура, §9 зависимости, §10 конфиг, §11 критерии |
| `docs/tools-schema.md` | §1.2 контракт входа, §1.5 ошибки (ToolError), §1.6 риски/фолбэки, §1.7 живая проверка; схема тула `whoami` (input/output) |
| `docs/brief-tools-schema.md` | соглашения по схемам (snake_case, `$defs`, enum и т.п.) |

## Фаза 1 = каркас + ОДИН тул `whoami` (вертикальный срез)

Остальные 14 тулов — фаза 2. Не делать их сейчас.

### Обязательно создать

```
main.py                      # единственная точка входа: uvicorn + Bearer-middleware + MCP-приложение
requirements.txt             # runtime: mcp, httpx, pyyaml, structlog — и всё
requirements-dev.txt         # pytest, pytest-asyncio, jsonschema
config.yml.example           # пример конфига (без секретов)
.env.example
pytest.ini
.gitignore
Dockerfile
Dockerfile.tests
docker-compose.yml
.github/workflows/ci.yml     # по образцу соседних проектов: push в ghcr.io/rcktsci/jmap-mcp:<YYYY-MM-DD>
src/__init__.py
src/config/__init__.py
src/config/settings.py       # env + config.yml → dataclass/pydantic-модель; секретов нет
src/mcp/__init__.py
src/mcp/auth.py              # BearerAuthMiddleware: pass-through, ContextVar
src/mcp/jmap.py              # JMAP-клиент: session, call, ошибки методов, blob download/upload
src/mcp/server.py            # MCPServer + регистрация тула whoami
src/mcp/tools.py             # бизнес-логика (пока только whoami)
tests/conftest.py
tests/test_auth.py
tests/test_jmap_client.py
tests/test_whoami.py
```

### Требования к коду

1. **auth.py** — ASGI-middleware (как в `telegram-mcp-gh`): парсит `Authorization: Bearer <token>`;
   нет заголовка/не Bearer → `401 {"detail": "unauthorized"}` **до** входа в MCP-приложение;
   при успехе токен кладётся в `ContextVar` и читается тулами. **Валидность ключа не проверяется** —
   это делает Stalwart (дизайн §5.2). Токен не логируется, не пишется в ошибки.
2. **jmap.py** — асинхронный `httpx`:
   - `session()`: `GET {base}/jmap/session` (fallback `/.well-known/jmap`) с `Authorization: Bearer <token>`;
     отдаёт `api_url`, `download_url`, `upload_url`, `account_id` (`primaryAccounts["urn:ietf:params:jmap:mail"]`),
     `capabilities`.
   - `call(methods)`: `POST {session.api_url}` с `{"using": [...], "methodCalls": [...]}`; `using` = пересечение
     `session.capabilities` с `urn:ietf:params:jmap:core|mail|submission`.
   - **Ошибки методов приходят внутри HTTP 200** (дизайн §6) — разбирать `responses[]` и поднимать ToolError
     с текстом JMAP-ошибки.
   - **Проверка апстрима** (дизайн §5.3): хост/схема/порт из session-документа сверяются с настроенным
     `stalwart.url`; не совпали → ошибка, запрос не уходит.
   - **Кэш сессии** (дизайн §5.2): в памяти, ключ `sha256(bearer)`, TTL 60 с; сам bearer в кэш не кладём.
     Отключается конфигом.
   - blob: `download` по шаблону `downloadUrl` и `upload` (multipart) по `uploadUrl/{accountId}`.
3. **server.py** — `MCPServer("jmap-mcp")`; регистрация тула `whoami` по схеме из `docs/tools-schema.md`
   (input/output, описания, формат ответа — как в спеке; **не** придумывать другие поля).
   `whoami` = session + `Identity/get`; возвращает `account_id`, `email`, `identities`, `capabilities`,
   `state` (nullable, `str | None`).
4. **Политика ошибок** (дизайн §7, `tools-schema.md` §1.5): ожидаемые ошибки → **исключение тула**
   (SDK превращает в `isError: true` с actionable-текстом), не поле `error` в успешном ответе.
   Непрогнозируемые исключения — тоже в ToolError, без стектрейса в тексте ответа и без токена.
5. **Транспорт**: `mcp_server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")`,
   обёрнутое в Bearer-middleware; uvicorn; порт из конфига (по умолчанию 8000), `host` по умолчанию `127.0.0.1`.
   `host="0.0.0.0"` в вызове `streamable_http_app` — обязателен (иначе DNS-rebinding protection режет
   запросы по IP с 421; см. дизайн §8 «Замечания по транспорту»).
6. **Конфиг** (`config.yml` + env): `stalwart.url`, `stalwart.session_path`, `request_timeout_sec`,
   `max_attachment_mb`, `max_page_size`, `max_body_chars`, `allow_roots`, `download_dir`, `session_cache_ttl_sec`,
   `listen`/`port`/`log_level`. Секретов нет ни в одном виде. Env перекрывает yaml.
7. **Логи** — `structlog`, как в `telegram-mcp-gh`. Никогда не логировать заголовок `Authorization`
   и сам bearer; для корреляции — только `sha256(bearer)[:8]`.

### Тесты (pytest)

- `test_auth.py`: без заголовка → 401; не-Bearer → 401; с корректным Bearer → 200 и токен в ContextVar.
- `test_jmap_client.py`: мок JMAP-сервера (`httpx.MockTransport`): разбор session; батчинг methodCalls;
  per-method ошибка внутри HTTP 200 → ToolError; несовпадение хоста апстрима → ошибка без обращения наружу;
  кэш сессии по ключу.
- `test_whoami.py`: сборка ответа `whoami` против мока (включая `state: null` и `identities`).
- Тесты не ходят в сеть.

## Гейты приёмки (все пройти)

1. `python -m pytest -q` — зелёное, без предупреждений о пропущенных тестах.
2. `python main.py` поднимается; `curl` с `Authorization: Bearer test` даёт `initialize` + `tools/list`
   (200) с **ровно 1** тулом `whoami`; без заголовка — 401.
3. `whoami` против мок-сервера возвращает поля схемы (`account_id`, `email`, `identities`,
   `capabilities`, `state`), `state` = null, если сервер его не отдал.
4. `grep` по коду: нет `sqlalchemy|alembic|psycopg|postgres|fastapi|jinja|health`, нет `Authorization`/`token`
   в строке логирования, нет записи bearer на диск.
5. `requirements.txt` — только `mcp`, `httpx`, `pyyaml`, `structlog` (+ версии). БД-зависимостей нет.
6. `git status --porcelain -uall` — только новые файлы фазы 1, лишнего нет.
7. Ничего не закоммичено (коммит делает оркестратор).

## Границы

- **Только `whoami`.** Остальные 14 тулов — фаза 2. Не реализовывать.
- Не добавлять health, UI, БД, OAuth, мультиаккаунт, админ-тулы.
- Не менять `docs/tools-schema.md` и `tools/tools_schema.py` (источник правды по схемам) — если схема мешает
  реализации, записать в отчёт, не править молча.
- Не коммитить и не пушить.
- Режим работы: короткие циклы, сразу проба, без длинных рассуждений. Если инструмент не работает — не обходить
  его сырыми вызовами и не добывать токены, а сказать об этом.

## Отчёт мне (оркестратору)

3–6 строк: гейты 1–7 — каждый «пройден/не пройден»; точная команда запуска тестов и её вывод;
число созданных файлов; что осталось на фазу 2; риски.
