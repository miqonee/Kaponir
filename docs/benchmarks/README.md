# Исходные результаты синтетических экспериментов

Архивы содержат завершённые контрольные точки стенда: найденные значения,
ответы модели, решения валидатора, параметры и техническую телеметрию.
Источник — тестовые документы из репозитория. Пользовательские документы,
vault, ключи, веса и текст reasoning сюда не включаются.

`manifest.json` связывает путь исходной контрольной точки с gzip-архивом и SHA-256
распакованного JSON и фактическим статусом прогона. Этот SHA можно сопоставить с `result_sha256` в
[VALIDATION_RESULTS.json](../VALIDATION_RESULTS.json). Незавершённые прогоны
не архивируются как завершённые результаты. Отдельно включена прерванная техническая
диагностика `qwen08-native-unbounded`: её статус остаётся `interrupted`, а незапрошенные
случаи не получают оценку качества.

Пример чтения без установки зависимостей:

```sh
python3 - <<'PY'
import gzip, json
from pathlib import Path
path = Path('docs/benchmarks/qwen08-calibration--result.json.gz')
state = json.loads(gzip.decompress(path.read_bytes()))
print(state['metrics'])
PY
```

Обновление архивов: `python3 scripts/export_validation_evidence.py`.
