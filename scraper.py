from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, sync_playwright


_MAX_CAPTURED_JSON_BYTES = 20_000  # payloads larger than this are almost always bundled libraries or widget configs, not content data
_BLOCKED_HTTP_STATUSES = {403, 429, 503}
_BLOCKER_TERMS = (
    "captcha",
    "cloudflare",
    "datadome",
    "access denied",
    "verify you are human",
    "unusual traffic",
)


@dataclass
class ScrapeConfig:
    storage_state_path: Path | None = None
    user_agent: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    proxy: dict[str, Any] | list[dict[str, Any]] | None = None
    block_resources: bool = True
    wait_selector: str | None = None
    site_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 6.0


class PlaywrightScraper:
    """Small, extensible browser scraper. Site-specific behavior belongs in profiles."""

    def __init__(self, config: ScrapeConfig | None = None) -> None:
        self.config = config or ScrapeConfig()
        if self.config.min_delay_seconds < 0 or self.config.max_delay_seconds < 0:
            raise ValueError("request delays must be nonnegative")
        if self.config.min_delay_seconds > self.config.max_delay_seconds:
            raise ValueError("min_delay_seconds must be less than or equal to max_delay_seconds")
        self._last_request_at: float | None = None

    def fetch(self, url: str) -> dict[str, Any]:
        self._rate_limit()
        proxy: Any = self._choose_proxy(self.config.proxy)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, proxy=proxy)
            try:
                return self._fetch_with_browser(browser, url)
            finally:
                browser.close()

    def _fetch_with_browser(self, browser: Browser, url: str) -> dict[str, Any]:
        context_options: dict[str, Any] = {}
        if self.config.storage_state_path and self.config.storage_state_path.exists():
            context_options["storage_state"] = str(self.config.storage_state_path)
        if self.config.user_agent:
            context_options["user_agent"] = self.config.user_agent
        if self.config.headers:
            context_options["extra_http_headers"] = self.config.headers

        context = browser.new_context(**context_options)
        page = context.new_page()
        captured_api_responses: list[dict[str, Any]] = []
        page.on("response", lambda response: self._capture_response(response, captured_api_responses))
        if self.config.block_resources:
            page.route("**/*", self._route_handler)

        try:
            response = page.goto(url, wait_until="domcontentloaded")
            http_status = response.status if response else None
            blocked = detect_blocker(http_status, page.title(), page.locator("body").inner_text(), page.url)
            if blocked is None:
                if self.config.wait_selector:
                    page.locator(self.config.wait_selector).wait_for(state="attached")
                else:
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except Exception:
                        pass
            html = page.content()
            profile = self.config.site_profiles.get(urlparse(page.url).netloc, {})
            result = parse_page(
                html,
                page.url,
                captured_api_responses,
                profile,
                blocked or detect_blocker(http_status, result_title(html), result_text(html), page.url),
            )
            if self.config.storage_state_path and result["blocked"] is None:
                self.config.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(self.config.storage_state_path))
            return result
        finally:
            context.close()

    def _route_handler(self, route: Any) -> None:
        if route.request.resource_type in {"image", "font", "stylesheet"}:
            route.abort()
        else:
            route.continue_()

    @staticmethod
    def _capture_response(response: Any, captured: list[dict[str, Any]]) -> None:
        resource_type = response.request.resource_type
        content_type = response.headers.get("content-type", "")
        if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
            return
        entry: dict[str, Any] = {
            "url": response.url,
            "status": response.status,
            "content_type": content_type,
        }
        if "json" in content_type and response.ok:
            try:
                payload = response.json()
                size = len(json.dumps(payload))
                if size > _MAX_CAPTURED_JSON_BYTES:
                    entry["json_truncated"] = True
                    entry["size_bytes"] = size
                else:
                    entry["json"] = payload
            except Exception:
                pass
        captured.append(entry)

    def _rate_limit(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            time.sleep(max(0, random.uniform(self.config.min_delay_seconds, self.config.max_delay_seconds) - elapsed))
        self._last_request_at = time.monotonic()

    @staticmethod
    def _choose_proxy(proxy: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any] | None:
        if isinstance(proxy, list):
            return random.choice(proxy) if proxy else None
        return proxy


def parse_page(
    html: str,
    url: str,
    captured_api_responses: list[dict[str, Any]],
    site_profile: dict[str, Any] | None = None,
    blocked: dict[str, Any] | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    profile = site_profile or {}
    content_soup = BeautifulSoup(str(soup), "html.parser")
    for selector in profile.get("_exclude", []):
        for tag in content_soup.select(selector):
            tag.decompose()
    result: dict[str, Any] = {
        "page_title": soup.title.get_text(strip=True) if soup.title else None,
        "meta_description": _meta_content(soup, "description"),
        "main_text": content_soup.get_text(" ", strip=True),
        "headings": [heading.get_text(" ", strip=True) for heading in soup.select("h1, h2, h3")],
        "all_links": [
            {"text": link.get_text(" ", strip=True), "href": link.get("href")}
            for link in soup.select("a[href]")
        ],
        "domain": urlparse(url).netloc,
        "scraped_at": datetime.now(UTC).isoformat(),
        "captured_api_responses": captured_api_responses,
        "blocked": blocked,
    }
    for field_name, selector in profile.items():
        if field_name == "_exclude":
            continue
        result[field_name] = [node.get_text(" ", strip=True) for node in soup.select(selector)]
    return result


def result_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(" ", strip=True) if soup.title else ""


def result_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def detect_blocker(
    http_status: int | None,
    title: str,
    text: str,
    url: str,
) -> dict[str, Any] | None:
    if http_status in _BLOCKED_HTTP_STATUSES:
        return {"reason": "http_status", "status": http_status, "url": url}
    combined = f"{title} {text}".casefold()
    for term in _BLOCKER_TERMS:
        if term in combined:
            return {"reason": "challenge_page", "indicator": term, "url": url}
    return None


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": name}) or soup.find(
        "meta", attrs={"property": f"og:{name}"}
    )
    content = tag.get("content") if tag else None
    return content if isinstance(content, str) else None


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Render and extract a web page with Playwright")
    parser.add_argument("url", help="URL to scrape; never hardcoded")
    parser.add_argument("--output", type=Path, default=Path("data/results.json"))
    parser.add_argument("--storage-state", type=Path, default=Path("data/storage_state.json"))
    parser.add_argument("--wait-selector")
    parser.add_argument("--user-agent")
    parser.add_argument("--header", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--profiles", type=Path, help="JSON mapping of domains to CSS selector profiles")
    parser.add_argument("--no-block-resources", action="store_true")
    args = parser.parse_args()

    try:
        headers = parse_headers(args.header)
        profiles = load_profiles(args.profiles)
        result = PlaywrightScraper(ScrapeConfig(
            storage_state_path=args.storage_state,
            user_agent=args.user_agent or os.getenv("SCRAPER_USER_AGENT"),
            headers=headers,
            site_profiles=profiles,
            wait_selector=args.wait_selector,
            block_resources=not args.no_block_resources,
        )).fetch(args.url)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Scrape failed: {error}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 3 if result["blocked"] else 0


def parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        if "=" not in value or not value.split("=", 1)[0].strip():
            raise ValueError(f"invalid header {value!r}; expected NAME=VALUE")
        name, header_value = value.split("=", 1)
        headers[name.strip()] = header_value
    return headers


def load_profiles(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    profiles = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(profiles, dict):
        raise ValueError("profiles JSON must contain an object")
    return profiles


if __name__ == "__main__":
    sys.exit(main())
