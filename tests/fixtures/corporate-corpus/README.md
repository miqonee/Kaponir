# Synthetic corporate regression corpus

This compact fixture set supports `cargo test -p kaponir-cli --test corporate`.
It contains 64 synthetic documents in Russian and English, their annotations in
`manifest.json`, and the reviewed team rules used by the regression test.

The documents exercise masking and exact restoration across incidents, invoices,
onboarding, deployment, support chats, code reviews and security configuration.
All names, credentials, domains, addresses and other values are synthetic test
data; they are not production secrets.

Only the inputs required by the test are kept here. Historical logs, generated
HTML, result JSON and analysis snapshots remain in the developer's local,
ignored `artifacts/` directory. The corpus checks known examples; it is not an
independent measurement of detection quality on arbitrary real documents.
