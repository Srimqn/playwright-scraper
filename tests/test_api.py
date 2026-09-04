from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app)
        self.environment = patch.dict(
            os.environ,
            {
                "SCRAPER_API_KEY": "test-key",
                "SCRAPER_ALLOWED_DOMAINS": "example.com",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_missing_or_invalid_api_key(self) -> None:
        missing = self.client.post("/scrape", json={"url": "https://example.com"})
        invalid = self.client.post(
            "/scrape",
            headers={"X-API-Key": "wrong"},
            json={"url": "https://example.com"},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)

    def test_disallowed_domain(self) -> None:
        response = self.client.post(
            "/scrape",
            headers={"X-API-Key": "test-key"},
            json={"url": "https://not-example.com"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Target domain is not allowed")

    @patch.dict(os.environ, {"SCRAPER_API_KEY": "test-key", "SCRAPER_ALLOWED_DOMAINS": "example.com"})
    @patch("api.PlaywrightScraper")
    def test_valid_scrape_request(self, scraper_class) -> None:
        scraper_class.return_value.fetch.return_value = {
            "page_title": "Example",
            "blocked": None,
        }
        response = self.client.post(
            "/scrape",
            headers={"X-API-Key": "test-key"},
            json={"url": "https://example.com", "wait_selector": "body"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["page_title"], "Example")
        scraper_class.assert_called_once()
        scraper_class.return_value.fetch.assert_called_once_with("https://example.com/")

    @patch("api.PlaywrightScraper")
    def test_blocked_scrape_result(self, scraper_class) -> None:
        scraper_class.return_value.fetch.return_value = {
            "blocked": {"reason": "http_status", "status": 429}
        }
        response = self.client.post(
            "/scrape",
            headers={"X-API-Key": "test-key"},
            json={"url": "https://example.com"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["blocked"]["status"], 429)

    def test_invalid_request_data(self) -> None:
        response = self.client.post(
            "/scrape",
            headers={"X-API-Key": "test-key"},
            json={"url": "not-a-url"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
