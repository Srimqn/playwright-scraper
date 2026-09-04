from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from scraper import PlaywrightScraper, ScrapeConfig, load_profiles


app = FastAPI(title="Authorized Scraper API")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "Invalid request data"})


class ScrapeRequest(BaseModel):
    url: HttpUrl
    wait_selector: str | None = None
    profiles_path: str | None = None


def _allowed_domains() -> set[str]:
    return {
        domain.strip().casefold().rstrip(".")
        for domain in os.getenv("SCRAPER_ALLOWED_DOMAINS", "").split(",")
        if domain.strip()
    }


def _require_api_key(api_key: str | None) -> None:
    configured_key = os.getenv("SCRAPER_API_KEY")
    if not configured_key or api_key != configured_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _check_allowed_domain(url: str) -> None:
    hostname = (urlparse(url).hostname or "").casefold().rstrip(".")
    if not hostname or hostname not in _allowed_domains():
        raise HTTPException(status_code=403, detail="Target domain is not allowed")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape")
def scrape(request: ScrapeRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    target_url = str(request.url)
    _check_allowed_domain(target_url)

    try:
        profiles = load_profiles(Path(request.profiles_path)) if request.profiles_path else {}
        result = PlaywrightScraper(
            ScrapeConfig(wait_selector=request.wait_selector, site_profiles=profiles)
        ).fetch(target_url)
    except HTTPException:
        raise
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"Invalid scraper configuration: {error}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="Scrape failed") from error

    if result.get("blocked") is not None:
        raise HTTPException(status_code=403, detail=result)
    return result
