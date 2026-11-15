# Что сделано в сессии и как получились результаты

Исследование 11–14 ноября; упаковка 15 ноября 2026. Актуальная база результатов
`627f58d`. Этот файл фиксирует выполненную работу, а NEXT_STEPS — ещё не выполненную.

## Основные этапы

| Этап | Результат |
|---|---|
| Ошибки типов и непонятные настройки | Разделены обнаружение/категоризация/пересмотр, формат суммы защищён, исходная оценка сохраняется, tooltip-вопросники |
| Разбор решений | Причины review/событий, асинхронный пересмотр, явное применение/отмена, выбор типа/scope, RU/EN и размеры экрана |
| Детекторы | Регрессии timestamp Thu/Nov с сохранением настоящего Thu Lan Nguyen, m.goloshanov, Git ref/node ID, unknown PATH вместо PROJ |
| Установка моделей | Проверка наличия, загрузка, инструкция для закрытого контура, свои HF GGUF, закреплённые артефакты |
| Первые три 2B | Общее сравнение обнаружило обрывы Qwen/Huihui; исходные числа сохранены исторически |
| Инференс и внешний провайдер | Нативный бюджет, структурное разбиение, независимый review, проверка внешнего маршрута, перечисления и полного документа |
| Матрица семи моделей | Одинаковый локальный пул, документы и prompt hashes, шесть схем, выводы поиска/отсева раздельно |
| Диагностика | RAM-архив, reasoning, формат JSON, контекст статуса, защита credentials и ошибочные типы |
| Контроли | 32 правильные границы, single-candidate, record/document, bounded/off/native, sampling; неполнота отражена явно |
| Долговечность | Пер-response checkpoint перед рекурсией, replay без дублей, восстановление очереди после остановки runner |
| Завершение | Обе очереди completed, модели scratch удалены, toolchain восстановлен, фоновых прогонов нет |
| Упаковка | Корневой benchmark-пакет, все доступные результаты/логи/UI, офлайн Git и восстановление отчётов; 0 новых инференсов |
| Уточнение продолжения, 15 ноября | Пользователь подтвердил старт с постановки проверки приватности; добавлены практический разбор схем и последовательность политики/выделения цели/короткого контракта. Новых измерений нет |
| Очередь моделей, 15 ноября | Проверены карточки и GGUF для трёх LFM2.5, Tiny Aya Global, Qwen3.5-4B и Spark-X2.6-4B; записано расширение до 13 вариантов с учётом native reasoning, окна и памяти MoE. Веса не скачивались, модели не запускались |

## Подтверждённые технические причины

- Старый byte chunk: вход 3201 + ответ 895 = 4096, finish_reason=length, OOM=0.
- Qwen RAM-state archive по умолчанию: RSS ~2→7.7 GiB, вытеснение без OOM;
  отключение проверено повтором с совпадением SHA ответов.
- Native unbounded thinking: 4096/8192 reasoning без финального JSON при допустимом
  общем контексте. Отдельная причина, не input overflow и не доказанный OOM.
- Nanbeige: STOP с невалидным JSON; full bounded timeouts; parser/evidence/type
  ошибки отделены от смысловых keep. У полного публичного ответа off — экранирование.
- Контекст Nanbeige 1320 не включил статус; Gemma 1320 включил и всё равно получил keep.
- У Nanbeige до cache-fix потерялись тела пяти завершённых ответов незаконченного
  документа; эта часть вычислялась повторно один раз, legacy записи сохранены.
- Gemma full после остановки runner возобновлён с одним сохранённым ответом.
  Наблюдатель дождался исчезновения старых процессов, совпавший ответ взят из кеша.

## Проверки и их границы

Исходный финальный Python log: 117 tests, OK. Четыре loopback-теста сначала упёрлись
в sandbox PermissionError, после разрешения loopback все прошли. Rust 190/clippy —
успешные проверки предыдущего кода, не новая сборка при упаковке. Все 42 ячейки
пересчитаны, 41 опубликованный архив сверен. Во время архивирования не запускаются
модели и не меняются пользовательские правила/S1/Vault.

Исходные инструменты, проверки UI и журналы имеют собственные timestamps. Старый
файл со status=running хранится как свидетельство незаконченного опыта; это не
текущая очередь. Неполные ответы и незапрошенные случаи не выданы за завершённое качество.

## История кода, сохранённая в offline bundle

| Коммит | Дата | Назначение |
|---|---|---|
| `627f58d` | 2026-11-14 | Complete seven model evaluation with Gemma controls and practical findings |
| `bbc3002` | 2026-11-14 | Publish seven model matrix and Gemma privacy context evidence |
| `12dc657` | 2026-11-14 | Publish completed Granite context and reasoning controls |
| `eefb542` | 2026-11-14 | Publish all six Granite benchmark schemes and validation failures |
| `e277a3a` | 2026-11-14 | Publish completed Nanbeige full context and thinking controls |
| `65d9f2b` | 2026-11-14 | Report Nanbeige full document review outcomes and unanswered cases |
| `079b864` | 2026-11-14 | Document Nanbeige full context CPU timeouts with live token counters |
| `111bab5` | 2026-11-14 | Expose timeout and failed batch diagnostics in control tables |
| `e9bcd88` | 2026-11-14 | Publish all six Nanbeige benchmark schemes and raw evidence |
| `80087d3` | 2026-11-14 | Record completed Nanbeige union validation and resume provenance |
| `2a3cc71` | 2026-11-14 | Compare additive regional candidate pools without new inference |
| `d1f175d` | 2026-11-14 | Document Nanbeige context truncation and first validation results |
| `d8685ab` | 2026-11-14 | Expose request parsing failures separately in benchmark reports |
| `da1f653` | 2026-11-14 | Persist discovery responses before recursive benchmark requests |
| `40d47a0` | 2026-11-14 | Compare individual candidate validation across all light models |
| `b7bc08a` | 2026-11-14 | Finish MiniCPM native and recommended sampling controls |
| `6a9a570` | 2026-11-13 | Document MiniCPM context controls and raw output limitations |
| `3d26646` | 2026-11-13 | test: publish held-out coverage and separate output-contract failures |
| `243f246` | 2026-11-13 | docs: generate comparable model tables and preserve context visibility proof |
| `56037b0` | 2026-11-13 | test: record Qwen 0.8 context controls and independent failure evidence |
| `08c7358` | 2026-11-13 | docs: publish reproducible benchmark evidence and coverage by data type |
| `87e8462` | 2026-11-13 | test: complete four-model scheme matrix and isolate classifier failures |
| `8fbed2d` | 2026-11-13 | docs: record scheme results and interrupted cohort status |
| `4c1b178` | 2026-11-13 | fix(worker): bound runtime memory and preserve inconclusive reviews |
| `b7bb735` | 2026-11-12 | Budget model inference and improve independent review |
| `36b8567` | 2026-11-12 | Clarify context truncation and OOM evidence in model comparison |
| `9412eaf` | 2026-11-12 | Improve decision review and add Hugging Face model catalog |
| `9af24ee` | 2026-11-12 | Improve model installation diagnostics and background analysis UI |
| `291fbe4` | 2026-11-11 | Add realistic synthetic document corpus and evaluation tools |
| `24c2e36` | 2026-11-11 | Add background learning, categorization, and reversible rule automation |
| `3dc4705` | 2026-11-11 | feat: separate LLM analysis and S1 choice rule classification, add Qwen3.5-0.8B, enhance worker management and style dropdowns |
| `1633b7d` | 2026-11-11 | Redesign console, group sessions, and improve detection accuracy |
| `4d9e913` | 2026-11-11 | feat: implement full UI/UX, detector, Request Inspector, Qwen3 worker & telemetry overhaul |
| `01c7b4a` | 2026-11-02 | docs: refresh bilingual READMEs and clarify Kaponir comparison |
| `1d6d0cf` | 2026-11-02 | Add Xiaomi candidate discovery and clarify product documentation |
| `4f1bdbe` | 2026-11-02 | Initial public release of Kaponir |

Полные SHA и даты: [git-history.json](data/git-history.json). Старые документы,
скриншоты и исходные файлы найдены через [COLLECTION.json](data/COLLECTION.json).
