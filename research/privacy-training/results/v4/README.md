# Материалы Kaponir Privacy v4

В комплекте находятся измерения на validation-v4, сырые ответы, привязанные позиции, квитанции HTTP-получателя, исходные хеши и оба адаптера Qwen3.5-2B. Отбор завершился без допущенного кандидата, test-v4 сохранён для следующего отбора.

- [Отчёт](report.md)
- [Адаптер первой эпохи](full/epoch-1) и [адаптер второй эпохи](full/epoch-2)
- [Измерения](measurements/v4/validation) и [попозиционное сравнение](validation-comparison.json)
- [Аудит v3](v3-audit.json) и [диагностика v4](validation-diagnostics.json)
- [Контрольные суммы](evidence-sha256.json) и [правило отбора](preregistration.json)

Веса адаптеров хранятся в Git LFS. После переключения на ветку выполните git lfs pull. Для повторения эксперимента из корня репозитория используйте [run-v4.ps1](../../run-v4.ps1) с установленной средой пилота и локальным контрольным адаптером epoch-1 из прогона v3. Его SHA-256 записан в [assessment-inputs.json](assessment-inputs.json).
