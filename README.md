# Genie provider registry

Public provider declarations for Genie. This repository contains registry data and documentation only; the Genie application remains separately licensed and is not distributed here.

Numeric `registry/<N>` releases contain `registry.toml`, `registry.tar`, and `registry.toml.sig`. Genie verifies the production Ed25519 signature and archive hashes before parsing. Registry updates are explicit; generation uses the user's installed provider copies.

The first release is `registry/2`, dated 2026-10-04, and requires Genie 0.2.0 or newer. It contains OpenAI, xAI, Gemini, and an empty local OpenAI-compatible template. These declarations are a dated snapshot, not a promise that an account has access or quota. No credentials, profiles, generated images, application source, or signing keys are included.

Production public key: `603d9f069ae7344d58a74f494f5dd64cf03c4fba0632684d6486b063addc6ee3`. Public-key SHA-256: `9cbac704124a6acfc859ea91857d1d0926694b4c9fc460a72f7ff5c5d367e96e`.

## Model watch

A weekly workflow lists each vendor's models and, when they differ from `providers/`, opens one pull request with unverified worksheets under `candidates/`. It never edits `providers/`, never signs and never publishes; a human verifies every value and publishes locally. The design, threat sketch, the secrets to create and the review and publish steps are in [docs/model-watch.md](docs/model-watch.md).

## Price estimates

`prices/prices.toml` seeds the registry's schema-2 price table from Genie's
embedded `crates/genie-app/data/prices.toml` (Genie 0.4.6). The initial copy is
byte-for-byte identical, including its existing source comments, dates and
source URLs. Every figure is a dated estimate, never a bill. Price entries match
an exact vendor host, operation and model ID.

The model-watch pull request includes **Price pages to recheck** when an entry
was checked more than 90 days before the UTC run date, or a newly listed image
model has no price entry. It links each stale entry's recorded sources and each
unpriced model's official vendor pricing page. Price warnings alone never open
a pull request; a human checks the sources and edits the table. The watch never
fetches prices, edits the table, signs or publishes it. Prices are intended for
the separately signed format-2 registry assets; existing format-1 assets remain
providers and profiles only. See [the model-watch guide](docs/model-watch.md).

Offline watch tests (Python 3.11+, standard library only):

```sh
python3 -m unittest discover -s tests -v
```
