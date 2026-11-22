"""Render a researcher-readable report from the measured v4 evidence."""

import json

from dataset import ROOT, check_snapshot


def load(path):
    return json.loads(path.read_bytes())


def pct(value):
    return f"{100 * value:.1f}%".replace(".", ",") if value is not None else "—"


def table(title, pair, limits):
    old, new = pair["old"], pair["new"]
    diagnostic = pair["private_decisions"]
    rows = [
        f"### {title}", "",
        "| Показатель | Старый epoch-1 | Новая эпоха | Разница | Порог |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, field, denominator, threshold in (
        ("Приватные полностью скрыты", "private_full", "private", f"≥{limits['private_full_min']}"),
        ("Публичные затронуты", "public_touched", "public", f"≤{limits['public_touched_max']}"),
        ("Побайтовое восстановление", "restored", "documents", str(limits["restored_min"])),
        ("Документы с раскрытием", "documents_with_leak", "private_documents", "—"),
        ("Спорные полностью скрыты", "review_full", "review", "—"),
    ):
        rows.append(f"| {label} | {old[field]}/{old[denominator]} | {new[field]}/{new[denominator]} "
                    f"| {new[field] - old[field]:+d} | {threshold} |")
    for label, key in (("Ошибочный keep", "wrong_keep"), ("Пропуск приватной позиции", "omitted"),
                       ("Приватные позиции в непригодных ответах", "unusable_response")):
        a, b = (diagnostic[k].get(key, 0) for k in ("old_outcomes", "new_outcomes"))
        rows.append(f"| {label} | {a} | {b} | {b - a:+d} | — |")
    f1 = [pct(pair[k]["all_spans"]["f1"]["rate"]) for k in ("old_model", "new_model")]
    rows.extend([
        "", f"F1 по точной позиции, типу и действию {f1[0]} → {f1[1]}. Открытые и частично закрытые "
        f"приватные вхождения {old['private_open']} и {old['private_partial']} → "
        f"{new['private_open']} и {new['private_partial']}.",
        "", f"На тех же позициях появились {diagnostic['new_wrong_keep']} новых ошибочных keep и "
        f"{diagnostic['new_omissions']} новых пропусков. Ранее полностью скрытые приватные вхождения раскрылись "
        f"на {diagnostic['newly_exposed']} позициях. Новых публичных потерь {len(pair['positions']['new_public_losses'])}.", "",
    ])
    return rows


def render_report(run):
    manifest = check_snapshot(ROOT / "data/v4")
    evidence = load(run / "v3-audit.json")
    training = load(run / "full/run.json")
    selection = load(run / "selection.json")
    comparisons = load(run / "validation-comparison.json")
    selected = selection["selected_epoch"]
    rows = ["# Kaponir Privacy v4", "", "## Итог и допуск", ""]
    if selected is None:
        rows.extend([
            "Ни одна эпоха не прошла зафиксированный отбор на валидации. Независимый test-v4 сохранён для будущего "
            "зафиксированного кандидата. Регрессии новой LoRA v2 и v3 требуют выбранного адаптера.", "",
        ])
    else:
        rows.extend([f"На валидации выбрана эпоха {selected}. Ниже результаты test-v4 и регрессий того же адаптера.", ""])
    rows.extend(["## Проверка исходных ошибок v3", ""])
    if evidence["evidence"] == "local_receipts":
        errors = evidence["unusable_response_by_joined_error"]
        rows.extend([
            f"Среди 24 открытых или частично скрытых приватных позиций с непригодным ответом "
            f"{errors.get('ungrounded_occurrence', 0)} относятся к невозможности привязки, "
            f"{errors.get('invalid_schema', 0)} к схеме, {errors.get('duplicate_or_overlap', 0)} к пересечению или дублю, "
            f"{errors.get('invalid_json', 0)} к синтаксису JSON и {errors.get('truncated_answer', 0)} к усечению.",
            "",
            f"Из {evidence['public_losses']} публичных потерь {evidence['public_already_touched_by_detectors']} "
            f"затронули штатные детекторы, ещё {evidence['public_touched_only_after_model']} добавились с моделью. "
            f"Правила полей изменили диапазоны у {evidence['public_changed_by_field_rules']} из этих вхождений. "
            "Позиции UTF-8 и замены сохранены в v3-audit.json.", "",
        ])
    else:
        rows.extend(["Часть квитанций недоступна. Исходные цифры помечены в v3-audit.json как данные пользователя.", ""])
    rows.extend([
        "## Корпус, обучение и протокол", "",
        "| Раздел | Документы | Новые семейства | mask | keep | review | Чистые |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for split, item in manifest["splits"].items():
        action = item["by_action"]
        rows.append(f"| {split} | {item['records']} | {len(item['families_added'])} "
                    f"| {action['mask']} | {action['keep']} | {action['review']} | {item['clean']} |")
    rows.extend([
        "", f"Train сохраняет исходные {manifest['train_inherited']} записей v3 побайтно. Состав семейств и SHA-256 "
        "записаны в data/v4/manifest.json. Новые validation и test разведены по семействам до обучения.",
        "", f"Замороженный SHA-256 test-v4 {manifest['splits']['test']['sha256']}.", "",
        f"Две LoRA обучены от Qwen/Qwen3.5-2B@{training['revision']} на {training['gpu']}. "
        f"Выполнено {training['steps']} шагов за {training['elapsed_seconds'] / 60:.1f} мин. "
        f"Пик выделенной PyTorch VRAM {training['peak_allocated_bytes'] / 1024**3:.2f} ГиБ. "
        "Seed 42, BF16, rank 16, alpha 32, dropout 0, AdamW с LR 1e-4, batch 1 с накоплением 4. "
        "Loss считается по ответу ассистента.",
        "", "В первом сравнении совпадают промпт v3, client-label, полевые правила, контекст 1024, "
        "жадная генерация до 256 токенов и выключенный режим рассуждения. Покрытие и восстановление считаются "
        "по фактически полученному локальным HTTP-адресатом тексту. Отбор требует полного наблюдения, "
        "точного восстановления и не более 2% публичных потерь. Среди прошедших выбирается лучший recall точных mask.",
        "", "## Сравнение на validation-v4", "",
    ])
    limits = manifest["splits"]["validation"]["thresholds"]
    for variant, comparison in comparisons["fields"].items():
        rows.extend(table(variant + " при исходных правилах", comparison, limits))
    if "correction" in comparisons:
        rows.extend(["## Адресная коррекция", "", "Однократно проверено узкое исключение для суммы в явно "
                     "публичной строке CSV при точном keep модели. Новые раскрытия проверены попозиционно "
                     "относительно исходных правил на тех же весах.", ""])
        for variant, comparison in comparisons["correction"].items():
            rows.extend(table(variant + " с CSV-коррекцией", comparison, limits))
    rows.extend(["## Независимый тест и регрессии", ""])
    if selected is not None:
        for file in sorted(run.glob("comparison-*.json")):
            pair = load(file)
            rows.extend(table(file.stem.removeprefix("comparison-"), pair, pair["thresholds"]))
    else:
        rows.extend([
            "Пороги test-v4 составляют 55/57 приватных, максимум 1/57 публичных и 71/71 восстановлений. "
            "Старый контроль v3 дал 157/217, 22/182 и 72/72. Старый контроль v2 дал 288/334, 10/167 и 163/163.", "",
        ])
    rows.extend([
        "## Повторный запуск и материалы", "",
        "Из корня репозитория запустить ./research/privacy-training/run-v4.ps1 в PowerShell. Адаптеры лежат "
        "в full/epoch-1 и full/epoch-2 каталога прогона. Сырые ответы, привязка, квитанции и попозиционные "
        "переходы сохранены в measurements и validation-comparison.json. Хеши фактического кода "
        "и параметры отбора записаны в code-provenance.json, preregistration.json и selection.json.", "",
    ])
    (run / "report.md").write_text("\n".join(rows), encoding="utf-8", newline="\n")
