# README visuals

The overview diagrams are editable SVGs, not application screenshots:

| Asset | Language | Use |
|---|---|---|
| `kaponir-flow-en.svg` | English | English README workflow |
| `kaponir-flow-ru.svg` | Russian | Russian README workflow |
| `kaponir-masking.jpg` | Russian | Existing masking laboratory capture |
| `kaponir-worker.jpg` | Russian | Existing document worker capture |

The `console-dashboard-*-en.png` assets are real English UI captures. Do not translate text
over screenshots or reuse Russian captures in the English README.

## Capturing replacement screenshots

When screenshot capture is available:

1. Start the application with an isolated development home and synthetic values only.
   Use the setup in [Contributing](../../CONTRIBUTING.md#run-and-verify).
2. Select **EN** for `README.md` or **RU** for `README.ru.md` before opening each screen.
   Match the sample document language as well; switching the UI does not translate documents.
3. For the laboratory, use `password: Cedar-47!Plum` as a minimal reproducible input.
   Keep the source, actual masked output, and decision explanation visible. Token numbers
   come from the test vault; do not edit them into an expected result.
4. For the worker, show its actual connection and queue state. Do not imply that a model
   is downloaded or a job completed unless it is. A connected idle worker is sufficient.
5. Use the same theme, viewport, and browser zoom for both languages. Keep labels legible
   at README width, close unrelated overlays, and exclude browser chrome and real file paths.
6. Save English captures as `kaponir-masking-en.png` and `kaponir-worker-en.png`, then add
   them to the English README with English alt text and short captions. Display the main
   screen at full width; put the secondary screen in a collapsible details block.
7. Check the rendered README and verify that labels, dialogs, and tooltips use the intended
   language. Keep synthetic values and the screenshot's real state consistent with the caption.

Do not create screenshots by compositing, image generation, or replacing UI labels in an
image. Update the workflow SVGs directly when the documented flow changes.

## Консоль после переработки

`console-dashboard-dark.png`, `console-dashboard-light.png`, их варианты `-en.png` и `console-documents-dark.png`
сняты `scripts/validate_console.py` на синтетическом трафике локального fixture-поставщика.
Скрипт также проверяет 390/768/1440 px, RU/EN, потоковые ответы, аудит и запуск воркера.
Полные снимки и журнал сохраняются в `artifacts/console` (также доступны как артефакт CI).
