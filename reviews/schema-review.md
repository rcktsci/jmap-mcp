# Ревью: JSON Schema 15 MCP-инструментов (jmap-mcp)

Дата: 2026-10-27. Роль: ревьюер (не автор). Объект: `docs/tools-schema.md`, `tools/tools_schema.py`, `tools/tools_schema_check.json`, `docs/brief-tools-schema.md`, дизайн-док v2.

## Вердикт

**Принято с правками.** Схемы валидны, описания сильные, md ↔ генератор рассинхронизированы не были. Но сам генератор **не запускается** в текущем виде (путь до `docs/tools-schema.md` перепутан — падение раньше проверки сверки), плюс одна реальная дыра в выразимости контракта `move_emails` и пара расхождений между документами. Схемы не переписывал: только указания, что и где чинить.

## Как проверял

- Запуск `python tools/tools_schema.py` → **FileNotFoundError: tools\tools-schema.md** (упало на `inject_into_markdown`, до `verify_markdown`).
- Независимая сверка: 30 JSON-блоков из `docs/tools-schema.md` сопоставлены со схемами из `tools/tools_schema_check.json` → **0 расхождений, все 30 валидны** (Draft202012Validator). Т.е. на сегодня md и генератор синхронны — но штатный скрипт этого подтвердить не может.
- Семантические пробы валидатором (см. §4).

---

## Таблица: пункт (из §1–6) | находка | вердикт | что делать

| § | Пункт | Находка | Вердикт | Что делать |
|---|---|---|---|---|
| 1 | Согласованность | `tools_schema.py`: `HERE = Path(__file__).parent` → `tools/`; `DOC_MD = HERE/"tools-schema.md"` → `tools/tools-schema.md` (нет такого файла). Скрипт падает до `verify_markdown`, сверка md↔генератор штатно не выполняется никогда. | **fix (blocker)** | В `tools_schema.py` заменить `DOC_MD = HERE / "tools-schema.md"` на `DOC_MD = HERE.parent / "docs" / "tools-schema.md"`. `REPORT_JSON` оставить в `tools/` (или перенести оба в `docs/` — но оба в одном месте). После правки прогнать `python tools/tools_schema.py` — должно напечатать `markdown blocks match generated schemas: True`. |
| 1 | Согласованность | Заголовок `docs/tools-schema.md` §1: «команда `python docs/tools_schema.py`», «Источник истины — `docs/tools_schema.py`» — файл лежит в `tools/`. | **fix** | В шапке md исправить пути на `tools/tools_schema.py` / `python tools/tools_schema.py`. |
| 1 | Согласованность | Докстринг `tools_schema.py` (стр. ~14) обещает отчёт в `docs/tools_schema_check.json`; фактически пишется `tools/tools_schema_check.json`. | **fix** | Поправить текст докстринга (или перенести отчёт). |
| 1 | Согласованность | Дизайн-док v2 §7: `attachments: [{"blobId": ...}]` — против snake_case; и §7: «Ошибка — данные ({"error": ...}), а не исключение» — против §1.5 `tools-schema.md` (ToolError). Первое автор уже пометил (§1.4), второе — нет. | **fix** | В дизайн-доке v2 §7 исправить `blobId` → `blob_id` и строку про ошибки на «ошибка = ToolError (isError:true)» в соответствии с §1.5. |
| 2 | Качество для агента | Пагинация (`position`/`limit`/`has_more`/`total`), форматы дат (`date-time`), единицы (bytes), untrusted-body предупреждения — присутствуют и связны. `whoami.state` = токен аккаунта, в описании явно «pass it to list_changes as since_state»; `list_changes.state` — новый токен; перегрузка имени снята описаниями. | **ok** | — |
| 2 | Качество для агента | `search_emails` и `count_emails` декларируют «те же фильтры»; `count_emails` не имеет `has_attachment`… нет, имеет. Фильтры совпадают. | **ok** | — |
| 3 | Валидность | Все 30 схем валидны (check_schema) — подтверждено отчётом и повторной проверкой. Пустых/бессмысленных схем нет. | **ok** | — |
| 3 | Валидность | `required` с полями, которых нет в выдаче, не найдено; output-поля соответствуют figures. | **ok** | — |
| 4 | Выразимость | `additionalProperties: false` — везде, включая тулы без параметров. XOR (`allOf/not`) корректен. «Хотя бы один флаг» у `set_flags` с `type: boolean` — корректно исключает `null` (проба: `{seen:null}` → invalid). | **ok** | — |
| 4 | Выразимость | **`move_emails`:** `anyOf` ветки = голые `{"required": [name]}` без ограничения типа. Проба: `{"ids":["x"], "mailbox_role": null}` → **VALID**, хотя описание тула обещает «Exactly one target must be given». Схема пропускает «цель = null». | **fix (важно)** | В `tools_schema.py` `REQUIRED_ONE_OF["move_emails"]` генерация: ветки вида `{"required": [name], "properties": {name: {"type": "string"}}}` (тот же паттерн, что и `REQUIRED_ONE_FLAG`). Это и есть «ровно одна не-null цель». Проверку в теле тула оставить как defense-in-depth (SDK эти правила всё равно не валидирует). |
| 4 | Выразимость | `apply_input_contract` — минимальный и корректный обходной путь; ручная правка inputSchema после генерации не нужна (генератор остаётся источником истины). | **ok** | — |
| 5 | Stalwart-риски | См. таблицу ниже. Критично для релиза: Thread/get, calculateTotal, state, полнотекстовый оператор, `header`/`before`/`after`/`hasKeyword`/`notKeyword`. | **open** | Пройти §1.7 на живом Stalwart до реализации; fallback'и прописаны. |
| 5 | Stalwart-риски | Новый риск, не учтённый в §1.7: `Email/query` с `limit: 0` для `count_emails` — RFC 8620 допускает `limit: null`, но не оговаривает 0; возможен отказ сервера. | **open** | Добавить в §1.7 проверку «limit:0 принимается?»; fallback — `limit:1` + `calculateTotal` (total и есть count). |
| 6 | Покрытие | Сценарий владельца (whoami → list_mailboxes → search → get_email → set_flags → upload → create_draft → send_draft) закрыт целиком. Лишних тулов нет; `count_emails` оправдан как дешёвый счётчик. | **ok** | — |
| 6 | Покрытие | `download_attachment` пишет файл в ФС сервера и не возвращает байты — семантика «путь на своей машине» верна только при локальном развёртывании MCP-сервера (listen 127.0.0.1). При удалённом клиенте содержимое до агента не доходит. | **open** | Зафиксировать ограничение в описании тула (одна фраза «works when the MCP server runs on the same machine as the client») — либо оставить как есть как осознанное решение. |
| 6 | Покрытие | `list_changes` возвращает только id; для чтения — N вызовов `get_email`. Приемлемо, пагинация через `has_more_changes`. | **ok** | — |

---

## Правки по приоритету

### Блокеры
1. **[F1] `tools_schema.py`: путь до `docs/tools-schema.md`.** Заменить `DOC_MD = HERE / "tools-schema.md"` на `DOC_MD = HERE.parent / "docs" / "tools-schema.md"`. Без этого генератор не запускается, штатная сверка md↔генератор не выполняется (падает на `inject_into_markdown`). После правки: `python tools/tools_schema.py` должно вывести `markdown blocks match generated schemas: True`.

### Важно
2. **[F10] `move_emails`: контракт «ровно одна цель» пропускает `null`-цель.** В `tools_schema.py` `REQUIRED_ONE_OF` для `move_emails` генерировать ветки `{"required": [name], "properties": {name: {"type": "string"}}}` (не голый `required`). Тот же приём, что уже используется в `REQUIRED_ONE_FLAG`. После регенерации проба `{"ids":["x"],"mailbox_role":null}` должна стать невалидной.
3. **[F4] Дизайн-док v2 §7: две рассинхронизации.** `attachments: {"blobId": ...}` → `{"blob_id": ...}`; «Ошибка — данные`{"error":...}`» → «Ошибка = ToolError (isError:true)». Иначе источник правды по архитектуре противоречит `tools-schema.md` §1.4/§1.5.

### Косметика
4. **[F2]** Шапка `docs/tools-schema.md` §1: `docs/tools_schema.py` → `tools/tools_schema.py`, `python docs/tools_schema.py` → `python tools/tools_schema.py`.
5. **[F3]** Докстринг `tools_schema.py` (~стр. 14): отчёт пишется в `tools/tools_schema_check.json`, а не `docs/…`. Выровнять.

---

## Риски Stalwart (критичность → fallback)

| Риск | Критичность | Fallback (из §1.6/§1.7) |
|---|---|---|
| `Thread/get` не поддерживается / поля нет | высокая (get_thread — единственный путь к переписке) | `Email/query` с фильтром `inThread`; схема ответа та же |
| `calculateTotal` дорог/отсутствует | высокая (total в list/search/count) | флаг `include_total` (default false) + `total: int \| None`; либо убрать total, счёт из count_emails |
| `state` нет в ответах/`session.state` ≠ account state | высокая (list_changes невозможен) | если state нет — list_changes уходит, whoami.state=null |
| Полнотекстовый оператор для `text` | средняя | явная ошибка вместо частичного результата (текст уже в описании поля) |
| `header`/`before`/`after`/`hasKeyword`/`notKeyword` | средняя | прокси-фильтрация (дорого) или явный отказ тула |
| Точные строки `role` (регистр/нестандартные) | низкая | нормализация нижний регистр + `other` |
| Формат `downloadUrl`/`uploadUrl` (+ multipart-поле, Bearer) | средняя | подбор по живой проверке; иной маршрут |
| `EmailSubmission/set` + `onSuccessUpdateEmail` | средняя | перенос в Sent вручную (mailboxIds = Sent) |
| `Identity/get`: `replyTo`/`isDefault` | низкая | reply_to=[]; is_default=первая |
| `textBody` vs `bodyValues` | средняя | `bodyValues[partId]` как fallback |
| Набор `keywords` ($seen/$flagged/$draft/$answered) | средняя | маппинг на свои имена + описание в докстринге |
| `$answered` в Email/set | низкая | отказ флага с внятной ошибкой |
| `totalEmails`/`unreadEmails` без свойств | низкая | явно запросить свойства |
| `Email/query` с `limit: 0` (count_emails) — НЕ в §1.7 | средняя | `limit: 1` + calculateTotal |

---

## Открытые вопросы к владельцу

1. Подтвердить на живом Stalwart `limit: 0` в `Email/query` (count_emails) — иначе fallback `limit:1` + total.
2. Устроит ли ограничение `download_attachment` («файл на той же машине, что и MCP-сервер») — оставляем или описываем явно?
3. Чинить ли `move_emails` в схеме (F10) или оставить только проверку в теле тула? (Рекомендую — в схеме: двойная защита дёшева.)
4. `whoami.state` nullable (str|None), а `list_changes.since_state` required str — подтвердить, что при отсутствии state тул честно падает с пояснением, а не молчит.
