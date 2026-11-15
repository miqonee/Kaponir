# Где лежат доказательства и исходные материалы

## Каталог

| Материал | Место и назначение |
|---|---|
| Основной машинный свод | [data/validation-results.json](data/validation-results.json) |
| 41 опубликованный gzip | [data/validation/manifest.json](data/validation/manifest.json): имя, статус, SHA архива и JSON |
| Полная инвентаризация захвата | [data/COLLECTION.json](data/COLLECTION.json): происхождение, путь, SHA, кодировка, redactions, aliases, omissions |
| Индекс опытов | [data/experiment-index.csv](data/experiment-index.csv): текущие, ранние, прерванные, UI и диагностические результаты |
| Исходники/разметка | [data/corpus/expected.json](data/corpus/expected.json), соседняя input/; оригинальный README сохранён в gzip |
| Четыре фикстуры проб | [context-control-candidates.json](data/fixtures/context-control-candidates.json), [20 проб](data/fixtures/validation-review-heldout.json), [12 проб](data/fixtures/local-model-review.json), [каталог моделей](data/fixtures/validation-models.json) |
| Семь закреплённых GGUF | [data/models.json](data/models.json), включая SHA и HF URL |
| Будущие кандидаты: только метаданные, без результатов | [data/planned-models.json](data/planned-models.json): шесть моделей, первоисточники, ограничения и опубликованные SHA; локальная проверка весов ещё не выполнена |
| Старое исследование/внешний маршрут | [data/inference-results.json](data/inference-results.json) и raw results из индекса |
| Исходный код и история | [repository.bundle](evidence/source/repository.bundle) + [git-history.json](data/git-history.json) |
| Прежние тексты отчётов | reference/*.md.gz, byte-exact, включая MODEL_COMPARISON, REVIEW_QUALITY, INFERENCE_EXPERIMENTS, VALIDATION_EXPERIMENTS и SCHEME_COMPARISON |
| Установленные причины RAM-проблемы | [График памяти](evidence/figures/validation-memory.svg), runtime-cache-* архивы и исходники C++ в сессионном захвате |
| Очереди, выбор holdout, before-cache snapshot | Дополнительные artifacts, каталог даёт исходные имена и новые пути |
| Тесты и оперативные диагностики | evidence/session: сохранённые text/json/py/cpp/h/log/карточки моделей |

## Как читать gzip

```sh
python3 - <<'PY'
import gzip, json
from pathlib import Path
p=Path('benchmark/data/validation/gemma-calibration--result.json.gz')
s=json.loads(gzip.decompress(p.read_bytes()))
print(s['status'],s['metrics']['double'])
PY
```

Для исходного файла из artifacts найдите строку original_source в CSV. Если она
alias, данные уже сохранены в другом архиве с тем же SHA. Исторический источник
остаётся известен, идентичные байты не дублируются. `/tmp` в source — происхождение,
а не требование наличия файла сегодня.

## Какие статусы не смешивать

`completed` у файла означает завершение его протокола, но метод может иметь
quality_is_lower_bound_if_incomplete=true, failed_batches или errors. Для статуса
проверяйте также complete_documents/attempted_cases/invalid_decisions.
`interrupted`, `aborted`, старый `running` и контекстно ошибочный v1 сохранены
исторически, без подмены завершёнными оценками. В актуальной STATE все очереди завершены.

12/20 калибровочных проб, single controls, авторские sampling-профили, старый
app-layer и external отличаются от общей матрицы. Индекс сохраняет их отдельно.
Не собирать из них искусственный общий рейтинг точности или скорости.

## Проверки интерфейса

Сохранены все доступные снимки artifacts и их aliases, а также result.json
проверок. Примеры последних моделей/разбора:

- [Управление моделями RU, desktop](evidence/artifacts/validation-schemes/models-ui/models-ru-dark-1440.png)
- [Управление моделями EN, mobile](evidence/artifacts/validation-schemes/models-ui/models-en-light-390.png)
- [Разбор решений RU, desktop](evidence/artifacts/validation-schemes/review-ui/review-ru-dark-1440.png)
- [Разбор решений EN, mobile](evidence/artifacts/validation-schemes/review-ui/review-en-light-390.png)

Остальные изображения и протоколы находятся по каталогу. Это тестовые экраны;
по картинке не судят о численной точности модели.

## Что намеренно не входит

- GGUF и runtime executable: сохранены размеры/SHA/источники, повторно загружаются
  или переносятся в закрытый контур при необходимости генерации.
- Две собранные ELF-копии frozen detector exporter: выводы detectors.json,
  candidate_pool, его SHA и исходный код сохранены; бинарник можно пересобрать.
- Рабочие API-ключи, SQLite/Vault/sidecar, process PID и архив toolchain/cache.
  Они не нужны для исследования и не восстанавливаются в пользовательское окружение.
- В одном старом сессионном handoff рабочий provider key заменён маркером.
  Остальная информация и параметры сохранены; source/content SHA и факт redaction
  записаны. Сами ключи не хешируются отдельной публичной записью.

Точный список omissions — в COLLECTION.json. Все прочие доступные артефакты JSON,
логи и PNG внутри artifacts сохранены; все доступные текстовые `kaponir-*` файлы
верхнего `/tmp` захвачены как reference. Это снимок исследовательской информации,
а не резервная копия контейнера, весов или пользовательского Vault.
