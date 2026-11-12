# Contributing to Kaponir

Kaponir is a local reversible anonymization proxy. The control panel is embedded in the Rust
binary; it has no frontend build step, remote fonts, trackers, or JavaScript dependencies.

## Run and verify

```sh
cargo build --locked -p kaponir-cli
cargo run -p kaponir-cli -- --home /tmp/kaponir-development init
cargo run -p kaponir-cli -- --home /tmp/kaponir-development ui --no-open
cargo test --locked --workspace
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
python3 -m unittest discover -s scripts -p 'test_*.py'
node --check crates/kaponir-proxy/assets/app.js
node --check crates/kaponir-proxy/assets/panel.js
node --check crates/kaponir-proxy/assets/i18n.js
python3 scripts/check_docs.py
```

For the isolated browser theme regression, run this from the repository root:

```sh
python3 -m http.server 8766 --bind 127.0.0.1
```

Open `http://127.0.0.1:8766/scripts/test_theme.html` in a browser. Expect seven passing
scenarios and zero failures. This is a separate browser check, not part of Python unittest
discovery. It loads the actual theme bootstrap, theme event handlers, radio markup and CSS
from the application sources, without starting the API. It uses real localStorage and iframe
reloads; only `matchMedia` and its change events are simulated. Coverage includes restoring
both manual themes before app initialization, preserving the last manual choice across OS
changes and reloads, Auto following both OS themes and remaining Auto after reload, and
missing or invalid saved preferences. Use a separate port/origin from the running application;
the test restores pre-existing storage values when finished. Reload the page to rerun it.

Use synthetic data and a separate development home. Never commit real documents, provider
keys, vault keys, or decrypted logs. Tests use local mock providers and must not require
network calls to an external model. Corporate regression fixtures live in
`tests/fixtures/corporate-corpus` and contain only synthetic documents, annotations and reviewed
rules. Keep them with the source. Historical validation reports belong in the local, ignored
`artifacts/` directory, not in the public tree.

## Control panel

- `assets/index.html`: sidebar, semantic structure, forms and static labels.
- `assets/app.css`: shared styles, responsive layout and accessible focus states.
- `assets/app.js`: lab, document discovery and rule management.
- `assets/review.js`: unified detector/LLM review, explicit recommendations and action previews.
- `assets/help.js`: shared settings help for pointer, keyboard and touch.
- `assets/panel.js`: proxy overview, events, connection guide, settings and setup wizard.
- `assets/i18n.js` and `assets/locales/*.json`: language selection and translations.

Paths above are relative to `crates/kaponir-proxy`. Dynamic content is created with DOM APIs
and `textContent`. Never interpolate user data into HTML. CSP allows only local scripts and
styles. Dynamic visual properties use `element.style.setProperty`; setting an inline `style`
attribute is blocked by CSP. Retain keyboard access, labels, visible focus and reduced motion.

Do not fabricate dashboard activity. UI previews and sidecar calls must not count as model
proxy traffic. A cache hit still represents masked values sent again and must be counted.
Streams are recorded on completion or cancellation. Public telemetry must never include
message contents, original values, model names, upstream URLs or session identifiers.

## Translation contributions

The dictionaries use source strings as keys (gettext style). Russian source keys keep the
existing interface easy to audit; all interface translations live outside application logic.
English is the fallback language. Browser language selects Russian when appropriate; an
explicit selection is saved locally. Changing language must preserve draft documents and forms.

1. Edit a value in `assets/locales/en.json` or `ru.json`, preserving its key.
2. Preserve numbered placeholders such as `{{0}}` and `{{1}}`. Never translate identifiers,
   template tokens such as `{SVC}`, regular expressions or API paths.
3. New static text uses `data-i18n`; attributes use `data-i18n-placeholder`,
   `data-i18n-title` or `data-i18n-aria-label`. Add the key to both dictionaries.
4. Dynamic labels use `tr(source, [arguments])`. Translate only interface labels, never
   document contents, rule values or tokens. Backend error codes may also be dictionary keys.
5. For a new language, copy a dictionary, add its code to `supported` in `i18n.js`, add a
   native language option in the selector, and embed its JSON route in `src/web.rs`.
   Extend dictionary parity tests to cover it.
6. Run `scripts/test_locales.py` via unittest discovery and check the affected screens in a browser.

## Documentation

The user-facing entry point is the English `README.md`; `README.ru.md` contains the Russian
overview with a mutual language switch. `docs/README.md` is the Russian documentation index.
Keep detailed S1, worker, provider and operations material in their dedicated guides instead
of duplicating it on the main page. Clearly distinguish implemented behavior, historical
experiments and future work. Examples must use synthetic values and existing commands.

Run `python3 scripts/check_docs.py` after editing any Markdown file. It checks local links
and GitHub-style heading anchors across the published documentation tree, without network
access. Local, ignored `artifacts/` are intentionally excluded. It intentionally skips fenced code and external URLs. `--root PATH` checks
another documentation tree, such as an unpacked release. `scripts/release_check.py` performs
this check automatically on every archive it creates. Also verify changed commands against
`kaponir --help` and describe any scenario that was not actually exercised.

## Change descriptions

Explain the user-visible behavior and include the relevant checks. For detector changes,
provide sensitive and public examples, restoration checks, and corpus coverage. For traffic
accounting, cover cached requests, streaming and failure paths. Do not present synthetic
regression results as independent evidence of production detection quality.

## License

Contributions are accepted under the project's [Apache-2.0 license](LICENSE). Do not copy
third-party code, model weights, datasets, or documentation into the repository without
checking their license and recording the required attribution.
