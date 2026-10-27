# Бриф: фаза 4 — диалект JMAP: правка под Stalwart 1.0.0

Дата: 2026-10-27. Роль: **разработчик**. Репо: `rcktsci/jmap-mcp`, корень = рабочий каталог.

## Что это

MCP-сервер: stateless credential-transparent proxy «MCP → JMAP». 15 тулов, 148 тестов, мок-тесты зелёные.
Но против **живого Stalwart 1.0.0** (`https://mail.rocketscien.se`, key владельца) 8 расхождений:
наш код написан по RFC 8620/8621, а Stalwart 1.0.0 в нескольких местах отходит от RFC.
Отправка письма вживую работает только через ручной JMAP; наш `send_draft` отчитывался об успехе,
**не отправив ничего** (критично).

Задача фазы: починить код под реальный диалект Stalwart, не сломав RFC-совместимость, и закрыть
молчаливую ошибку отправки.

## Файлы для чтения

- `docs/tools-schema.md` — §1.5 (ошибки), §1.6 (риски/фолбэки), §1.7 (живая проверка). Источник правды по схемам.
- `src/mcp/jmap.py` — клиент: `session()`, `call()`, `_parse_session()`, `using()`, blob-методы, `_request()`.
- `src/mcp/tools.py` — тулы: фильтры (`_conditions`, `_combine`), `_query`, `_email_get`, `whoami`, `send_draft`,
  `move_emails`, `list_changes`.
- `src/config/settings.py` — конфиг.
- `tools/live_check.py` — зонд живой проверки (16 пунктов), уже в репо.

## Живые факты (проверено запросами к Stalwart 1.0.0 — это истина)

Сессия: `apiUrl=https://mail.rocketscien.se/jmap/`,
`downloadUrl=.../jmap/download/{accountId}/{blobId}/{name}?accept={type}`,
`uploadUrl=.../jmap/upload/{accountId}/`, `username=katren@tech.rocketscien.se`,
**account id = `j`**, `sessionState=cbb4eb6c`, **account state** (в ответах `Mailbox/get`/`Email/query`) — например `sau`.
Роли папок: `inbox`, `drafts`, `sent`, `trash`, `junk` (без `archive`).

| Что | RFC 8620/8621 | Stalwart 1.0.0 (живой факт) |
|---|---|---|
| `Email/query` filter: несколько условий | `{"operator":"and","conditions":[…]}` | **400 notRequest** — принимает **ровно одно** условие |
| `filter.inMailbox` | `Id[]` (массив) | **строка**: `{"inMailbox":"a"}`; массив → 400 |
| `filter.header` | `{"name":…, "values":[…]}` | **400** (не поддержан) |
| `filter.notKeyword/hasKeyword/hasAttachment/text/after/before` | — | работают по одному |
| `Email/query limit: 0` | допустимо | принимается (200) |
| `Email/set` update `mailboxIds` | `Id[]` | **map** `{"e": true}`; массив → 400 invalidProperties |
| `Email/set` update `keywords` | map | работает как есть |
| `EmailSubmission/set create` | `IdentityMap<EmailSubmission>` = `{emailId:{identityId,sendAt}}` | **`{emailId: {"emailId": emailId, "identityId": id}}`** — `emailId` дублируется внутри значения; вариант `{emailIds:[…],identityId:…}` → 400 invalidProperties |
| `EmailSubmission/set onSuccessUpdateEmail` | `EmailSet` c `update: {emailId:{mailboxIds:[sentId]}}` | **`{"update": {emailId: {"mailboxIds": {"e": true}}}}`** — map, без `accountId`; с массивом → notUpdated |
| `Identity/get` | `isDefault` есть | **нет `isDefault`** (есть `replyTo`) |
| `Email/changes` | `sinceState` = account state | отвергает sessionState («invalid JMAP State») |
| multipart загрузка | — | поле **`file`**, ответ `{accountId, blobId, type}` |
| `Email/get` properties/bodyProperties | — | все наши комбинации работают (200) |

Живой ответ об отправке при верной форме:
`{"created": {"<emailId>": {"id": "<submissionId>", "sendAt": "…", "undoStatus": "pending"}}}`
(это **id подачи**, не id письма). При неверной форме — `notCreated: {…: {"type":"invalidProperties", …}}`
внутри **HTTP 200** → наш код обязан это превращать в ошибку, а не в «успех».

## Что чинить (8 пунктов)

1. **B1 `inMailbox`** (`tools.py`, `_conditions`): массив → строка (диалект stalwart).
2. **B2 несколько условий** (`_combine`): на stalwart — запрос по одному условию, **пересечение id на клиенте**,
   порядок — по первому запросу, `total` = размер пересечения, страница — срез. На rfc — как сейчас.
3. **B3 `header`**: привести к RFC (`{"name":…, "values":[…]}`); на stalwart (400) — **клиентская фильтрация**
   по уже загруженным `from`/`to`/`subject`, с честно пересчитанными `total`/`has_more`.
4. **B4 `move_emails`**: `mailboxIds` → map `{id: true}` (stalwart) / массив (rfc).
5. **B5 `send_draft`**: `create` → `{emailId: {"emailId": emailId, "identityId": id}}` (stalwart) /
   `{emailId: {"identityId": id}}` (rfc); `onSuccessUpdateEmail` → `{update: {emailId: {mailboxIds: {sentId: true}}}}`
   (stalwart) / `{accountId, update: {emailId: {mailboxIds: [sentId]}}}` (rfc).
6. **B6 ошибки методов → ToolError всегда.** Любой `notCreated`/`notUpdated`/`notFound`/`error` в ответе метода
   → ToolError с текстом сервера. **Категорически запрещено** рапортовать об успехе, когда метод упал.
   (Сейчас `send_draft` именно так и врал.)
7. **B7 `whoami.state`** → **account state**, а не sessionState. Взять из ответа дешёвого `Mailbox/get`
   (`properties: ["id"]` → поле `state` в ответе метода). `list_changes` использует его же (`since_state`).
   Описание тула уже обещает «account state token» — код должен ему соответствовать.
8. **B8 `downloadUrl`**: подставлять `{type}` (и `{accept}`) — content-type вложения,
   по умолчанию `application/octet-stream` (`jmap.py::_blob_url`).

## Архитектура: диалектный слой

- В `jmap.py` завести диалект: `rfc` | `stalwart`.
- Определение **один раз на апстрим**, кэш в памяти: read-only проба `Email/query` с фильтром из двух условий
  (`{"operator":"and","conditions":[{"notKeyword":"$seen"},{"inMailbox":"<id>"}]}`, `limit: 1`):
  400 notRequest → `stalwart`, 200 → `rfc`. Проба ничего не меняет.
- Конфиг-override: `stalwart.dialect: auto | rfc | stalwart` (по умолчанию `auto`).
- Перевод форм — в одном месте клиента (`jmap.py`), а не размазывать по тулам.
- Ошибки → единый хелпер, который поднимает ToolError с текстом сервера.

## Тесты

- Мок-сервер, который **эмулирует диалект stalwart**: 400 на массив `inMailbox`, 400 на `operator`,
  400 на `header`, 400 на `mailboxIds`-массив, требует `emailId` внутри значения `create`,
  отдаёт `notCreated`/`notUpdated` при неверной форме.
- Тесты на каждый пункт B1–B8 + на диалект: `auto` выбирает stalwart по пробе, `rfc` — без 400.
- Регрессия: 148 существующих тестов остаются зелёными; при необходимости добавить кейсы под оба диалекта.

## Документация

- `docs/tools-schema.md` §1.6: добавить таблицу отклонений Stalwart 1.0.0 от RFC (факты выше) и фолбэки.
- `docs/tools-schema.md` §1.7: отметить пункты, закрытые живой проверкой (1, 2, 3, 6, 7, 9, 11, 12, 14, 15, 16),
  и оставить открытыми то, что не подтверждено (`$answered`, `header`).
- После правок — регенерация: `python tools/tools_schema.py` → `markdown blocks match generated schemas: True`.
- `README.md`: короткая секция «Stalwart dialect» — что отличается и что делается.

## Гейты приёмки

1. `python -m pytest -q` — зелёное, без skips/warnings.
2. `python tools/tools_schema.py` → `all schemas valid: True` + `markdown blocks match generated schemas: True`.
3. Мок-тест: диалект `auto` на stalwart-моке не даёт ни одного 400; на rfc-моке — тоже.
4. Тест на B6: сервер вернул `notCreated` → тул вернул ToolError, **а не** «успех».
5. `grep` по коду: нет `sqlalchemy|alembic|psycopg|fastapi|health`; bearer не логируется.
6. `git status --porcelain -uall` — только ожидаемые файлы. Ничего не закоммичено.

## Границы

- Не менять публичные схемы тулов (`docs/tools-schema.md` — только §1.6/§1.7 и тексты описаний, если нужно;
  **JSON-схемы тулов не трогать**).
- Не добавлять новых тулов, полей, health, UI, БД.
- Не коммитить и не пушить.
- Режим: короткие циклы, сразу проба, числа и вердикты. Если инструмент не работает — не обходить сырыми
  вызовами, сказать.

## Отчёт мне (оркестратору)

3–6 строк: гейты 1–6 — каждый «пройден/не пройден»; вывод pytest; сколько пунктов B1–B8 закрыто;
что осталось (живой прогон); риски.
