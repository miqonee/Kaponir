# Проверка и восстановление

## 1. Проверка пакета без моделей

```sh
python3 benchmark/verify.py
```

Только стандартная библиотека Python 3.10+. Сверяются все файлы по корневому
manifest, gzip по каталогу и исходному validation-manifest, aliases и корпус.
42 ячейки основной матрицы пересчитываются независимым кодом по сохранённым
кандидатам/решениям; защищённые значения и review учитываются как в методике.
Ни одно обращение к LLM, HF, внешнему API или исходному `/tmp` не выполняется.
Проверка упаковки, офлайн-восстановления и повторной сборки свода уже выполнена;
результат — [PACKAGE_CHECKS.json](PACKAGE_CHECKS.json).

## 2. Восстановление JSON и таблиц без генерации

Из корня репозитория. Output должен быть новым или пустым и вне самого пакета:

```sh
BENCH_RESTORE=$(mktemp -d /tmp/benchmark-restore.XXXXXX)
python3 benchmark/restore.py --output "$BENCH_RESTORE"
python3 scripts/report_validation_results.py   --directory "$BENCH_RESTORE/artifacts/validation-schemes"   --output "$BENCH_RESTORE/reports/VALIDATION_RESULTS.json"   --tables-output "$BENCH_RESTORE/reports/VALIDATION_TABLES.md"
cmp benchmark/data/validation-results.json "$BENCH_RESTORE/reports/VALIDATION_RESULTS.json"
```

restore возвращает исходные пути **внутри нового каталога**, не старые абсолютные
пути машины. Восстанавливает JSON/логи artifacts, сохраняет неудачные исторические
опыты, но не исполняет их. `--include-ui` добавляет скриншоты. Веса, бинарный
экспортёр детекторов, Vault и старые процессы не восстанавливаются.
Генератор отчётов пишет matrix.json во входной каталог; поэтому используется копия.

## 3. Исходный код и история без интернета

Bundle содержит main на `627f58d` и всю достижимую историю — 36 коммитов.
В него намеренно не входит более поздний коммит упаковки этого каталога.

```sh
BENCH_SOURCE=$(mktemp -d /tmp/benchmark-source.XXXXXX)
git clone --branch main benchmark/evidence/source/repository.bundle "$BENCH_SOURCE/repo"
git -C "$BENCH_SOURCE/repo" checkout --detach 627f58d
```

Для точного восстановления свода используйте скрипт из этого checkout вместо
текущего `scripts/report_validation_results.py`. Он найдёт исходный корпус и свои
зависимости там же. Старые реализации доступны, например, по `36b8567`,
исправления runtime/UI — `4c1b178`, сохранение ответов поиска — `da1f653`.

## 4. Если нужно именно повторить генерацию

Это отдельная затратная работа, не необходимая для чтения результатов. Потребуются
GGUF и runtime с указанными SHA, исходники и экспортёр соответствующей версии,
новый каталог output, те же документы, prompt/schema, политика и sampling.
Точные модели — [models.json](data/models.json), методы — [METHODOLOGY.md](METHODOLOGY.md).

Пример основного профиля (веса заранее установлены):

```sh
cargo build -p kaponir-cli --example detector_candidates
python3 scripts/evaluate_validation_schemes.py --model gemma4-e4b-qat   --model-dir /path/to/models --runtime /path/to/llama-server   --detectors target/debug/examples/detector_candidates --response-format auto   --context-tokens 4096 --output-tokens 1600 --timeout 1200   --output artifacts/new-reproduction/gemma-calibration
```

Пример полного контроля:

```sh
python3 scripts/evaluate_context_controls.py --model gemma4-e4b-qat   --model-dir /path/to/models --runtime /path/to/llama-server   --detectors target/debug/examples/detector_candidates --contexts document   --thinking bounded off --context-tokens 16384 --output-tokens 4096   --retry-output-tokens 8192 --temperature 1 --top-p .95 --top-k 64   --min-p 0 --seed 42 --timeout 1200   --output artifacts/new-reproduction/gemma-card-document
```

Новый бинарник экспортёра может иметь другой SHA. Сначала побайтно/структурно
сверьте его JSON-пул со снимком detectors.json: разница детекторов не должна
выдаваться за эффект LLM. Не возобновляйте старую контрольную точку с новым SHA.
Архивный frozen exporter ELF не включён, его выводы и SHA сохранены.

## 5. Проверки разработки

К исходной базе относятся 117 Python tests PASS; Rust 190 и clippy проходили
на предыдущем checkpoint. Логи сохранены в коллекции.

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/check_docs.py
```

В исходной managed-песочнице четыре worker-теста требовали разрешения loopback.
Ошибка создания сокета не означает, что требуется менять worker. На другой машине
проверяйте реальные ограничения среды. Перенос пакета не требует Cargo/Rustup,
а повторение сборки — требует toolchain и зависимостей исходного проекта.
