#!/usr/bin/env python3
"""Weekly vendor model watch for the Genie provider registry.

Lists each vendor's models through its official list endpoint, selects the
image-capable ones, compares them with ``providers/*.toml`` and writes
``candidates/<vendor>.md`` worksheets for a human to verify.

It never edits ``providers/``, never signs and never publishes: an unverified
value cannot reach a signed registry release through this script.

Security posture (see docs/model-watch.md):

* API keys come only from the environment, travel only in request headers,
  and are never printed, logged or written to a file.
* Vendor responses are untrusted. Model ids must match a strict grammar and
  length bound; anything else is dropped and only counted, never echoed.
* Redirects are refused, so a key header is never replayed to another host.

Exit codes: 0 no changes, 3 changes found, 1 error (any other code is also an
error to the workflow).

Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, TypeGuard

EXIT_NO_CHANGES = 0
EXIT_ERROR = 1
EXIT_CHANGES = 3

# Strict model id grammar: ASCII letters, digits, dot, underscore and hyphen,
# starting with a letter or digit. No slash, colon, whitespace, backtick or
# markdown/TOML metacharacter can pass, so an accepted id is safe inside a
# TOML quoted key, a markdown code span and a documentation URL path.
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_MODEL_ID_LEN = 100
# Upper bound on entries one vendor listing may contain. A larger listing is
# treated as an error rather than truncated: retired detection on a partial
# list would report models as retired that are merely past the cut.
MAX_LISTED_MODELS = 2000
# Genie admits at most 64 models per provider file; more candidates than that
# in one week cannot be added anyway, so the rest are dropped and counted.
MAX_CANDIDATES_PER_VENDOR = 64
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10
REQUEST_TIMEOUT_SECONDS = 30
# A usable API key is visible ASCII only; anything else would be rejected by
# http.client with an error message that contains the key.
KEY_RE = re.compile(r"[\x21-\x7e]+")
PAGE_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-=.~+/]{1,512}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
USER_AGENT = "genie-registry-model-watch/1"
DESCRIPTION = "Weekly vendor model watch for the Genie provider registry."
TO_VERIFY = "TO VERIFY"


class WatchError(Exception):
    """A vendor check failed. The message is safe to print: it never carries a
    key, a header or raw response text."""


Transport = Callable[[str, dict], object]


@dataclass(frozen=True)
class Vendor:
    """One vendor's watch policy. Every vendor-specific fact lives here."""

    name: str
    display: str
    env_key: str
    provider_file: str
    list_url: str
    auth_header: str
    auth_prefix: str
    # Fallback name patterns, used only where the vendor exposes no
    # documented capability field for image output.
    patterns: tuple[str, ...]
    doc_urls: tuple[str, ...]
    model_doc_template: str | None
    key_advice: str


VENDORS: tuple[Vendor, ...] = (
    Vendor(
        name="openai",
        display="OpenAI",
        env_key="OPENAI_MODELS_KEY",
        provider_file="providers/openai.toml",
        list_url="https://api.openai.com/v1/models",
        auth_header="Authorization",
        auth_prefix="Bearer ",
        # GET /v1/models documents only id, object, created, owned_by and
        # shutdown_date: no capability field, so image models go by name.
        # dall-e-* is deliberately absent: Genie does not declare it.
        patterns=(r"gpt-image-.*", r"chatgpt-image-.*"),
        doc_urls=(
            "https://platform.openai.com/docs/guides/image-generation",
            "https://platform.openai.com/docs/api-reference/images/create",
        ),
        model_doc_template="https://platform.openai.com/docs/models/{id}",
        key_advice="a restricted key with only Models: Read",
    ),
    Vendor(
        name="xai",
        display="xAI",
        env_key="XAI_MODELS_KEY",
        provider_file="providers/xai.toml",
        list_url="https://api.x.ai/v1/image-generation-models",
        auth_header="Authorization",
        auth_prefix="Bearer ",
        # Used only when a listed model has no output_modalities field.
        patterns=(r"grok-.*image.*",),
        doc_urls=(
            "https://docs.x.ai/docs/guides/image-generation",
            "https://docs.x.ai/developers/rest-api-reference/inference/models",
        ),
        model_doc_template=None,
        key_advice="a key on a team with a low spending cap",
    ),
    Vendor(
        name="gemini",
        display="Google Gemini",
        env_key="GEMINI_MODELS_KEY",
        provider_file="providers/gemini.toml",
        list_url="https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        auth_header="x-goog-api-key",
        auth_prefix="",
        # The Model resource has supportedGenerationMethods but no output
        # modality, so the name decides; generateContent support is required
        # too, because Genie's gemini-image protocol calls generateContent.
        patterns=(r"gemini-.*image.*",),
        doc_urls=(
            "https://ai.google.dev/gemini-api/docs/image-generation",
            "https://ai.google.dev/gemini-api/docs/models",
        ),
        model_doc_template=None,
        key_advice="a key on a project with a low spending cap",
    ),
)


@dataclass
class Listing:
    """Validated result of one vendor listing."""

    image_ids: set[str] = field(default_factory=set)
    # Every validated id and alias seen, image or not: a registry model is
    # retired only when it appears nowhere in the listing.
    all_ids: set[str] = field(default_factory=set)
    shutdown_dates: dict[str, str] = field(default_factory=dict)
    dropped: int = 0


@dataclass
class VendorResult:
    vendor: Vendor
    status: str  # "checked" | "skipped" | "error"
    message: str = ""
    added: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    shutdown: dict[str, str] = field(default_factory=dict)
    dropped: int = 0
    overflow: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.retired)


# ---------------------------------------------------------------- transport


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib replays request headers, Authorization included, to a redirect
    target. A list endpoint has no reason to redirect, so refuse."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise WatchError(f"refused HTTP {code} redirect")


_OPENER = urllib.request.build_opener(_RefuseRedirects())


def fetch_json(url: str, headers: dict) -> object:
    """GET ``url`` over HTTPS with ``headers`` and parse a bounded JSON body.

    Errors name the HTTP status or the failure class only; a response body,
    which could reflect request data, is never included."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise WatchError("refused non-HTTPS URL")
    request = urllib.request.Request(url, method="GET")
    for name, value in headers.items():
        request.add_header(name, value)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except WatchError:
        raise
    except urllib.error.HTTPError as error:
        raise WatchError(f"HTTP {error.code}") from None
    except urllib.error.URLError as error:
        reason = type(error.reason).__name__ if not isinstance(error.reason, str) else "URLError"
        raise WatchError(f"network error ({reason})") from None
    except TimeoutError:
        raise WatchError("request timed out") from None
    except http.client.HTTPException as error:
        raise WatchError(f"protocol error ({type(error).__name__})") from None
    except OSError as error:
        raise WatchError(f"network error ({type(error).__name__})") from None
    except ValueError:
        # http.client rejects an invalid header value with a ValueError whose
        # text contains the whole header, key included. Never let it through.
        raise WatchError("invalid request") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise WatchError(f"response larger than {MAX_RESPONSE_BYTES} bytes")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError, UnicodeDecodeError and integers
        # past the int-to-str digit limit; RecursionError covers deep nesting.
        raise WatchError("response is not valid JSON") from None


# --------------------------------------------------------------- validation


def valid_model_id(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_MODEL_ID_LEN
        and MODEL_ID_RE.fullmatch(value) is not None
        and ".." not in value
    )


def matches_patterns(vendor: Vendor, model_id: str) -> bool:
    return any(re.fullmatch(pattern, model_id) for pattern in vendor.patterns)


def _require_list(container: object, key: str) -> list:
    if not isinstance(container, dict) or not isinstance(container.get(key), list):
        raise WatchError(f"response has no '{key}' array")
    return container[key]


def _admit(listing: Listing, raw_id: object) -> str | None:
    if valid_model_id(raw_id):
        listing.all_ids.add(raw_id)
        return raw_id
    listing.dropped += 1
    return None


def _check_count(count: int) -> None:
    if count > MAX_LISTED_MODELS:
        raise WatchError(f"listing exceeds {MAX_LISTED_MODELS} entries")


def parse_openai(vendor: Vendor, payloads: list[object]) -> Listing:
    listing = Listing()
    entries = _require_list(payloads[0], "data")
    _check_count(len(entries))
    for entry in entries:
        model_id = _admit(listing, entry.get("id") if isinstance(entry, dict) else None)
        if model_id is None:
            continue
        if matches_patterns(vendor, model_id):
            listing.image_ids.add(model_id)
        shutdown = entry.get("shutdown_date")
        if isinstance(shutdown, str) and DATE_RE.fullmatch(shutdown):
            listing.shutdown_dates[model_id] = shutdown
    return listing


def parse_xai(vendor: Vendor, payloads: list[object]) -> Listing:
    listing = Listing()
    entries = _require_list(payloads[0], "models")
    _check_count(len(entries))
    for entry in entries:
        if not isinstance(entry, dict):
            listing.dropped += 1
            continue
        model_id = _admit(listing, entry.get("id"))
        if model_id is None:
            continue
        aliases = entry.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases[:16]:
                _admit(listing, alias)
        outputs = entry.get("output_modalities")
        if isinstance(outputs, list):
            image = "image" in outputs
        else:
            image = matches_patterns(vendor, model_id)
        if image:
            listing.image_ids.add(model_id)
    return listing


def parse_gemini(vendor: Vendor, payloads: list[object]) -> Listing:
    listing = Listing()
    entries: list = []
    for payload in payloads:
        entries.extend(_require_list(payload, "models"))
        _check_count(len(entries))
    for entry in entries:
        if not isinstance(entry, dict):
            listing.dropped += 1
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.startswith("models/"):
            name = name[len("models/"):]
        else:
            name = None
        model_id = _admit(listing, name)
        if model_id is None:
            continue
        methods = entry.get("supportedGenerationMethods")
        generates = isinstance(methods, list) and "generateContent" in methods
        if generates and matches_patterns(vendor, model_id):
            listing.image_ids.add(model_id)
    return listing


PARSERS = {"openai": parse_openai, "xai": parse_xai, "gemini": parse_gemini}


def fetch_pages(vendor: Vendor, key: str, transport: Transport) -> list[object]:
    headers = {vendor.auth_header: vendor.auth_prefix + key}
    payloads = [transport(vendor.list_url, headers)]
    if vendor.name != "gemini":
        return payloads
    for _ in range(MAX_PAGES - 1):
        token = payloads[-1].get("nextPageToken") if isinstance(payloads[-1], dict) else None
        if not token:
            return payloads
        if not isinstance(token, str) or not PAGE_TOKEN_RE.fullmatch(token):
            raise WatchError("invalid page token in response")
        url = vendor.list_url + "&pageToken=" + urllib.parse.quote(token, safe="")
        payloads.append(transport(url, headers))
    if isinstance(payloads[-1], dict) and payloads[-1].get("nextPageToken"):
        raise WatchError(f"listing exceeds {MAX_PAGES} pages")
    return payloads


# ----------------------------------------------------------------- registry


def load_registry(root: Path, vendor: Vendor) -> dict:
    path = root / vendor.provider_file
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError:
        raise WatchError(f"{vendor.provider_file} is missing") from None
    except tomllib.TOMLDecodeError:
        raise WatchError(f"{vendor.provider_file} is not valid TOML") from None
    models = document.get("models")
    if not isinstance(models, dict):
        raise WatchError(f"{vendor.provider_file} has no models table")
    return document


def check_vendor(root: Path, vendor: Vendor, env: dict, transport: Transport) -> VendorResult:
    key = env.get(vendor.env_key, "").strip()
    if not key:
        return VendorResult(vendor, "skipped", f"{vendor.env_key} is not set")
    if not KEY_RE.fullmatch(key):
        # Name the secret, never its value.
        return VendorResult(vendor, "error", f"{vendor.env_key} contains invalid characters")
    try:
        document = load_registry(root, vendor)
        listing = PARSERS[vendor.name](vendor, fetch_pages(vendor, key, transport))
    except WatchError as error:
        return VendorResult(vendor, "error", str(error))
    registry_ids = set(document["models"])
    added = sorted(listing.image_ids - registry_ids)
    overflow = max(0, len(added) - MAX_CANDIDATES_PER_VENDOR)
    return VendorResult(
        vendor,
        "checked",
        added=added[:MAX_CANDIDATES_PER_VENDOR],
        retired=sorted(registry_ids - listing.all_ids),
        unchanged=sorted(registry_ids & listing.all_ids),
        shutdown={i: d for i, d in listing.shutdown_dates.items() if i in registry_ids},
        dropped=listing.dropped,
        overflow=overflow,
    )


# ------------------------------------------------------------------ output


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"unsupported TOML value {type(value).__name__}")


# Required fields per geometry, from the Genie loader (ModelSpecDraft): a
# skeleton used when the provider file has no model to seed values from.
SKELETONS = {
    "openai-images": {
        "max_n": 1,
        "geometry": "pixels",
        "sizes": ["1024x1024"],
        "qualities": [],
        "formats": [],
        "default_aspect": "1:1",
    },
    "ratio-resolution": {
        "max_n": 1,
        "geometry": "ratio-resolution",
        "ratios": ["1:1"],
        "resolutions": [],
        "qualities": [],
        "formats": [],
        "default_aspect": "1:1",
        "pixels": {"1:1/": "1024x1024"},
    },
}


def seed_model(document: dict) -> tuple[str | None, dict]:
    """The last declared model is the proposal's starting point; every value
    is still marked TO VERIFY because a sibling's limits are not evidence."""
    models = document.get("models") or {}
    if models:
        name = list(models)[-1]
        return name, models[name]
    key = "openai-images" if document.get("protocol") == "openai-images" else "ratio-resolution"
    return None, SKELETONS[key]


def verify_url(vendor: Vendor, model_id: str) -> str:
    if vendor.model_doc_template:
        return vendor.model_doc_template.format(id=model_id)
    return vendor.doc_urls[0]


def candidate_block(vendor: Vendor, document: dict, model_id: str) -> tuple[str, str]:
    seed_name, seed = seed_model(document)
    url = verify_url(vendor, model_id)
    note = f"  # {TO_VERIFY}: {url}"
    lines = [f'[models."{model_id}"]']
    tables = []
    for key, value in seed.items():
        if isinstance(value, dict) and key == "pixels":
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_toml_value(value)}{note}")
    for key, value in tables:
        lines.append(f'[models."{model_id}".{key}]')
        for pair, size in value.items():
            lines.append(f"{_toml_value(pair)} = {_toml_value(size)}{note}")
    header = (
        f"Values seeded from `{seed_name}`; " if seed_name else "Skeleton values; "
    ) + "none of them is verified for this model."
    return header, "\n".join(lines)


def candidate_markdown(result: VendorResult, document: dict) -> str:
    vendor = result.vendor
    out = [
        f"# {vendor.display} model candidates",
        "",
        "Generated by the weekly model watch. **Nothing here is verified.** Each",
        f"value is marked {TO_VERIFY}; check it against the vendor documentation,",
        f"copy the verified block into `{vendor.provider_file}` by hand, update",
        "`verified` and `source` there, then delete this file before merging.",
        "",
        "Documentation to check:",
        "",
    ]
    out += [f"- {url}" for url in vendor.doc_urls]
    out.append("")
    if result.added:
        out += ["## Added", ""]
        for model_id in result.added:
            header, block = candidate_block(vendor, document, model_id)
            out += [
                f"### `{model_id}`",
                "",
                f"Listed by the vendor as image-capable and absent from `{vendor.provider_file}`.",
                header,
                "",
                "```toml",
                block,
                "```",
                "",
            ]
    if result.overflow:
        out += [f"{result.overflow} further added models were dropped (cap {MAX_CANDIDATES_PER_VENDOR}).", ""]
    if result.retired:
        out += ["## Retired", ""]
        for model_id in result.retired:
            out.append(
                f"- `{model_id}` is declared in `{vendor.provider_file}` but no longer listed. "
                "Confirm the retirement in the vendor's deprecation notes before removing it; "
                "a key without access to a model also hides it."
            )
        out.append("")
    if result.shutdown:
        out += ["## Announced shutdown dates", ""]
        out += [f"- `{i}`: {d}" for i, d in sorted(result.shutdown.items())]
        out.append("")
    return "\n".join(out)


def report(results: Iterable[VendorResult]) -> str:
    rows = [
        "| Vendor | Status | Added | Retired | Unchanged | Dropped ids |",
        "|---|---|---|---|---|---|",
    ]
    notes = []
    for r in results:
        if r.status == "checked":
            rows.append(
                f"| {r.vendor.display} | checked | {len(r.added) + r.overflow} | "
                f"{len(r.retired)} | {len(r.unchanged)} | {r.dropped} |"
            )
        else:
            rows.append(f"| {r.vendor.display} | {r.status}: {r.message} | - | - | - | - |")
            if r.status == "skipped":
                notes.append(
                    f"- {r.vendor.display} was not checked: the `{r.vendor.env_key}` secret is absent."
                )
        if r.dropped:
            notes.append(
                f"- {r.vendor.display}: {r.dropped} listed ids failed the id grammar and were dropped unread."
            )
    return "\n".join(rows + ([""] + notes if notes else []))


def pr_body(results: list[VendorResult]) -> str:
    changed = [r for r in results if r.changed]
    lines = [
        "Automated weekly model watch. **Unverified:** every proposed value is marked",
        f"{TO_VERIFY}. This pull request changes only `candidates/`; it never edits",
        "`providers/`, never signs and never publishes.",
        "",
        report(results),
        "",
        "Worksheets:",
        "",
    ]
    lines += [f"- `candidates/{r.vendor.name}.md`" for r in changed]
    lines += [
        "",
        "Review and publish: follow `docs/model-watch.md` (verify each value against the",
        "vendor docs, move verified blocks into `providers/*.toml`, delete the worksheet,",
        "merge, then sign and publish locally with Genie's `scripts/registry_release.py`).",
        "",
        "Do not push your own commits to `model-watch/auto`: the watch regenerates it.",
        "Branch from it instead; the watch then leaves the branch alone.",
    ]
    return "\n".join(lines) + "\n"


def write_candidates(root: Path, results: list[VendorResult]) -> list[Path]:
    """Rewrite candidates/ for every checked vendor: a worksheet where there
    are changes, none where there are not. Skipped or failed vendors' files
    are left as they are in this tree, because no fresh evidence replaces
    them; the workflow carries a skipped vendor's worksheet forward from the
    existing model-watch/auto branch (a failed vendor fails the run, so
    nothing is pushed)."""
    directory = root / "candidates"
    written = []
    for result in results:
        if result.status != "checked":
            continue
        path = directory / f"{result.vendor.name}.md"
        if result.changed:
            directory.mkdir(exist_ok=True)
            document = load_registry(root, result.vendor)
            path.write_text(candidate_markdown(result, document) + "\n", encoding="utf-8")
            written.append(path)
        elif path.exists():
            path.unlink()
    return written


def run(argv: list[str], env: dict | None = None, transport: Transport = fetch_json) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--pr-body", type=Path, help="write the pull request body here")
    parser.add_argument("--summary", type=Path, help="append a markdown job summary here")
    parser.add_argument("--github-output", type=Path, help="append step outputs here")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env

    results = [check_vendor(args.root, vendor, env, transport) for vendor in VENDORS]
    try:
        write_candidates(args.root, results)
    except (OSError, WatchError) as error:
        print(f"model-watch: cannot write candidates: {type(error).__name__}", file=sys.stderr)
        return EXIT_ERROR

    text = report(results)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write("## Model watch\n\n" + text + "\n")
    errors = [r for r in results if r.status == "error"]
    changed = any(r.changed for r in results)
    complete = all(r.status == "checked" for r in results)
    if args.pr_body and changed:
        args.pr_body.write_text(pr_body(results), encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"complete={'true' if complete else 'false'}\n")
            skipped = " ".join(r.vendor.name for r in results if r.status == "skipped")
            handle.write(f"skipped={skipped}\n")
    for r in errors:
        print(f"model-watch: {r.vendor.display} failed: {r.message}", file=sys.stderr)
    if errors:
        return EXIT_ERROR
    return EXIT_CHANGES if changed else EXIT_NO_CHANGES


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
