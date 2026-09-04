from __future__ import annotations

import json
import subprocess
import sys
import unittest
from types import SimpleNamespace

from scraper import (
    PlaywrightScraper,
    _MAX_CAPTURED_JSON_BYTES,
    detect_blocker,
    parse_page,
    parse_headers,
)


class ScraperTests(unittest.TestCase):
    def test_normal_page_parsing(self) -> None:
        result = parse_page(
            "<html><head><title>Example</title></head><body><h1>Hello</h1><p>World</p></body></html>",
            "https://example.com/page",
            [],
        )
        self.assertEqual(result["page_title"], "Example")
        self.assertEqual(result["headings"], ["Hello"])
        self.assertIn("Hello World", result["main_text"])
        self.assertIsNone(result["blocked"])

    def test_open_graph_metadata_fallback(self) -> None:
        result = parse_page(
            '<meta property="og:description" content="A description">',
            "https://example.com",
            [],
        )
        self.assertEqual(result["meta_description"], "A description")

    def test_non_content_tags_are_removed(self) -> None:
        result = parse_page(
            "<script>secret_js</script><style>secret_css</style><noscript>secret_html</noscript><p>Visible</p>",
            "https://example.com",
            [],
        )
        self.assertEqual(result["main_text"], "Visible")

    def test_profile_exclusions_apply_to_main_text(self) -> None:
        result = parse_page(
            '<nav>Navigation</nav><main><p>Content</p></main><footer>Footer</footer>',
            "https://example.com",
            [],
            {"_exclude": ["nav", "footer"]},
        )
        self.assertEqual(result["main_text"], "Content")

    def test_json_payload_size_truncation(self) -> None:
        response = SimpleNamespace(
            request=SimpleNamespace(resource_type="xhr"),
            headers={"content-type": "application/json"},
            url="https://example.com/api",
            status=200,
            ok=True,
            json=lambda: {"value": "x" * _MAX_CAPTURED_JSON_BYTES},
        )
        captured: list[dict[str, object]] = []
        PlaywrightScraper._capture_response(response, captured)
        self.assertTrue(captured[0]["json_truncated"])
        self.assertNotIn("json", captured[0])

    def test_blocker_detection(self) -> None:
        self.assertEqual(
            detect_blocker(429, "", "", "https://example.com"),
            {"reason": "http_status", "status": 429, "url": "https://example.com"},
        )
        self.assertEqual(
            detect_blocker(None, "Access Denied", "", "https://example.com"),
            {"reason": "challenge_page", "indicator": "access denied", "url": "https://example.com"},
        )
        self.assertIsNone(detect_blocker(200, "Example", "Normal content", "https://example.com"))

    def test_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            PlaywrightScraper(type("Config", (), {
                "min_delay_seconds": 6,
                "max_delay_seconds": 2,
            })())
        with self.assertRaises(ValueError):
            parse_headers(["Accept-Language"])

    def test_invalid_cli_header_returns_clean_error(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scraper.py", "https://example.com", "--header", "InvalidHeader"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("expected NAME=VALUE", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_malformed_profile_json_returns_clean_error(self) -> None:
        profile_path = "tests/malformed-profile.json"
        with open(profile_path, "w", encoding="utf-8") as profile_file:
            profile_file.write("{invalid")
        try:
            completed = subprocess.run(
                [sys.executable, "scraper.py", "https://example.com", "--profiles", profile_path],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            import os
            os.remove(profile_path)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Configuration error", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
