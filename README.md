# Kaponir

**English · [Русский](README.ru.md)**

**A local, reversible text anonymizer and proxy for LLMs.**

Kaponir hides secrets, internal infrastructure, personal data, and commercial information
before sending text to an external language model, then restores the original values in its
response. The CLI, Web UI, proxy, and background document analysis share one local rule space
and encrypted vault.

[Quick start](#quick-start) ·
[Features](#what-kaponir-does) ·
[Documentation](docs/README.md) ·
[Security](docs/SECURITY.md) ·
[Contributing](CONTRIBUTING.md)

```text
Original request                     External LLM                       User response

Project: Northern Vector       →     Project: ANON_PROJ_1         →     Project: Northern Vector
password: Cedar-47!Plum        →     password: ANON_SECRET_1      →     password: Cedar-47!Plum
10.50.12.15                    →     248.131.59.15                →     10.50.12.15
                                    ↑
                       Mappings remain in the local vault
```

> **The main idea:** the model receives a useful working context without the original
> protected values. Reversibility comes from local storage, not model memory.

## Contents

- [Project goals](#project-goals)
- [What Kaponir does](#what-kaponir-does)
- [Quick start](#quick-start)
- [Use cases](#use-cases)
- [Rules, packs, and discover](#rules-packs-and-discover)
- [S1: optional candidate scoring](#s1-optional-candidate-scoring)
- [Data and security](#data-and-security)
- [Limitations](#limitations)
- [Architecture](#architecture)
- [Verification and development](#verification-and-development)
- [Documentation](#documentation)

## Project goals

AI agents benefit from real configurations, document excerpts, and support history, but these
sources may contain passwords, internal system addresses, customer names, payment details, and
commercial terms. Replacing all text with random characters destroys its structure and prevents
the model from reasoning effectively.

Kaponir addresses two connected tasks:

1. **Before a request**, it detects sensitive fragments and replaces them with stable placeholders
   or surrogates while preserving the syntax of text, JSON, YAML, configurations, and tool calls.
2. **After a response**, it finds known tokens and restores the corresponding original values,
   including inside streaming SSE and JSON tool arguments.

The project is designed for local operation. S1 is disabled by default, the interface and proxy
listen on a loopback address, and the external LLM provider receives only the processed request.
Kaponir is not a universal DLP and does not guarantee detection of every secret: its policy and
rules must be configured for your data.

## What Kaponir does

| Area | Implemented capabilities |
|---|---|
| **Masking** | Secrets and credentials, IP addresses, email, domains and URLs, paths, service names, identifiers, personal data, and user-defined values. Decisions depend on detectors, packs, rules, and policy. |
| **Reversibility** | Stable `ANON_<KIND>_<N>` tokens, DNS-compatible forms, HMAC pseudonyms, and IP surrogates; restoration of plain text and escaped JSON strings. |
| **CLI** | `mask`, `restore`, `discover`, uncertain-decision review, rules, packs, statistics, S1, and settings transfer. Works with files and shell pipelines. |
| **LLM proxy** | OpenAI Chat Completions and Anthropic Messages; text, `tool_result`, tool arguments, buffered responses, and streaming responses. |
| **Providers** | Up to 64 connections with separately encrypted keys, individual endpoints, shared model pools, session affinity, and explicitly enabled retry on HTTP 429. |
| **Web UI** | Traffic overview, connections, events, rules, decision review, masking laboratory, discover, S1 settings, and background analysis. Available in Russian and English. |
| **Discover** | Local document analysis, occurrence examples, stable IDs, an encrypted collection, and explicit confirmation of selected MASK rules. |
| **Background analysis** | A separate worker, SQLite queue, checkpoints, pause/resume/cancel; text files, images, PDF, and opt-in OCR. |
| **Integrations** | Sidecar REST for custom gateways and per-session streaming restoration; Docker Compose for the proxy and worker. |
| **Observability** | Sanitized events, technical JSON metrics, and Prometheus metrics without message contents or original values. |

For the detailed request and response field schema, see the [architecture](docs/DESIGN.md#9-прокси-для-агентов-этап-2-kaponir-proxy-kaponir-serve).
For the control panel, see the [control panel guide](docs/CONTROL_PANEL.md). The detailed guides
are currently written in Russian.

## Quick start

### Requirements

- A stable Rust toolchain with Cargo;
- Linux x64 is the primary locally verified platform; CI includes tests and builds on macOS,
  but macOS was not exercised in this local verification;
- Python 3.12+ and worker dependencies are required only for native image/PDF processing;
- No external models are required to build the project or run its standard tests.

### Build and mask locally

```bash
cargo build --locked --release -p kaponir-cli
export PATH="$PWD/target/release:$PATH"

# Creates ~/.config/kaponir: configuration, keys, vault, and rule files.
# Use KAPONIR_HOME or the global --home option for a different directory.
kaponir init --corp-domain mycompany.ru

printf '%s\n' 'password: Cedar-47!Plum' | kaponir mask
printf '%s\n' 'password: ANON_SECRET_1' | kaponir restore
```

The actual token number depends on the current vault. Do not copy the example token as a
ready-made restoration secret: first perform your own masking in a new workspace.

Inspect a file without storing mappings:

```bash
kaponir mask deployment.yaml --format yaml --dry-run --explain --report
kaponir mask deployment.yaml --format yaml --output safe.yaml
kaponir restore safe.yaml --output restored.yaml
```

`--dry-run` does not save new mappings, so its preview output is not intended for subsequent
restoration in another process. Keep the vault for a working mask → restore cycle.

### Web UI

```bash
kaponir ui --no-open
# Open http://127.0.0.1:8787/ui/
```

`kaponir ui` without `--no-open` also tries to open a browser. The same UI is available after a
normal `kaponir serve`. For the first connection, open **Providers and API**, add a provider,
retrieve its models, and save the connection. The panel displays the client address and an SDK example.

### Proxy for an existing client

```bash
kaponir serve

# Example for an Anthropic client:
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude

# Example for an OpenAI-compatible client:
OPENAI_BASE_URL=http://127.0.0.1:8787/v1 my-script
```

When a connection is saved, Kaponir injects the provider key. Without a matching profile, it uses
direct upstream mode with the client's credentials. Do not send real requests until you have
verified the address, route, and rules with synthetic data.

## Use cases

### 1. Share a configuration with a chat safely

Use the CLI or the **Laboratory** in the Web UI. The laboratory shows candidates, decisions, and
reasons; its preview does not save vault or journal entries. The save button records mappings
for later restoration.

### 2. Connect an AI agent without manually processing every message

Start `kaponir serve`, configure a provider, and point the SDK or agent to the Kaponir address.
The proxy processes supported text fields, while session memory masks values again when they
reappear in conversation history. Tool schemas and signed `thinking` blocks are not modified.

### 3. Create rules from corporate documents

```bash
kaponir discover contract.txt invoice.yaml --json
kaponir discover contract.txt --remember
kaponir discover --saved --json
kaponir discover --saved --select SUGGESTION_ID --scope team
kaponir discover --saved --reject OTHER_SUGGESTION_ID
```

A normal preview is local: it does not call S1, save the collection, or change rules. `--select`
creates only the selected exact MASK rules. `--reject` saves a negative training label but does
not create a KEEP rule or change current masking.

### 4. Analyze a directory, scans, or PDF files

```bash
kaponir worker --input-root /absolute/path/to/documents run .
kaponir discover --saved --json
```

For a persistent queue, run `kaponir worker serve` alongside `kaponir serve`. The worker extracts
text, passes it to discover, and stores suggestions; OCR model output does not become rules.
Q4/Q5 downloads, job management, and Compose are described in the [worker guide](docs/WORKER.md).

### 5. Integrate processing into a custom gateway

```bash
curl -s http://127.0.0.1:8787/api/v1/anonymize \
  -H 'content-type: application/json' \
  -d '{"text":"password: Cedar-47!Plum","session":"example-session"}'

curl -s http://127.0.0.1:8787/api/v1/deanonymize \
  -H 'content-type: application/json' \
  -d '{"text":"password: ANON_SECRET_1","session":"example-session"}'
```

For streaming, use `/api/v1/deanonymize/stream` with `session` and `stream` identifiers, a `chunk`
fragment, the `done` flag, and optional `json_escape`. See the [architecture](docs/DESIGN.md#9-прокси-для-агентов-этап-2-kaponir-proxy-kaponir-serve)
for the contract and boundaries.

### 6. Run with Docker Compose

```bash
cp deploy/compose.env.example .env
# Set two different long tokens and the source directory in .env.
mkdir -p documents

docker compose up -d --build kaponir                 # proxy only
docker compose --profile analysis up -d --build      # proxy + worker
```

Compose stores the vault, keys, queue, and models in volumes; the document directory is mounted
read-only. Do not use `docker compose down -v` for a routine update: it deletes data and keys.
See [worker and Compose](docs/WORKER.md#3-docker-compose) for details.

## Rules, packs, and discover

### Three rule scopes

- **Personal** — the workspace owner's private values.
- **Team** — rules shared by a team.
- **Global** — global policy; `enforced = true` cannot be overridden by a lower-priority scope.

Scopes organize rule priority and distribution, but they are not a ready-made multi-user RBAC
model. Exact values, regex, Composite DSL templates, MASK/KEEP, types, categories, and strategies
are supported. After rule changes, the Web UI rebuilds the engine without restarting and clears
affected masking caches.

```bash
kaponir rule add --value 'Example Client' --kind CLIENT
kaponir rule add --keep --value ingress-nginx
kaponir rule add --scope global --enforced --value 'Internal Project' --kind PROJ
kaponir rule list
kaponir rule check
```

Uncertain decisions can be converted into rules:

```bash
kaponir review --group
kaponir review mask 3 4 --kind SVC --scope team
kaponir review keep --shape 'x-x'
kaponir review template '{env:prod|stage}-{SVC}-{hash}'
```

### Language and domain packs

The built-in packs are `lang-en`, `lang-ru`, `domain-tech`, `domain-web`, and `region-ru`. They add
dictionaries, validators, and language rules; the core contains no Russian or regional logic.
A custom data pack is a directory containing `pack.toml` and `words.txt`, loaded via `pack_dirs`.

```bash
kaponir pack list
kaponir pack test ./packs/lang-en
```

`pack test` checks a pack's detectors against its labeled corpus without S1. It is not a quality
measurement on an independent production corpus.

## S1: optional candidate scoring

S1 estimates the confidentiality probability of **already detected uncertain candidates**. It does
not scan documents for new entities or compensate for detector misses. Hard masks and explicit
rules are not reevaluated by the model.

| Provider | Purpose |
|---|---|
| `none` | Default: deterministic detectors, rules, and policy only. |
| `ngram` | A local model based on L0 features; trained manually on confirmed decisions. |
| `http` | An external `probabilities` service or SystemOne within the selected disclosure level. |

Disclosure levels: **L0** provides features without the original string or context; **L1** adds
the value; **L2** adds limited context with other detected candidates replaced. L0 is not
encryption. L1/L2 disclose data to the selected service even in shadow mode. Credentials are
limited to L0, and new SECRET candidates from discover are never sent to S1 for scoring.

Start with shadow mode:

```toml
[s1]
provider = "ngram"
shadow = true

[s1.http]
# Used only when provider = "http".
disclosure = "l0"
timeout_ms = 200
```

```bash
kaponir s1 train       # at least 3 different confirmed values for each class
kaponir s1 status
kaponir s1 rollback
```

Training does not enable `ngram` automatically. `shadow = false` allows scoring to affect
uncertain decisions but does not prove calibration quality. If S1 fails, local decisions are
preserved and `[policy] uncertain` applies; its default is `mask`. See the [S1 guide](docs/S1.md)
for full contracts, the L2 profile, and limitations.

## Data and security

By default, data is stored in `KAPONIR_HOME`, otherwise `$XDG_CONFIG_HOME/kaponir`, otherwise
`~/.config/kaponir`:

```text
config.toml             settings; provider keys need not be stored in plaintext
vault.db                encrypted mappings, journal, sessions, and discover collection
vault.key               vault master key
token.key               optional shared HMAC/surrogate key
rules/*.toml            Personal, Team, and Global scopes
s1/                     local ngram models
worker/                 queue and checkpoints when the worker is configured here
models/                 OCR weights and runtime when this directory is selected
```

- Vault values and provider keys are encrypted with ChaCha20-Poly1305; searches use blind indexes.
- `vault.key` is required for restoration. Losing it makes old mappings unavailable.
- `token.key` / `KAPONIR_TOKEN_KEY` lets a team generate identical HMAC pseudonyms.
- `mask --irreversible` stores no mappings and cannot restore the original values.
- A non-local `listen` address requires an API token; external access additionally needs TLS and network controls.
- The UI uses no frontend build step, external fonts, trackers, or JavaScript dependencies.

For the complete trust model, storage, backup, and secret export, see [SECURITY.md](docs/SECURITY.md).
Settings transfer is not a vault backup: see [SETTINGS_TRANSFER.md](docs/SETTINGS_TRANSFER.md).

## Limitations

Understand these boundaries before using Kaponir with real data:

- **Detection completeness is not guaranteed.** A secret unknown to the detectors can remain exposed; S1 does not search for it.
- **Uncertain does not always mean secret.** Extra masks are possible; review them through rules and review.
- **Reversibility requires the vault and keys.** HMAC/irreversible mode and a lost vault cannot be reversed.
- **The proxy does not support the OpenAI Responses API.** Supported interfaces are Chat Completions, Anthropic Messages, and sidecar REST.
- **Not every model will preserve meaning.** Substitutions change the text visible to the model; exact restoration is possible only for tokens returned without distortion.
- **In-memory traffic is not an audit log.** The overview and events are bounded and lost on restart.
- **OCR is only a text source.** Quality depends on the page; the model does not automatically make mask/keep decisions.
- **Team/Global are not RBAC.** Separate user roles and a shared multi-server vault are not implemented.
- **The server trusts the workspace owner.** Kaponir does not protect data from a host administrator who controls the process, memory, keys, and vault.
- **Synthetic regressions are not a production evaluation.** Saved corpora verify known scenarios, not universal completeness and accuracy.

## Architecture

```text
CLI ───────────────┐
Web UI ────────────┤
OpenAI/Anthropic ──┼──→ Engine ─→ Recognizers + Packs + Rules ─→ masked text
Sidecar REST ──────┤       │                    │
Worker → Discover ─┘       ├─→ optional S1      │
                           └─→ Vault ←──────────┘
                                  │
                                  └─→ response restoration
```

The Rust workspace consists of nine crates:

| Crate | Responsibility |
|---|---|
| `kaponir-core` | Engine, tokens, policy, rules, discover, sessions, and interfaces. |
| `kaponir-recognizers` | Deterministic detectors and candidate resolution. |
| `kaponir-lang-ru` | Russian morphology and word forms. |
| `kaponir-region-ru` | Regional validators and payment details. |
| `kaponir-packs` | Loading language, domain, and regional packs. |
| `kaponir-vault` | SQLite, encryption, blind indexes, journal, and settings storage. |
| `kaponir-s1` | Local ngram and HTTP/SystemOne adapters. |
| `kaponir-proxy` | Proxy, REST, SSE, Web UI, metrics, and connections. |
| `kaponir-cli` | The unified `kaponir` binary, configuration, worker, and engine assembly. |

Architectural decisions and formats: [DESIGN.md](docs/DESIGN.md).

## Verification and development

```bash
cargo fmt --all -- --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace
python3 -m unittest discover -s scripts -p 'test_*.py'
node --check crates/kaponir-proxy/assets/app.js
node --check crates/kaponir-proxy/assets/panel.js
node --check crates/kaponir-proxy/assets/i18n.js
python3 scripts/check_docs.py
```

The regression test uses 64 synthetic documents, 1,344 labeled values, and 8,808 occurrences;
it checks public terms and byte-for-byte restoration. It preserves known behavior but is not an
independent quality evaluation on arbitrary data. See [OPERATIONS.md](docs/OPERATIONS.md) for the
methodology, benchmark, CI, and release verification, and [CONTRIBUTING.md](CONTRIBUTING.md) for
development guidelines.

## Documentation

Start with the [documentation index](docs/README.md). Main routes:

- [CLI reference](docs/COMMANDS.md) — commands, flags, formats, and examples.
- [Control panel](docs/CONTROL_PANEL.md) — UI, providers, pools, and events.
- [Discover](docs/DISCOVERY.md) — suggestions, collection, confirmations, and API.
- [Worker](docs/WORKER.md) — queue, OCR, PDF, and Docker Compose.
- [S1](docs/S1.md) — L0/L1/L2 disclosure, shadow/active modes, HTTP, and training.
- [Security](docs/SECURITY.md) — trust model, keys, networking, and backups.
- [Operations](docs/OPERATIONS.md) — quality, metrics, benchmark, CI, and release.
- [Settings transfer](docs/SETTINGS_TRANSFER.md) — shared JSON for CLI and Web.
- [Architecture](docs/DESIGN.md) — internal contracts and decisions.

Research and historical reports are listed separately in the [documentation index](docs/README.md#исследования-и-история).
They describe specific runs and do not replace the current guides. The detailed guides are
currently written in Russian; the Russian project overview is available in [README.ru.md](README.ru.md).

## License

[Apache-2.0](LICENSE). The project is named **Kaponir**; the `ANON` token prefix and historical
cryptographic labels are retained for compatibility.
