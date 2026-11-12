# Model watch

A scheduled GitHub workflow (`.github/workflows/model-watch.yml`) checks each vendor's official model list every Monday and, when it finds image models that `providers/*.toml` does not declare, or declared models the vendor no longer lists, opens a pull request with unverified worksheets under `candidates/`. A human verifies the values, moves them into `providers/`, and signs and publishes on their own machine. The workflow cannot publish anything.

Decided 2026-11-12 by Michel.

## Threat sketch

1. **Signing key.** The Ed25519 production key never enters CI: the workflow has no secret for it, creates no release and never pushes to `main`. A compromised workflow can at worst open a misleading pull request.
2. **Unverified data.** Everything in a watch pull request is unverified until a human checks it. Every proposed value is marked `TO VERIFY` with the doc URL to check, and the watch never edits `providers/`, so an unchecked value cannot reach a signed release by merging alone.
3. **Vendor keys.** The three keys can only list models: OpenAI's is a restricted key with Models read and nothing else; the xAI and Google keys sit on accounts with low spending caps. They travel only in request headers, are never printed, and exist only in the read-only job. A key is stripped of surrounding whitespace and refused, by secret name only, if it holds anything but visible ASCII, so a copy-paste slip cannot make Python echo it in an error in this public repository's logs.
4. **Fork pull requests.** The workflow has no `pull_request` or `pull_request_target` trigger, so a fork cannot run it or reach its secrets.
5. **Hostile vendor responses.** Model ids must match `[A-Za-z0-9][A-Za-z0-9._-]*` with no `..`, at most 100 characters; anything else is dropped and counted, never pasted. Listings are bounded (4 MiB, 2,000 entries, 10 pages) and redirects are refused so a key is not replayed to another host.
6. **Token scope.** Two jobs. `watch` holds the vendor keys and a read-only token, runs the script and uploads `candidates/` as an artifact. `pull-request` holds `contents: write` and `pull-requests: write`, has no vendor keys, runs no vendor-facing code, and admits only the three known worksheet names from the artifact. The push is `--force-with-lease`, so it never overwrites a branch someone moved after the job read it.

## Decision

A weekly scheduled job lists models through each vendor's documented list endpoint, selects image-capable models, compares them with `providers/*.toml` and writes `candidates/<vendor>.md`. One branch, `model-watch/auto`, is regenerated from `main` on every run and carries at most one open pull request, which is updated rather than duplicated. When nothing differs, no pull request is opened, and an open one is closed once every vendor has been checked and nothing remains. A vendor skipped because its secret is absent keeps the worksheet the branch already had, carried forward from `model-watch/auto`. Signing and publishing stay on Michel's machine through Genie's `scripts/registry_release.py`.

How each vendor is read (all in `VENDORS` in `scripts/model_watch.py`):

| Vendor | Endpoint | Key header | Image selection |
|---|---|---|---|
| OpenAI | `GET https://api.openai.com/v1/models` | `Authorization: Bearer` | No capability field is documented, so by name: `gpt-image-*`, `chatgpt-image-*`. The optional `shutdown_date` is reported for declared models. |
| xAI | `GET https://api.x.ai/v1/image-generation-models` | `Authorization: Bearer` | `output_modalities` contains `image`; name pattern `grok-*image*` only when the field is absent. Aliases count as listed for retirement. |
| Google Gemini | `GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000`, following `nextPageToken` | `x-goog-api-key` | No output-modality field, so `supportedGenerationMethods` must contain `generateContent` (the method Genie's `gemini-image` protocol calls) and the name must match `gemini-*image*`. |

A "retired" model is one declared in `providers/` that appears nowhere in the vendor's listing (as an id or an alias). A key without access to a model hides it too, so a retirement is a prompt to check, not a fact.

Exit codes of the script: `0` no changes, `3` changes found, `1` error. The workflow opens or updates the pull request only on `3`, and fails visibly on anything else. A vendor that errors fails the run, so nothing is pushed and the branch keeps its previous worksheet; rerun it from the Actions tab once the vendor recovers.

**Closing the watch pull request does not silence it.** While `providers/` still differs from what the vendors list, the next weekly run opens a new pull request with the same worksheets. To stop it, resolve the difference in `providers/` (add the models, or remove the retired ones), or remove that vendor's secret so the vendor is skipped.

## Alternatives rejected

- **Signing in CI.** It would put the production key, the one thing every Genie user trusts, in a hosted runner reachable by any workflow change or compromised action. A weekly publish is not worth that. The registry's whole trust model is that the key stays in Michel's custody.
- **Scraping vendor documentation pages alone.** Doc pages change layout without notice, are written for people, and lag or lead the API. The list endpoints are the vendors' own machine-readable answer to "what can this key call". The documentation is still the source for limits and geometry, which is why each worksheet links it for the human check.
- **Editing `providers/*.toml` directly in the pull request.** One careless merge would publish unverified limits in the next signed release. Worksheets keep verification a separate, deliberate human step.
- **A pull request per week.** Duplicates pile up; one regenerated branch and one updated pull request stay readable.

## One-time setup (Michel)

1. Allow the workflow token to open pull requests: repository **Settings → Actions → General → Workflow permissions**, tick **Allow GitHub Actions to create and approve pull requests**, and save. Leave the default permission at read; the job asks for its own. Without this tick the run pushes `model-watch/auto` and then fails with a message naming this setting. (As of 2026-11-12 the box is unticked on this repository.)
2. Recommended in the same page: tick **Require actions to be pinned to a full-length commit SHA**.
3. Create the keys:
   - **OpenAI:** platform.openai.com → API keys → Create a **restricted** key; set every permission to None except **Models: Read**.
   - **xAI:** console.x.ai → a team with a low spending limit (or a prepaid balance you are content to lose) → API key. If the console lets you restrict the key's endpoints, allow only model listing.
   - **Google:** aistudio.google.com → API key in a project with a low spending cap, or one without billing; restrict the key to the Generative Language API.
4. Add them as repository secrets (**Settings → Secrets and variables → Actions → New repository secret**), named exactly `OPENAI_MODELS_KEY`, `XAI_MODELS_KEY` and `GEMINI_MODELS_KEY`, or from a terminal, pasting the value at the prompt so it never enters shell history:

   ```sh
   gh secret set OPENAI_MODELS_KEY --repo michelabboud/genie-registry
   gh secret set XAI_MODELS_KEY --repo michelabboud/genie-registry
   gh secret set GEMINI_MODELS_KEY --repo michelabboud/genie-registry
   ```

   Any key may be left out; that vendor is skipped and the pull request and job summary say so.
5. Run it once by hand: **Actions → model-watch → Run workflow**, or `gh workflow run model-watch.yml --repo michelabboud/genie-registry`.

## Review, merge and publish

1. Open the watch pull request. Its table shows, per vendor, how many models were added, retired, unchanged and dropped by the id grammar, and which vendors were skipped.
2. Do not push to `model-watch/auto`; the watch regenerates it. Branch from it instead (`git fetch origin model-watch/auto && git switch -c models/<date> origin/model-watch/auto`). The watch never overwrites or closes a branch that carries anyone else's commit.
3. For each added model, open the linked vendor doc and check every `TO VERIFY` value: `max_n`, geometry, sizes or ratios/resolutions/pixels, qualities, formats and defaults. The values were copied from the last model in the same file only as a starting point.
4. Copy each verified block into the right `providers/<vendor>.toml`, remove the `TO VERIFY` comments, and update that file's `verified` date and `source` URLs. For a retired model, confirm the vendor's deprecation notice before removing its block.
5. Delete the `candidates/<vendor>.md` files you handled, so `candidates/` does not linger on `main`.
6. Open a pull request from your branch to `main`, review the diff, and merge. Close the watch pull request.
7. Publish on your machine with Genie's publisher (`scripts/registry_release.py` in the Genie repository; its runbook covers the dry run, the local signature and the upload of `registry/<N>`). Nothing in this repository's CI signs or publishes.

## Running and testing locally

```sh
python3 -m unittest discover -s tests -v       # fixtures only, no network, no keys
OPENAI_MODELS_KEY=... python3 scripts/model_watch.py --root .
```

The tests use fixtures shaped after each vendor's documented response (`tests/fixtures/`), cover a missing key, a key with a newline sent through the real request path, hostile ids, hostile JSON, retirement, the no-change path, and assert that a sentinel key value never reaches stdout, stderr or any written file. `tests/test_workflow_steps.py` extracts the pull-request job's shell steps from the workflow and runs them against a local bare repository with a stub `gh`: branch creation, a no-op rerun, the force-with-lease race, a human commit, skipped-vendor carry forward, the empty-commit guard and an `ls-remote` failure.

## References used for the endpoint shapes

- OpenAI, list models: https://platform.openai.com/docs/api-reference/models/list (also https://developers.openai.com/api/reference/resources/models/methods/list)
- xAI, models: https://docs.x.ai/developers/rest-api-reference/inference/models
- Google, models.list: https://ai.google.dev/api/models ; API-key header: https://aistudio.google.com/docs/api-key
