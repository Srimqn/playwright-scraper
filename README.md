# Playwright scraper

A small Python 3.11+ scraper designed to be learned and extended in stages. The URL is always supplied at runtime.

## Setup

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Stages

### 1. Base fetch

`PlaywrightScraper.fetch()` launches headless Chromium, opens a fresh context, navigates with `domcontentloaded`, then waits for either `--wait-selector` or a short `networkidle` window. `page.content()` returns the rendered DOM, including JavaScript-generated content.

The selector is preferable when the site has a stable “results loaded” element. Network idle is only a fallback because analytics and long polling can keep a page busy indefinitely.

### 2. Session handling

The context loads `storage_state` when `data/storage_state.json` exists and writes it after a successful run. This persists cookies and local storage. Treat that file as a credential: it is ignored by Git and should not be shared. A login wall still requires an intentional, interactive login flow to create the state file.

### 3. User-Agent and headers

Pass `--user-agent "..."` and repeat `--header Name=Value`. They become `new_context(user_agent=...)` and `extra_http_headers`. Headers are configuration, not a way to evade access controls.

### 4. Request interception

Images, fonts, and stylesheets are aborted by `page.route()` for speed. Disable this with `--no-block-resources` if the application needs CSS or image-triggered behavior. XHR/fetch and JSON responses are recorded in `captured_api_responses`, which lets you inspect whether a structured internal API is a better source than DOM parsing. Review API terms and authorization before using it as an extractor.

### 5. Parsing

BeautifulSoup parses the final HTML because the requested generic fields are document-shaped: title, metadata, text, headings, and links. Playwright locators are better when extraction must happen while the page is live or when interaction is needed before reading a value.

A domain profile can add structured fields without changing the scraper:

```python
profiles = {"example.com": {
	"_exclude": ["nav", "footer", ".cookie-banner"],
	"price": ".price",
	"author": "[data-author]",
}}
config = ScrapeConfig(site_profiles=profiles)
```

Profile values are lists of matching text values, which is safer than silently selecting only the first match.
The reserved `_exclude` list removes matching elements from `main_text` while leaving the other generic fields unchanged.

### 6. JSON output

The CLI writes one JSON object to `data/results.json` by default. It includes `page_title`, `meta_description`, `main_text`, `headings`, `all_links`, `domain`, `scraped_at`, and API response metadata/payloads when available.

## Run

```powershell
python scraper.py https://example.com --wait-selector "body" --user-agent "LearningScraper/1.0"
python scraper.py https://example.com --header "Accept-Language=en-US,en;q=0.9"
```

## HTTP API for n8n

Install the API dependencies from `requirements.txt`, then start locally with:

```powershell
$env:SCRAPER_API_KEY = "replace-with-a-private-key"
$env:SCRAPER_ALLOWED_DOMAINS = "example.com,www.elevancehealth.com"
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

On the Azure VM, run the same application under a service manager and bind it to `127.0.0.1`. Put NGINX with HTTPS in front of it; do not expose port `8000` publicly.

The n8n HTTP Request node should use `POST https://your-domain.example/scrape`, an `X-API-Key` header, and a JSON body such as:

```json
{
	"url": "https://example.com",
	"wait_selector": "body",
	"profiles_path": null
}
```

`SCRAPER_API_KEY` is required. `SCRAPER_ALLOWED_DOMAINS` is a comma-separated exact hostname allowlist; an empty value rejects every target. The API returns `403` for blocked pages or disallowed domains, `400` for invalid request data, and `500` for unexpected scrape failures.

## Protection and rate limits

The scraper waits a randomized 2–6 seconds between requests made by the same `PlaywrightScraper` instance. It does not bypass CAPTCHA, Cloudflare, DataDome, robots restrictions, login walls, or rate limits. If a run returns a challenge page, stop and inspect the site’s terms, official API, authentication flow, and permitted crawl rate. A proxy parameter already exists (`ScrapeConfig.proxy`) and accepts one Playwright proxy dict or a list to choose from later; supply only proxies you are authorized to use.
