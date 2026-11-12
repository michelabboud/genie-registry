"""Tests for scripts/model_watch.py. Fixtures only: no network, no keys.

The fixtures under tests/fixtures/ follow each vendor's documented list
response shape (see docs/model-watch.md for the reference URLs); they are not
captured from live accounts.
"""

from __future__ import annotations

import contextlib
import email.message
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("model_watch", REPO / "scripts" / "model_watch.py")
assert _spec is not None and _spec.loader is not None
mw = importlib.util.module_from_spec(_spec)
sys.modules["model_watch"] = mw  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(mw)

SENTINEL = "sk-SENTINEL-must-never-appear-0123456789abcdef"
ALL_KEYS = {
    "OPENAI_MODELS_KEY": SENTINEL + "-openai",
    "XAI_MODELS_KEY": SENTINEL + "-xai",
    "GEMINI_MODELS_KEY": SENTINEL + "-gemini",
}

OPENAI_TOML = """schema_version = 1
protocol = "openai-images"
base_url = "https://api.openai.com/v1"
source = ["https://platform.openai.com/docs/guides/image-generation"]
verified = "2026-10-03"

[models."gpt-image-1"]
max_n = 10
geometry = "pixels"
sizes = ["1024x1024", "1536x1024", "1024x1536"]
qualities = ["low", "medium", "high", "auto"]
default_quality = "auto"
formats = ["png", "jpeg", "webp"]
default_format = "png"
default_aspect = "1:1"

[models."gpt-image-2"]
max_n = 10
geometry = "pixels"
sizes = ["1024x1024", "1536x1024", "1024x1536"]
qualities = ["low", "medium", "high", "auto"]
default_quality = "auto"
formats = ["png", "jpeg", "webp"]
default_format = "png"
default_aspect = "1:1"
custom_sizes = { step = 16, max_edge = 3840, max_edge_ratio = 3, min_pixels = 655360, max_pixels = 8294400 }
"""

XAI_TOML = """schema_version = 1
protocol = "xai-images"
base_url = "https://api.x.ai/v1"
source = ["https://docs.x.ai/docs/guides/image-generation"]
verified = "2026-10-03"

[models."grok-imagine-image-2.0"]
max_n = 10
geometry = "ratio-resolution"
ratios = ["1:1", "16:9", "auto"]
resolutions = ["1k", "2k"]
qualities = ["low", "medium", "auto"]
default_quality = "auto"
formats = []
default_aspect = "auto"
default_resolution = "1k"
[models."grok-imagine-image-2.0".pixels]
"""

GEMINI_TOML = """schema_version = 1
protocol = "gemini-image"
base_url = "https://generativelanguage.googleapis.com/v1"
source = ["https://ai.google.dev/gemini-api/docs/image-generation"]
verified = "2026-10-03"

[models."gemini-3.1-flash-image"]
max_n = 1
geometry = "ratio-resolution"
ratios = ["1:1", "16:9"]
resolutions = ["1K"]
qualities = []
formats = []
default_aspect = "1:1"
default_resolution = "1K"
[models."gemini-3.1-flash-image".pixels]
"1:1/1K" = "1024x1024"
"16:9/1K" = "1376x768"

[models."gemini-2.0-flash-image"]
max_n = 1
geometry = "ratio-resolution"
ratios = ["1:1"]
resolutions = []
qualities = []
formats = []
default_aspect = "1:1"
[models."gemini-2.0-flash-image".pixels]
"1:1/" = "1024x1024"
"""


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransport:
    """Serves fixtures by URL and records every request it was given."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, headers: dict) -> object:
        self.calls.append((url, dict(headers)))
        if url not in self.routes:
            raise AssertionError(f"unexpected URL {url}")
        response = self.routes[url]
        if isinstance(response, Exception):
            raise response
        return response


GEMINI_PAGE2 = mw.VENDORS[2].list_url + "&pageToken=Cg5wYWdlLXR3by10b2tlbg%3D%3D"


def default_routes() -> dict:
    return {
        mw.VENDORS[0].list_url: fixture("openai_models.json"),
        mw.VENDORS[1].list_url: fixture("xai_image_models.json"),
        mw.VENDORS[2].list_url: fixture("gemini_models_page1.json"),
        GEMINI_PAGE2: fixture("gemini_models_page2.json"),
    }


class WatchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="model-watch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        providers = self.tmp / "providers"
        providers.mkdir()
        (providers / "openai.toml").write_text(OPENAI_TOML, encoding="utf-8")
        (providers / "xai.toml").write_text(XAI_TOML, encoding="utf-8")
        (providers / "gemini.toml").write_text(GEMINI_TOML, encoding="utf-8")

    def run_watch(self, env: dict, routes: dict | None = None):
        transport = FakeTransport(default_routes() if routes is None else routes)
        stdout, stderr = io.StringIO(), io.StringIO()
        body = self.tmp / "pr-body.md"
        summary = self.tmp / "summary.md"
        output = self.tmp / "github-output"
        argv = [
            "--root", str(self.tmp),
            "--pr-body", str(body),
            "--summary", str(summary),
            "--github-output", str(output),
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mw.run(argv, env=env, transport=transport)
        return code, stdout.getvalue(), stderr.getvalue(), transport

    def candidate(self, vendor: str) -> str:
        return (self.tmp / "candidates" / f"{vendor}.md").read_text(encoding="utf-8")

    def all_written_text(self) -> str:
        return "".join(
            path.read_text(encoding="utf-8")
            for path in self.tmp.rglob("*")
            if path.is_file()
        )


class VendorShapes(WatchCase):
    def test_openai_selects_by_name_pattern_and_reports_shutdown(self):
        code, out, _, _ = self.run_watch({"OPENAI_MODELS_KEY": "k"})
        self.assertEqual(code, mw.EXIT_CHANGES)
        text = self.candidate("openai")
        self.assertIn('[models."gpt-image-3"]', text)
        self.assertIn('[models."chatgpt-image-latest"]', text)
        self.assertNotIn("gpt-5.5", text)
        self.assertNotIn("dall-e-3", text)
        self.assertIn("`gpt-image-1`: 2027-03-01", text)
        self.assertNotIn("## Retired", text)
        self.assertIn("| OpenAI | checked | 2 | 0 | 2 | 0 |", out)

    def test_xai_selects_by_output_modalities_and_honours_aliases(self):
        code, out, _, _ = self.run_watch({"XAI_MODELS_KEY": "k"})
        self.assertEqual(code, mw.EXIT_CHANGES)
        text = self.candidate("xai")
        self.assertIn('[models."grok-imagine-image-3.0"]', text)
        self.assertNotIn("grok-imagine-video", text)
        self.assertIn("| xAI | checked | 1 | 0 | 1 | 0 |", out)

    def test_xai_without_modalities_falls_back_to_patterns(self):
        listing = mw.parse_xai(
            mw.VENDORS[1],
            [{"models": [{"id": "grok-imagine-image-9"}, {"id": "grok-9-mini"}]}],
        )
        self.assertEqual(listing.image_ids, {"grok-imagine-image-9"})
        self.assertEqual(listing.all_ids, {"grok-imagine-image-9", "grok-9-mini"})

    def test_gemini_paginates_requires_generate_content_and_detects_retired(self):
        code, out, _, transport = self.run_watch({"GEMINI_MODELS_KEY": "k"})
        self.assertEqual(code, mw.EXIT_CHANGES)
        self.assertEqual([url for url, _ in transport.calls], [mw.VENDORS[2].list_url, GEMINI_PAGE2])
        text = self.candidate("gemini")
        self.assertIn('[models."gemini-3.5-flash-image"]', text)
        self.assertNotIn("imagen", text)
        self.assertNotIn("image-embed", text)
        self.assertIn("## Retired", text)
        self.assertIn("`gemini-2.0-flash-image` is declared", text)
        self.assertIn("| Google Gemini | checked | 1 | 1 | 1 | 0 |", out)

    def test_gemini_page_limit_is_an_error_not_a_truncation(self):
        looping = {"models": [], "nextPageToken": "again"}
        routes = {mw.VENDORS[2].list_url: looping,
                  mw.VENDORS[2].list_url + "&pageToken=again": looping}
        code, _, err, _ = self.run_watch({"GEMINI_MODELS_KEY": "k"}, routes)
        self.assertEqual(code, mw.EXIT_ERROR)
        self.assertIn("exceeds 10 pages", err)
        self.assertFalse((self.tmp / "candidates").exists())

    def test_malformed_response_shape_is_an_error(self):
        routes = {mw.VENDORS[0].list_url: {"object": "list"}}
        code, _, err, _ = self.run_watch({"OPENAI_MODELS_KEY": "k"}, routes)
        self.assertEqual(code, mw.EXIT_ERROR)
        self.assertIn("no 'data' array", err)

    def test_oversized_listing_is_an_error(self):
        many = {"data": [{"id": f"m{i}"} for i in range(mw.MAX_LISTED_MODELS + 1)]}
        code, _, err, _ = self.run_watch({"OPENAI_MODELS_KEY": "k"}, {mw.VENDORS[0].list_url: many})
        self.assertEqual(code, mw.EXIT_ERROR)
        self.assertIn("exceeds", err)

    def test_requests_carry_the_key_in_the_documented_header_only(self):
        _, _, _, transport = self.run_watch(ALL_KEYS)
        seen = {url.split("?")[0]: headers for url, headers in transport.calls}
        self.assertEqual(seen["https://api.openai.com/v1/models"],
                         {"Authorization": "Bearer " + ALL_KEYS["OPENAI_MODELS_KEY"]})
        self.assertEqual(seen["https://api.x.ai/v1/image-generation-models"],
                         {"Authorization": "Bearer " + ALL_KEYS["XAI_MODELS_KEY"]})
        self.assertEqual(seen["https://generativelanguage.googleapis.com/v1beta/models"],
                         {"x-goog-api-key": ALL_KEYS["GEMINI_MODELS_KEY"]})
        for url, _ in transport.calls:
            self.assertNotIn(SENTINEL, url)


class MissingKeys(WatchCase):
    def test_missing_key_skips_vendor_and_says_so(self):
        code, out, _, transport = self.run_watch({"XAI_MODELS_KEY": "k"})
        self.assertEqual(code, mw.EXIT_CHANGES)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("OpenAI | skipped: OPENAI_MODELS_KEY is not set", out)
        self.assertIn("`GEMINI_MODELS_KEY` secret is absent", out)
        body = (self.tmp / "pr-body.md").read_text(encoding="utf-8")
        self.assertIn("`OPENAI_MODELS_KEY` secret is absent", body)
        summary = (self.tmp / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Google Gemini | skipped", summary)
        output = (self.tmp / "github-output").read_text()
        self.assertIn("complete=false", output)
        self.assertIn("skipped=openai gemini\n", output)

    def test_no_keys_at_all_is_no_change_and_incomplete(self):
        code, out, _, transport = self.run_watch({})
        self.assertEqual(code, mw.EXIT_NO_CHANGES)
        self.assertEqual(transport.calls, [])
        self.assertFalse((self.tmp / "pr-body.md").exists())
        self.assertIn("complete=false", (self.tmp / "github-output").read_text())


class HostileIds(WatchCase):
    def test_hostile_ids_are_dropped_counted_and_never_echoed(self):
        routes = {mw.VENDORS[0].list_url: fixture("openai_hostile.json")}
        code, out, err, _ = self.run_watch({"OPENAI_MODELS_KEY": "k"}, routes)
        # Only gpt-image-1 survives, and it is declared: retired gpt-image-2.
        self.assertEqual(code, mw.EXIT_CHANGES)
        self.assertIn("| OpenAI | checked | 0 | 1 | 1 | 10 |", out)
        written = self.all_written_text() + out + err
        for marker in ("Injected line", "evil.example", "**bold**", "`code`",
                       "../../providers", "-..-up", "XXXXXXXXXX", "ft:gpt", "12345"):
            self.assertNotIn(marker, written, marker)
        self.assertIn("10 listed ids failed the id grammar", out)

    def test_id_grammar(self):
        good = ["gpt-image-2", "gemini-3.1-flash-image", "grok_imagine.2", "a"]
        bad = ["", "a\nb", "a b", "../x", "a/b", "a..b", "-lead", ".lead", "a`b",
               "a]b", "a\"b", "é", "x" * (mw.MAX_MODEL_ID_LEN + 1), None, 7]
        for value in good:
            self.assertTrue(mw.valid_model_id(value), value)
        for value in bad:
            self.assertFalse(mw.valid_model_id(value), repr(value))
        self.assertTrue(mw.valid_model_id("x" * mw.MAX_MODEL_ID_LEN))

    def test_invalid_page_token_is_refused(self):
        routes = {mw.VENDORS[2].list_url: {"models": [], "nextPageToken": "a&key=x"}}
        code, _, err, _ = self.run_watch({"GEMINI_MODELS_KEY": "k"}, routes)
        self.assertEqual(code, mw.EXIT_ERROR)
        self.assertIn("invalid page token", err)


class ChangeDetection(WatchCase):
    def matching_routes(self) -> dict:
        return {
            mw.VENDORS[0].list_url: {"data": [{"id": "gpt-image-1"}, {"id": "gpt-image-2"}, {"id": "gpt-5"}]},
            mw.VENDORS[1].list_url: {"models": [{"id": "grok-imagine-image-2.0", "output_modalities": ["image"]}]},
            mw.VENDORS[2].list_url: {"models": [
                {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.0-flash-image", "supportedGenerationMethods": ["generateContent"]},
            ]},
        }

    def test_no_change_gives_no_candidates_and_no_body(self):
        code, out, _, _ = self.run_watch(ALL_KEYS, self.matching_routes())
        self.assertEqual(code, mw.EXIT_NO_CHANGES)
        self.assertFalse((self.tmp / "candidates").exists())
        self.assertFalse((self.tmp / "pr-body.md").exists())
        output = (self.tmp / "github-output").read_text()
        self.assertIn("complete=true", output)
        self.assertIn("skipped=\n", output)
        self.assertIn("| OpenAI | checked | 0 | 0 | 2 | 0 |", out)

    def test_stale_worksheet_is_removed_when_vendor_has_no_changes(self):
        (self.tmp / "candidates").mkdir()
        stale = self.tmp / "candidates" / "openai.md"
        stale.write_text("old", encoding="utf-8")
        self.run_watch(ALL_KEYS, self.matching_routes())
        self.assertFalse(stale.exists())

    def test_failed_vendor_keeps_its_previous_worksheet(self):
        (self.tmp / "candidates").mkdir()
        previous = self.tmp / "candidates" / "xai.md"
        previous.write_text("previous evidence", encoding="utf-8")
        routes = self.matching_routes()
        routes[mw.VENDORS[1].list_url] = mw.WatchError("HTTP 503")
        code, _, err, _ = self.run_watch(ALL_KEYS, routes)
        self.assertEqual(code, mw.EXIT_ERROR)
        self.assertIn("xAI failed: HTTP 503", err)
        self.assertEqual(previous.read_text(encoding="utf-8"), "previous evidence")

    def test_retired_model_detected(self):
        routes = self.matching_routes()
        routes[mw.VENDORS[0].list_url] = {"data": [{"id": "gpt-image-2"}]}
        code, _, _, _ = self.run_watch({"OPENAI_MODELS_KEY": "k"}, routes)
        self.assertEqual(code, mw.EXIT_CHANGES)
        text = self.candidate("openai")
        self.assertIn("## Retired", text)
        self.assertIn("`gpt-image-1` is declared", text)
        self.assertNotIn("## Added", text)

    def test_providers_are_never_modified(self):
        before = {p.name: p.read_bytes() for p in (self.tmp / "providers").iterdir()}
        self.run_watch(ALL_KEYS)
        after = {p.name: p.read_bytes() for p in (self.tmp / "providers").iterdir()}
        self.assertEqual(before, after)


class CandidateBlocks(WatchCase):
    def blocks(self, text: str) -> list[str]:
        parts = text.split("```toml\n")[1:]
        return [part.split("\n```", 1)[0] for part in parts]

    def test_blocks_parse_carry_every_required_field_and_mark_every_value(self):
        self.run_watch(ALL_KEYS)
        for vendor in ("openai", "xai", "gemini"):
            for block in self.blocks(self.candidate(vendor)):
                parsed = tomllib.loads(block)
                (model_id, model), = parsed["models"].items()
                self.assertTrue(mw.valid_model_id(model_id))
                required = {"max_n", "geometry", "qualities", "formats", "default_aspect"}
                required |= {"sizes"} if model["geometry"] == "pixels" else {"ratios", "resolutions", "pixels"}
                self.assertLessEqual(required, set(model), vendor)
                for line in block.splitlines():
                    if "=" in line and not line.startswith("["):
                        self.assertIn(mw.TO_VERIFY + ": https://", line)

    def test_blocks_seeded_from_the_real_registry_parse(self):
        for vendor in mw.VENDORS:
            document = mw.load_registry(REPO, vendor)
            _, block = mw.candidate_block(vendor, document, "new-model-1")
            self.assertIn("new-model-1", tomllib.loads(block)["models"])

    def test_empty_provider_falls_back_to_a_skeleton(self):
        document = {"protocol": "openai-images", "models": {}}
        header, block = mw.candidate_block(mw.VENDORS[0], document, "gpt-image-9")
        self.assertIn("Skeleton values", header)
        self.assertEqual(tomllib.loads(block)["models"]["gpt-image-9"]["geometry"], "pixels")


class SecretsNeverAppear(WatchCase):
    def assert_absent(self, out: str, err: str):
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn(SENTINEL, err)
        for path in self.tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL, path.read_text(encoding="utf-8", errors="replace"), path)

    def test_success_path(self):
        code, out, err, _ = self.run_watch(ALL_KEYS)
        self.assertEqual(code, mw.EXIT_CHANGES)
        self.assert_absent(out, err)

    def test_error_paths(self):
        routes = default_routes()
        for url in list(routes):
            routes[url] = mw.WatchError("HTTP 401")
        code, out, err, _ = self.run_watch(ALL_KEYS, routes)
        self.assertEqual(code, mw.EXIT_ERROR)
        self.assert_absent(out, err)

    def test_unexpected_exception_from_transport_is_not_swallowed_silently(self):
        # A programming error must surface rather than be reported as "no change".
        routes = default_routes()
        routes[mw.VENDORS[0].list_url] = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_watch(ALL_KEYS, routes)


class KeyHygiene(WatchCase):
    """A key pasted with stray whitespace or control bytes must never reach a
    log through http.client's header validation error."""

    def test_real_fetch_json_with_newline_key_never_echoes_it(self):
        # The real request path; .invalid never resolves, and http.client
        # validates the header before any connection is attempted.
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(mw.WatchError) as caught:
                mw.fetch_json("https://models.invalid/v1/models",
                              {"Authorization": "Bearer " + SENTINEL + "\n"})
        self.assertNotIn(SENTINEL, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue())

    def test_run_with_embedded_control_byte_key_refuses_without_network(self):
        env = {"OPENAI_MODELS_KEY": SENTINEL + "\nX"}
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--root", str(self.tmp)]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mw.run(argv, env=env)  # the real transport: it must not be reached
        self.assertEqual(code, mw.EXIT_ERROR)
        out = stdout.getvalue() + stderr.getvalue()
        self.assertIn("OPENAI_MODELS_KEY contains invalid characters", out)
        self.assertNotIn(SENTINEL, out)

    def test_surrounding_whitespace_is_stripped(self):
        _, _, _, transport = self.run_watch({"OPENAI_MODELS_KEY": "  key-1\r\n"})
        self.assertEqual(transport.calls[0][1], {"Authorization": "Bearer key-1"})

    def test_whitespace_only_key_counts_as_missing(self):
        code, out, _, transport = self.run_watch({"OPENAI_MODELS_KEY": " \n"})
        self.assertEqual(transport.calls, [])
        self.assertIn("OPENAI_MODELS_KEY is not set", out)


class HostileJson(unittest.TestCase):
    def serve(self, body: bytes):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self.close()

        class Opener:
            def open(self, request, timeout):
                return Response(body)

        return unittest.mock.patch.object(mw, "_OPENER", Opener())

    def test_deep_nesting_is_a_watch_error(self):
        with self.serve(b"[" * 100_000 + b"]" * 100_000):
            with self.assertRaises(mw.WatchError):
                mw.fetch_json("https://api.openai.com/v1/models", {})

    def test_huge_integer_is_a_watch_error(self):
        with self.serve(b'{"n": ' + b"9" * 5000 + b"}"):
            with self.assertRaises(mw.WatchError):
                mw.fetch_json("https://api.openai.com/v1/models", {})


class Transport(unittest.TestCase):
    def test_redirects_are_refused(self):
        handler = mw._RefuseRedirects()
        request = urllib.request.Request("https://api.openai.com/v1/models")
        with self.assertRaises(mw.WatchError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/")

    def test_non_https_url_is_refused(self):
        with self.assertRaises(mw.WatchError):
            mw.fetch_json("http://api.openai.com/v1/models", {"Authorization": "Bearer " + SENTINEL})

    def test_http_error_message_carries_status_only(self):
        body = io.BytesIO(("echo " + SENTINEL).encode())
        error = urllib.error.HTTPError("https://x.example", 401, "Unauthorized", email.message.Message(), body)

        class Opener:
            def open(self, request, timeout):
                raise error

        with unittest.mock.patch.object(mw, "_OPENER", Opener()):
            with self.assertRaises(mw.WatchError) as caught:
                mw.fetch_json("https://api.openai.com/v1/models", {"Authorization": "Bearer " + SENTINEL})
        self.assertEqual(str(caught.exception), "HTTP 401")
        self.assertIsNone(caught.exception.__cause__)

    def test_oversized_body_is_refused(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self.close()

        class Opener:
            def open(self, request, timeout):
                return Response(b"x" * (mw.MAX_RESPONSE_BYTES + 10))

        with unittest.mock.patch.object(mw, "_OPENER", Opener()):
            with self.assertRaises(mw.WatchError) as caught:
                mw.fetch_json("https://api.openai.com/v1/models", {})
        self.assertIn("larger than", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
