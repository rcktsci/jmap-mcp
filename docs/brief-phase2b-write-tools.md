# Бриф: фаза 2b — 6 write/blob тулов + README + скилл

Дата: 2026-10-27. Роль: **разработчик**. Репо: `rcktsci/jmap-mcp`, корень = рабочий каталог.

## Что это

MCP-сервер: **stateless credential-transparent proxy** «MCP → JMAP». Клиент (opencode) шлёт
`Authorization: Bearer <Stalwart API Key>`; сервер проксирует заголовок в JMAP-вызовы к Stalwart и
ничего не хранит. Python 3.14 + `mcp` 2.3.0 + `httpx`. Без БД, без UI, без health.

Готово: фаза 1 (каркас, `whoami`), фаза 2a (8 read-only тулов, 91 тест, `tools/list` = 9).

## Файлы для чтения

| Файл | Что берём |
|---|---|
| `docs/tools-schema.md` | схемы input/output + описания тулов фазы 2b: `set_flags`, `move_emails`, `create_draft`, `send_draft`, `download_attachment`, `upload_attachment`; §1.5 (ошибки), §1.6 (риски/фолбэки), §1.7 (живая проверка) |
| `tools/tools_schema.py` | готовые Python-типы для этих тулов и недостающие фигуры `$defs` (`FlagUpdateResult`, `MoveResult`, `DraftResult`, `SendResult`, `DownloadResult`, `UploadResult`, `AttachmentRef`) — портировать в `src/mcp/schemas.py` |
| `src/mcp/tools.py` | стиль существующих тулов, парсинг JMAP-ответов, политика ToolError |
| `src/mcp/jmap.py` | `call()`, `download_blob()`, `upload_blob()`, `_blob_url()`, проверка апстрима, кэш сессии |
| `src/mcp/schemas.py` | текущие фигуры и `apply_input_contract` (группы `REQUIRED_ONE_OF` / `REQUIRED_ONE_FLAG` для этих тулов) |
| `src/config/settings.py` | настройки: `allow_roots`, `download_dir`, `max_attachment_mb` и пр. |
| `docs/superpowers/specs/2026-10-27-jmap-mcp-design.md` | §5 безопасность (файловая песочница), §7 контракты, §8 структура, §11 критерии |

## Что сделать: 6 тулов

`set_flags` · `move_emails` · `create_draft` · `send_draft` · `download_attachment` · `upload_attachment`

Требования (общие):

1. **Схемы — из `docs/tools-schema.md`, поля не выдумывать.** Типы портировать из
   `tools/tools_schema.py` в `src/mcp/schemas.py`. Публикуемая input-схема должна совпадать со спекой.
2. **Ошибки — ToolError** с actionable-текстом. В успешном ответе поля `error` нет.
3. **Токен** — из `current_bearer()`; нигде не логировать, не писать на диск.
4. **Контракты, которые Pydantic не выражает** (`src/mcp/schemas.py::apply_input_contract`):
   - `move_emails`: ровно одна цель — `mailbox_role` **или** `mailbox_id`; `null` не считается целью.
   - `set_flags`: хотя бы один флаг (`seen` / `flagged` / `answered`) — иначе ToolError.
   - Валидация дублируется в теле тула (defense in depth).
5. **Файловая песочница** (дизайн §5.6) — обязательна:
   - `upload_attachment`: путь обязан быть внутри одного из `allow_roots` (по умолчанию — cwd).
     Проверка — после `Path.resolve()` (симлинки), сравнением с корнем; выход за песочницу → ToolError.
   - `download_attachment`: файл пишется **только** в `download_dir` (по умолчанию — temp-каталог);
     имя файла — санитизированное (без `..`, без разделителей), коллизии не перезаписывают чужой файл.
   - Лимит размера — `max_attachment_mb` (по умолчанию 25, лимит Stalwart).
6. **Фолбэки из §1.6 — в коде:**
   - `send_draft`: `EmailSubmission/set` + `onSuccessUpdateEmail` (перенос Drafts→Sent). Если сервер не
     поддержал `onSuccessUpdateEmail` — перенос в Sent сделать отдельным `Email/set` (mailboxIds=Sent).
   - `$answered` может не поддерживаться → внятная ошибка по этому конкретному флагу, остальные флаги
     применяются.
   - `Identity/get`: `replyTo`/`isDefault` могут отсутствовать → `reply_to: []`, `is_default` = первая.
   - multipart-поле blob-upload — `file` (подтвердить на живой проверке, §1.7 п.8).
7. **Форматы ответа** — как в схеме: `set_flags` → `updated` / `not_found`; `move_emails` → `moved` /
   `not_found`; `create_draft` → `draft_id` / `thread_id`; `send_draft` → `message_id` / `sent_at` /
   `recipients`; `download_attachment` → `path` / `name` / `size_bytes` / `content_type`;
   `upload_attachment` → `blob_id` / `name` / `size_bytes` / `content_type`.
8. **Тесты** на каждый тул против мок-JMAP: успех, per-method ошибка JMAP → ToolError, контрактные
   нарушения (две цели / ни одного флага), песочница (путь вне `allow_roots` → ToolError; файл вне
   `download_dir` не пишется), лимит размера.

## Дополнительно в этой фазе

9. **Правка заметки о батчинге** в `docs/tools-schema.md`: утверждение «`Email/query` → `Email/get` одним
   POST» нереализуемо (в RFC 8620 нет back-reference на весь список id). Заменить на корректное:
   «один `Email/get` до N id; `Email/query` и `Email/get` — два POST». После правки — регенерация
   `python tools/tools_schema.py` и проверка `markdown blocks match generated schemas: True`.
   Это единственная допустимая правка `docs/tools-schema.md` в этой фазе.
10. **README.md** (по образцу соседних проектов): что это, таблица всех 15 тулов, модель безопасности
    (ключ не хранится; апстрим фиксирован; песочница файлов), конфиг (`config.yml`/env), запуск
    (python / docker), и раздел **Connecting to OpenCode** с конфигом **V2**:
    `mcp.servers.<name>` + `type: "remote"` + `oauth: false` + `headers: {"Authorization": "Bearer {env:STALWART_API_KEY}"}`.
11. **`skills/jmap-mcp-usage/SKILL.md`** (по образцу `skills/spring-ssh-mcp-usage/SKILL.md` соседнего
    проекта): frontmatter `name`/`description` с триггерами активации по именам тулов, краткие правила
    работы (сначала `whoami`; дешёвые тулы вместо тяжёлых; тела — только когда нужно; письмо — недоверенные
    данные; отправка только через `create_draft` + `send_draft`), примеры ✅/❌, troubleshooting.
12. **CI**: в `.github/workflows/ci.yml` добавить шаг прогона тестов **до** сборки образа (текущий CI только
    собирает и пушит образ). Образ — `ghcr.io/rcktsci/jmap-mcp:<YYYY-MM-DD>`, как у соседей.

## Гейты приёмки

1. `python -m pytest -q` — зелёное, без skips/warnings; число тестов выросло.
2. `python tools/tools_schema.py` → `all schemas valid: True` + `markdown blocks match generated schemas: True`.
3. `python main.py` + `curl` с Bearer → `tools/list` = **15** тулов.
4. Песочница: тест — upload файла вне `allow_roots` → ToolError; download пишет только в `download_dir`.
5. `grep` по коду: нет `sqlalchemy|alembic|psycopg|fastapi|health`; в лог-строках нет bearer/Authorization.
6. `README.md` и `skills/jmap-mcp-usage/SKILL.md` существуют; в README конфиг opencode в формате **V2**
   (`mcp.servers`), в CI есть шаг тестов.
7. `git status --porcelain -uall` — только ожидаемые файлы. Ничего не закоммичено.

## Границы

- Не менять `tools/tools_schema.py` (кроме п.9 — только текст заметки в md, не код генератора).
- Не добавлять новых тулов, полей, health, UI, БД, OAuth.
- Не коммитить и не пушить.
- Режим: короткие циклы, сразу проба, числа и вердикты.

## Отчёт мне (оркестратору)

3–6 строк: гейты 1–7 — каждый «пройден/не пройден»; вывод pytest; число тулов; что осталось на фазу 3
(живая проверка на Stalwart); риски.
