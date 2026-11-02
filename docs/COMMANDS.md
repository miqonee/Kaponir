# Справочник команд CLI

[Главная](../README.ru.md) · [Карта документации](README.md) ·
[Discover](DISCOVERY.md) · [Worker](WORKER.md) · [S1](S1.md)

Все команды выполняет один бинарник `kaponir`. Полный синтаксис установленной версии доступен через
`kaponir --help` и `kaponir <команда> --help`.

## Общие параметры и пространство

```bash
kaponir --home /path/to/space <команда>
KAPONIR_HOME=/path/to/space kaponir <команда>
```

Без `--home` и `KAPONIR_HOME` используется `$XDG_CONFIG_HOME/kaponir` или `~/.config/kaponir`.
Глобальный `--home` ставится до подкоманды.

```bash
kaponir init
kaponir init --corp-domain mycompany.ru --corp-domain example.internal
kaponir init --wizard --language ru
kaponir init --learn contract.txt
kaponir stats
```

`init` создаёт конфиг, ключи и правила; повторный запуск не должен менять существующие ключи.
Мастер необязателен. `--learn` запускает локальный предпросмотр discover; с `--select ID` сохраняются
только выбранные предложения.

## Маскирование и восстановление

```bash
kaponir mask [FILE] [OPTIONS]
kaponir restore [FILE] [OPTIONS]
```

Без `FILE` команды читают stdin. `-o, --output FILE` записывает результат в файл.

### `mask`

| Параметр | Назначение |
|---|---|
| `--format auto\|text\|yaml\|json\|toml\|env\|ini` | Явный формат; без него он определяется по расширению, для stdin используется авто. |
| `--scope personal\|team\|global` | Контур новых записей; по умолчанию `personal`. |
| `--strategy variable\|hmac\|surrogate` | Стратегия для кандидатов вместо стратегии политики. |
| `--irreversible` | HMAC без записи в vault; восстановление невозможно. |
| `--no-journal` | Не сохранять спорные решения в журнал. |
| `--dry-run` | Предпросмотр без сохранения новых соответствий в vault. |
| `--explain` | Таблица кандидатов, вероятностей и причин в stderr. |
| `--json` | Один JSON с текстом, заменами и объяснениями. |
| `-r, --report` | Краткая сводка в stderr. |

Примеры:

```bash
cat deployment.yaml | kaponir mask --format yaml -r > safe.yaml
kaponir mask deployment.yaml --dry-run --explain
kaponir mask document.txt --json > mask-report.json
kaponir mask events.log --strategy hmac --irreversible > pseudonymous.log
```

Не используйте результат `--dry-run` или `--irreversible` для последующего обычного восстановления.

### `restore`

| Параметр | Назначение |
|---|---|
| `--json-escape` | Экранировать восстановленные значения для вставки внутрь JSON-строки. |
| `--strict` | Код выхода 2 при неизвестных токенах или токенах без прав. |
| `-r, --report` | Краткая сводка в stderr. |

```bash
kaponir restore safe.yaml --output restored.yaml
cat answer.json | kaponir restore --json-escape --strict
```

Восстановление понимает обычные и DNS-формы, варианты регистра и Markdown-экранирование токенов.
Оно не восстанавливает значения, для которых нет записи или прав доступа.

## Разбор спорных решений

```bash
kaponir review [--status open|masked|kept|dismissed|all] [--limit 50] [--group] [--json]
kaponir review mask IDS... [--kind KIND] [--scope SCOPE]
kaponir review keep IDS... [--scope SCOPE]
kaponir review mask --shape SHAPE [--kind KIND] [--scope SCOPE]
kaponir review keep --shape SHAPE [--scope SCOPE]
kaponir review template '{env:prod|stage}-{SVC}-{hash}' [--scope SCOPE]
kaponir review dismiss IDS...
kaponir review dismiss --shape SHAPE
```

`--group` объединяет записи по типу и форме значения. `mask` и `keep` создают правила; `dismiss`
закрывает запись без нового правила. Шаблон закрывает подходящие записи и задаёт Composite DSL.

```bash
kaponir journal purge             # по journal_ttl_days из конфига
kaponir journal purge --days 7    # также очищает старые сессии
```

Журнал содержит исходные значения. Очистка необратима и не заменяет защищённый backup.

## Правила и пакеты

```bash
kaponir rule add [--scope personal|team|global] [--kind KIND] \
  (--value VALUE | --template TEMPLATE | --regex REGEX) \
  [--keep] [--case-sensitive] [--enforced] \
  [--strategy variable|hmac|surrogate] [--category CATEGORY] [--note TEXT]

kaponir rule domain DOMAIN... [--scope SCOPE]
kaponir rule list [--scope SCOPE]
kaponir rule check
```

`--enforced` доступен только для Global. Точное значение, template и regex взаимоисключающи.
Категории: `credential`, `network`, `infra`, `identity`, `financial`, `contact`, `custom`.
Тип `KIND` определяет форму токена и отличается от категории политики.

```bash
kaponir rule add --value 'Северный Вектор' --kind PROJ --category custom
kaponir rule add --keep --value ingress-nginx --kind SVC
kaponir rule add --scope global --enforced --value 'Internal Project' --kind PROJ
kaponir rule domain example.internal --scope team
```

Файлы `rules/{personal,team,global}.toml` могут содержать конфиденциальные значения открытым текстом.
После ручного редактирования выполните `rule check`; работающий сервер перезагружает правила при
изменениях через UI, а не обещает отслеживать любые внешние правки файлов.

```bash
kaponir pack list
kaponir pack test ./packs/lang-en
kaponir pack test ./packs/lang-en --corpus /path/to/corpus.jsonl
```

По умолчанию `pack test` читает `tests/corpus.jsonl` внутри каталога пакета.

## Discover

```bash
kaponir discover FILES... [--s1] [--remember] [--json]
kaponir discover --documents-json documents.json [--s1] [--remember] [--json]
kaponir discover --saved [--json]
kaponir discover FILES... --select ID [--select ID...] [--scope SCOPE]
kaponir discover --saved --select ID [--scope SCOPE]
kaponir discover --saved --reject ID [--reject ID...]
```

- Без флагов — локальный предпросмотр без записи и S1.
- `--s1` — явная оценка подходящих примеров по текущему уровню раскрытия.
- `--remember` — объединение предложений с зашифрованной подборкой.
- `--saved` — чтение подборки без исходных документов.
- `--select` — точные MASK-правила только для указанных стабильных ID.
- `--reject` — отрицательные метки для обучения, без KEEP-правил.
- `--scope` — контур принимаемых правил, по умолчанию Personal.

`--saved`, `--documents-json` и список файлов — разные источники и не комбинируются. Повторный ID,
неизвестный ID или смешанное решение по одному предложению отклоняются. Полные лимиты и API —
в [DISCOVERY.md](DISCOVERY.md).

## S1

```bash
kaponir s1 train
kaponir s1 status
kaponir s1 rollback
```

`train` создаёт локальную `ngram` из подтверждённых меток; требуется минимум три разных положительных
и три отрицательных значения. Он не обучает HTTP/SystemOne и не включает провайдер автоматически.
`rollback` меняет местами текущую и предыдущую локальные модели. Настройка раскрытия и shadow/active —
в [S1.md](S1.md).

## Сервер и Web UI

```bash
kaponir serve [--listen ADDRESS] \
  [--anthropic-upstream URL] [--openai-upstream URL] [--no-system-hint]

kaponir ui [те же параметры serve] [--no-open]
```

По умолчанию адрес — `127.0.0.1:8787`, UI — `http://127.0.0.1:8787/ui/`. Настройки `[serve]`
из конфига применяются, если флаги их не переопределяют. `--no-system-hint` отключает добавление
подсказки о плейсхолдерах в system prompt.

Основные маршруты:

| Маршрут | Назначение |
|---|---|
| `POST /v1/messages`, `POST /v1/messages/count_tokens` | Anthropic Messages. |
| `POST /v1/chat/completions` | OpenAI Chat Completions. |
| `GET /v1/models` | Объединённый каталог моделей. |
| `POST /api/v1/anonymize` | Sidecar-маскирование. |
| `POST /api/v1/deanonymize` | Восстановление текста. |
| `POST /api/v1/deanonymize/stream` | Потоковое восстановление по `session` и `stream`. |
| `GET /api/v1/metrics` | Технические метрики и обезличенный трафик. |
| `GET /metrics` | Prometheus. |
| `GET /ui/` | Встроенная панель. |

При `api_token` REST и метрики используют `Authorization: Bearer`, прокси — `x-kaponir-token`.
Явный `X-Kaponir-Session` связывает историю и распределение подключений; без него сессия выводится
из первого сообщения разговора. OpenAI Responses API не реализован.

## Worker

Разовый обход без запущенного прокси:

```bash
kaponir worker --input-root /absolute/path/to/documents run .
kaponir worker --input-root /absolute/path/to/documents run contracts/file.pdf
```

Постоянная очередь:

```bash
kaponir worker serve
kaponir worker download q4
kaponir worker download q5
kaponir worker submit contracts
kaponir worker submit sessions/export.json
kaponir worker submit scans --s1
kaponir worker status
kaponir worker pause JOB_ID
kaponir worker resume JOB_ID
kaponir worker cancel JOB_ID
```

CLI управления обращается к настроенному worker; `run` выполняет обработку сам и требует свободной
очереди. Путь для `run`/`submit` относителен к `input_root`. Модель и Python-зависимости, токены,
лимиты и удалённый запуск подробно описаны в [WORKER.md](WORKER.md).

## Перенос настроек

```bash
kaponir config export --output settings.json
kaponir config export --include-secrets --output settings-with-keys.json
kaponir config import settings.json
```

CLI создаёт новый файл экспорта с правами `0600` и не перезаписывает существующий.
`--include-secrets` включает секреты в **незашифрованный** JSON. После импорта перезапустите сервер.
Это не резервная копия vault; см. [SETTINGS_TRANSFER.md](SETTINGS_TRANSFER.md) и
[SECURITY.md](SECURITY.md#экспорт-настроек--не-backup).
