"""
Core search logic: Google CSE, Playwright page loading, Claude API calls, caching.
"""

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_MODEL_EXTRACT = "claude-haiku-4-5-20251001"   # cheap model for role extraction
CLAUDE_MODEL_REFERRAL = "claude-sonnet-4-6"           # quality model for referral messages

FALLBACK_PLATFORMS = [
    {"name": "Greenhouse", "domain": "greenhouse.io"},
    {"name": "Lever",      "domain": "lever.co"},
    {"name": "Ashby",      "domain": "jobs.ashbyhq.com"},
]

# Clickable nav elements to look for on careers pages
CAREERS_NAV_KEYWORDS = [
    "jobs", "roles", "openings", "view all", "all departments",
    "engineering", "product", "design", "careers", "positions",
]

# Minimum useful page text length to bother calling Claude
MIN_USEFUL_CONTENT_CHARS = 500

# Keywords that indicate a paragraph is job-related (used for pre-filtering)
JOB_TEXT_KEYWORDS = {
    "apply", "role", "position", "experience", "requirements", "full-time",
    "part-time", "qualifications", "responsibilities", "salary", "remote",
    "hybrid", "location", "opening", "vacancy", "hire", "hiring",
}

# Max estimated tokens of filtered company text per Claude batch call
BATCH_TOKEN_BUDGET = 3500

CACHE_FILE = Path("cache.json")
CACHE_TTL_SECONDS = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Thread-local Playwright browser (one browser per worker thread)
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _get_browser():
    if not hasattr(_thread_local, "browser"):
        _thread_local._pw = sync_playwright().start()
        _thread_local.browser = _thread_local._pw.chromium.launch(headless=True)
    return _thread_local.browser


def _close_thread_browser():
    """Call at thread exit to cleanly shut down the browser."""
    if hasattr(_thread_local, "browser"):
        try:
            _thread_local.browser.close()
            _thread_local._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Disk cache (cache.json)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(data: dict):
    CACHE_FILE.write_text(json.dumps(data, indent=2))


def _cache_get(key: str):
    with _cache_lock:
        data = _load_cache()
        entry = data.get(key)
        if not entry:
            return None
        age = time.time() - entry.get("ts", 0)
        if age > CACHE_TTL_SECONDS:
            return None
        return entry.get("value")


def _cache_set(key: str, value):
    with _cache_lock:
        data = _load_cache()
        data[key] = {"value": value, "ts": time.time()}
        _save_cache(data)


def _page_cache_key(company: str) -> str:
    return f"page:{hashlib.sha256(company.lower().encode()).hexdigest()}"


def _claude_cache_key(company: str, job_title: str, text: str) -> str:
    raw = f"{company.lower()}|{job_title.lower()}|{text}"
    return f"claude:{hashlib.sha256(raw.encode()).hexdigest()}"


# ---------------------------------------------------------------------------
# Google Custom Search
# ---------------------------------------------------------------------------

def _cse_request(query: str, state: dict, lock: threading.Lock) -> list:
    """Run a Google Custom Search query, return list of result URLs."""
    api_key = os.environ["GOOGLE_API_KEY"]
    cse_id = os.environ["GOOGLE_CSE_ID"]

    today = datetime.now(timezone.utc).date().isoformat()
    with lock:
        if state.get("cse_date") != today:
            state["cse_date"] = today
            state["cse_calls_today"] = 0

    try:
        service = build("customsearch", "v1", developerKey=api_key)
        result = service.cse().list(q=query, cx=cse_id, num=3).execute()
        urls = [item["link"] for item in result.get("items", [])]
    except Exception as exc:
        urls = []
        # Log without exposing keys
        print(f"[CSE] Query failed: {type(exc).__name__}: {exc}")

    with lock:
        state["cse_calls_today"] = state.get("cse_calls_today", 0) + 1

    time.sleep(0.5)  # be polite to the free-tier rate limit
    return urls


def _tier1_google_cse(company: str, state: dict, lock: threading.Lock) -> list:
    query = f'"{company}" careers OR jobs'
    return _cse_request(query, state, lock)


def _tier2_platform_cse(company: str, job_title: str, state: dict, lock: threading.Lock) -> list:
    urls = []
    for platform in FALLBACK_PLATFORMS:
        query = f'"{company}" "{job_title}" site:{platform["domain"]}'
        results = _cse_request(query, state, lock)
        urls.extend(results)
    return urls


# ---------------------------------------------------------------------------
# Playwright page loading
# ---------------------------------------------------------------------------

def _visit_page_playwright(url: str) -> str:
    """Load a URL with a real browser, click relevant nav links, return body text."""
    browser = _get_browser()
    page = browser.new_page()
    try:
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # networkidle timeout is non-fatal; continue with what loaded

        # Look for nav elements pointing to job listings and click the best match
        for keyword in CAREERS_NAV_KEYWORDS:
            try:
                locator = page.get_by_role("link", name=re.compile(keyword, re.IGNORECASE))
                if locator.count() > 0:
                    locator.first.click()
                    page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        return page.inner_text("body") or ""
    except Exception as exc:
        print(f"[Playwright] Failed to load {url}: {type(exc).__name__}")
        return ""
    finally:
        try:
            page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Page text pre-filtering
# ---------------------------------------------------------------------------

def _filter_job_text(raw_text: str) -> str:
    """
    Keep only paragraphs that contain job-related keywords.
    Strips cookie banners, nav menus, footers, and legal boilerplate.
    """
    paragraphs = re.split(r"\n{2,}|\r\n{2,}", raw_text)
    kept = []
    for para in paragraphs:
        lower = para.lower()
        if any(kw in lower for kw in JOB_TEXT_KEYWORDS):
            # Drop very short noise lines
            if len(para.strip()) > 40:
                kept.append(para.strip())
    return "\n\n".join(kept)


# ---------------------------------------------------------------------------
# Local keyword pre-screen (skip Claude if clearly irrelevant)
# ---------------------------------------------------------------------------

def _text_likely_relevant(text: str, job_title: str) -> bool:
    """Quick check: does the text contain any word from the job title?"""
    title_words = {w.lower() for w in re.split(r"\W+", job_title) if len(w) > 2}
    lower_text = text.lower()
    return any(word in lower_text for word in title_words)


# ---------------------------------------------------------------------------
# Claude API calls
# ---------------------------------------------------------------------------

def _ask_claude_batch(
    company_texts: list[dict],
    job_title: str,
    location: str,
) -> dict[str, list]:
    """
    Send up to one batch of companies to Claude (Haiku) for role extraction.

    company_texts: list of {"company": str, "text": str}
    Returns: dict mapping company name -> list of {"role_title": str, "url": str}
    """
    client = anthropic.Anthropic()

    blocks = []
    for item in company_texts:
        blocks.append(
            f"### COMPANY: {item['company']}\n\n{item['text'][:2000]}"
        )

    combined = "\n\n---\n\n".join(blocks)

    prompt = (
        f"You are a job-search assistant. Below are careers page extracts from several companies.\n"
        f"The user is looking for open roles matching or similar to: **{job_title}**.\n"
        f"Location filter: only include roles that are based in, or explicitly open to remote "
        f"candidates in, **{location}**. Ignore roles that are EU-only, outside {location}, "
        f"or have no location information at all.\n\n"
        f"For each company, return a JSON object with the company name as key and a list of "
        f'matched roles as value. Each role: {{"role_title": str, "url": str}}. '
        f'If no match, use an empty list.\n\n'
        f"Return ONLY valid JSON. Example:\n"
        f'{{"Acme Corp": [{{"role_title": "Product Manager", "url": "https://..."}}], '
        f'"Beta Inc": []}}\n\n'
        f"Page extracts:\n\n{combined}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_EXTRACT,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as exc:
        print(f"[Claude] Batch extraction failed: {type(exc).__name__}: {exc}")
        return {}


def generate_referral_message(
    company: str,
    role_title: str,
    connection_name: str,
    connection_title: str,
) -> str:
    """Draft a personalised LinkedIn referral message using Claude Sonnet."""
    client = anthropic.Anthropic()
    prompt = (
        f"Write a warm, concise LinkedIn message (150 words max) asking "
        f"{connection_name} ({connection_title} at {company}) for a referral "
        f"for the role: {role_title} at {company}. "
        f"Be genuine and specific — reference the role and their position. "
        f"Do not include a subject line. Do not use placeholders."
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_REFERRAL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        return f"Could not generate message: {exc}"


# ---------------------------------------------------------------------------
# Per-company search (runs in a worker thread)
# ---------------------------------------------------------------------------

def search_company(
    company: str,
    connections: list[dict],
    job_title: str,
    location: str,
    state: dict,
    lock: threading.Lock,
) -> list[dict]:
    """
    Three-tier search for open roles at `company`.
    Returns list of result dicts; also updates shared `state` under `lock`.
    """
    results = []

    try:
        # --- Tier 1: find the company's own careers page ---
        page_key = _page_cache_key(company)
        cached_text = _cache_get(page_key)

        if cached_text:
            raw_text = cached_text
            tier = "Tier 1 (cached)"
        else:
            urls = _tier1_google_cse(company, state, lock)
            raw_text = ""
            tier = "Tier 1"
            for url in urls[:2]:
                raw_text = _visit_page_playwright(url)
                if raw_text:
                    break
            if raw_text:
                _cache_set(page_key, raw_text)

        filtered = _filter_job_text(raw_text)

        # Fall through to Tier 2 if content is too thin
        if len(filtered) < MIN_USEFUL_CONTENT_CHARS:
            urls2 = _tier2_platform_cse(company, job_title, state, lock)
            tier = "Tier 2"
            raw_text2 = ""
            for url in urls2[:3]:
                chunk = _visit_page_playwright(url)
                raw_text2 += "\n\n" + chunk
            filtered2 = _filter_job_text(raw_text2)
            if len(filtered2) >= len(filtered):
                filtered = filtered2

        # --- Local pre-screen: skip Claude if text is clearly irrelevant ---
        if len(filtered) < MIN_USEFUL_CONTENT_CHARS or not _text_likely_relevant(filtered, job_title):
            results = _build_rows(company, connections, [], tier + " — no match")
            return results

        # --- Claude extraction (with output cache) ---
        claude_key = _claude_cache_key(company, job_title, filtered)
        cached_roles = _cache_get(claude_key)

        if cached_roles is not None:
            matched_roles = cached_roles
        else:
            batch_result = _ask_claude_batch(
                [{"company": company, "text": filtered}],
                job_title,
                location,
            )
            matched_roles = batch_result.get(company, [])
            _cache_set(claude_key, matched_roles)

        if matched_roles:
            results = _build_rows(company, connections, matched_roles, tier)
        else:
            results = _build_rows(company, connections, [], tier + " — no match")

    except Exception as exc:
        print(f"[search] Error processing {company}: {type(exc).__name__}: {exc}")
        results = _build_rows(company, connections, [], "Error")
        with lock:
            state["errors"] = state.get("errors", 0) + 1

    finally:
        with lock:
            state["results"].extend(results)
            state["done"] += 1

    return results


def _build_rows(
    company: str,
    connections: list[dict],
    roles: list[dict],
    source: str,
) -> list[dict]:
    """Expand roles × connections into DataTable rows."""
    rows = []
    if not roles:
        for conn in connections:
            rows.append({
                "company": company,
                "role_title": "—",
                "connection_name": conn["name"],
                "connection_title": conn["title"],
                "source": source,
                "job_url": "",
            })
    else:
        for role in roles:
            for conn in connections:
                rows.append({
                    "company": company,
                    "role_title": role.get("role_title", ""),
                    "connection_name": conn["name"],
                    "connection_title": conn["title"],
                    "source": source,
                    "job_url": role.get("url", ""),
                })
    return rows


# ---------------------------------------------------------------------------
# Batch runner — called from app.py background thread
# ---------------------------------------------------------------------------

def run_search(
    company_map: dict[str, list[dict]],
    job_title: str,
    location: str,
    state: dict,
    lock: threading.Lock,
    max_workers: int = 3,
):
    """
    Process all companies concurrently. Batches Claude calls for efficiency.
    This function blocks until all companies are processed.
    """
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                search_company, company, connections, job_title, location, state, lock
            ): company
            for company, connections in company_map.items()
        }
        for future in concurrent.futures.as_completed(futures):
            company = futures[future]
            try:
                future.result()
            except Exception as exc:
                print(f"[runner] Unhandled error for {company}: {exc}")

    with lock:
        state["running"] = False
