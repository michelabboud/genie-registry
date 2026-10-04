# Genie provider registry

Public provider declarations for Genie. This repository contains registry data and documentation only; the Genie application remains separately licensed and is not distributed here.

Numeric `registry/<N>` releases contain `registry.toml`, `registry.tar`, and `registry.toml.sig`. Genie verifies the production Ed25519 signature and archive hashes before parsing. Registry updates are explicit; generation uses the user's installed provider copies.

The first release is `registry/2`, dated 2026-10-04, and requires Genie 0.2.0 or newer. It contains OpenAI, xAI, Gemini, and an empty local OpenAI-compatible template. These declarations are a dated snapshot, not a promise that an account has access or quota. No credentials, profiles, generated images, application source, or signing keys are included.

Production public key: `603d9f069ae7344d58a74f494f5dd64cf03c4fba0632684d6486b063addc6ee3`. Public-key SHA-256: `9cbac704124a6acfc859ea91857d1d0926694b4c9fc460a72f7ff5c5d367e96e`.
