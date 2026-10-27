# Бриф: правки по ревью схем (jmap-mcp)

Дата: 2026-10-27. Роль: **исполнитель правок**. Проект: `rcktsci/jmap-mcp` (корень репо = рабочий каталог).

## Контекст

Схемы 15 MCP-тулов разработаны (`docs/tools-schema.md`, генератор `tools/tools_schema.py`, отчёт
`tools/tools_schema_check.json`) и отревьюены (`reviews/schema-review.md`). Вердикт ревью:
«принято с правками» — 1 блокер, 2 важных, 2 косметических правки + 4 открытых вопроса владельца.
Открытые вопроса **закрыты решениями владельца** (ниже, §3) — реализуй их, не задавай заново.

## Файлы для чтения

- `reviews/schema-review.md` — ревью (правки F1, F10, F4, F2, F3 + таблица рисков + открытые вопросы).
- `docs/tools-schema.md` — предмет правок (шапка §1 + описания тулов).
- `tools/tools_schema.py` — генератор (источник истины для JSON-блоков в .md).
- `docs/superpowers/specs/2026-10-27-jmap-mcp-design.md` — дизайн (F4 правится там).

## 1. Правки кода и генератора

- **F1 (блокер).** `tools/tools_schema.py`: `DOC_MD = HERE / "tools-schema.md"` →
  `DOC_MD = HERE.parent / "docs" / "tools-schema.md"`. Сейчас скрипт падает `FileNotFoundError`
  до сверки md↔генератор.
- **F10 (важно).** `tools/tools_schema.py`: в `REQUIRED_ONE_OF["move_emails"]` ветки генерировать
  `{"required": [name], "properties": {name: {"type": "string"}}}` — тот же приём, что уже есть в
  `REQUIRED_ONE_FLAG`. Сейчас `{"ids": [...], "mailbox_role": null}` проходит схему.
- **F2 (косметика).** `docs/tools-schema.md` §1: пути `docs/tools_schema.py` → `tools/tools_schema.py`,
  команда `python docs/tools_schema.py` → `python tools/tools_schema.py`.
- **F3 (косметика).** `tools/tools_schema.py`, докстринг: отчёт пишется в
  `tools/tools_schema_check.json`, а не `docs/…`.
- **F4 (важно).** `docs/superpowers/specs/2026-10-27-jmap-mcp-design.md` §7:
  `attachments: [{"blobId": ...}]` → `{"blob_id": ...}`; строка «Ошибка — данные (`{"error": ...}`)»
  → «Ошибка = ToolError (`isError: true`) с actionable-текстом». Согласовать с `tools-schema.md` §1.5.

## 2. Регенерация

После правок прогнать `python tools/tools_schema.py` (Python: `C:\Users\usenkovsv\AppData\Local\Python\bin\python.exe`).
Скрипт должен отработать без исключений, перегенерировать `docs/tools-schema.md` и
`tools/tools_schema_check.json` и напечатать, что markdown-блоки совпадают со сгенерированными схемами
(`markdown blocks match generated schemas: True`).

## 3. Решения владельца по открытым вопросам — реализовать

- **D1 (`limit: 0`).** `count_emails` **не** полагается на `limit: 0`: используется `limit: 1` +
  `calculateTotal: true`. Отразить в описании тула («внутренний limit=1, считается total») и добавить
  пункт «принимает ли Stalwart `limit: 0`» в список живой проверки §1.7 (с fallback `limit: 1`).
- **D2 (`download_attachment`).** Ограничение «файл остаётся на машине MCP-сервера» — принято осознанно.
  Добавить в описание тула одну фразу: работает, когда MCP-сервер запущен на той же машине, что и клиент;
  при удалённом развёртывании содержимое до агента не доходит. В `docs/tools-schema.md` §1.6 (риски)
  добавить этот пункт как ограничение с тем же fallback-описанием.
- **D3 (`move_emails`).** Чинить **и** в схеме (F10), **и** оставить проверку в теле тула. Двойная защита.
- **D4 (`state`).** `whoami.state` — nullable (`str | None`). Если state недоступен, `list_changes`
  обязан честно упасть с ToolError и пояснением («сервер не отдал state; дельта-синк недоступна»),
  а не молча вернуть пустоту. Отразить в описании `list_changes` (поле `since_state` required) и в §1.7.

## 4. Гейты приёмки (все должны пройти)

1. `python tools/tools_schema.py` отрабатывает без исключений и печатает совпадение md↔генератор.
2. Все 30 схем (15 input + 15 output) проходят `Draft202012Validator.check_schema` — это делает сам
   генератор, отчёт в `tools/tools_schema_check.json`.
3. JSON-блоки в `docs/tools-schema.md` совпадают со сгенерированными (проверка генератора).
4. Проба после F10: `{"ids": ["x"], "mailbox_role": null}` — **невалидно** по схеме `move_emails`.
5. `git diff --stat` показывает только ожидаемые файлы: `tools/tools_schema.py`,
   `docs/tools-schema.md`, `tools/tools_schema_check.json`,
   `docs/superpowers/specs/2026-10-27-jmap-mcp-design.md`. Ничего лишнего не создано.
6. Новых тулов, новых полей и изменений семантики вне перечисленного — нет.

## 5. Границы

- Файлы — только внутри репо. **Ничего не коммитить и не пушить.**
- Не менять архитектуру и не добавлять тулы/поля сверх §1–3.
- Не придумывать содержимое писем, не писать реализацию тулов (это следующая фаза).
- Если правка требует чего-то не из перечисленного — записать в отчёт, не делать молча.
- Режим работы: короткие циклы, сразу проба, без длинных рассуждений.

## 6. Отчёт мне (оркестратору)

3–6 строк: гейты 1–6 — каждый «пройден/не пройден» + точный вывод, что напечатал генератор;
число изменённых файлов; что сделано по D1–D4; остатки (если есть).
