"""
Core search logic: direct URL probing, Playwright page loading, Claude API calls, caching.
"""

import hashlib
import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_MODEL_EXTRACT  = "claude-haiku-4-5-20251001"  # cheap model for role extraction
CLAUDE_MODEL_REFERRAL = "claude-sonnet-4-6"           # quality model for referral messages

# Common corporate suffixes to strip when deriving a domain slug from a company name
_CORP_SUFFIXES = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|corporation|company|group|holdings?|"
    r"international|technologies?|solutions?|services?|systems?|enterprises?|"
    r"ventures?|labs?)\b",
    re.IGNORECASE,
)

# Clickable nav elements to look for on careers pages
CAREERS_NAV_KEYWORDS = [
    "jobs", "roles", "openings", "view all", "all departments",
    "engineering", "product", "design", "careers", "positions",
]

MIN_USEFUL_CONTENT_CHARS = 500

# Keywords that indicate a paragraph is job-related (used for pre-filtering)
JOB_TEXT_KEYWORDS = {
    "apply", "role", "position", "experience", "requirements", "full-time",
    "part-time", "qualifications", "responsibilities", "salary", "remote",
    "hybrid", "location", "opening", "vacancy", "hire", "hiring",
}

CACHE_FILE        = Path("cache.json")
CACHE_TTL_SECONDS = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Single shared Playwright browser (one instance for the whole process)
# ---------------------------------------------------------------------------

_browser_lock        = threading.Lock()
_browser             = None
_playwright_instance = None


def _get_browser():
    global _browser, _playwright_instance
    if _browser is None:
        with _browser_lock:
            if _browser is None:  # double-checked locking
                _playwright_instance = sync_playwright().start()
                _browser = _playwright_instance.chromium.launch(headless=True)
    return _browser


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
        data  = _load_cache()
        entry = data.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > CACHE_TTL_SECONDS:
            return None
        return entry.get("value")


def _cache_set(key: str, value):
    with _cache_lock:
        data      = _load_cache()
        data[key] = {"value": value, "ts": time.time()}
        _save_cache(data)


def _page_cache_key(company: str) -> str:
    return f"page:{hashlib.sha256(company.lower().encode()).hexdigest()}"


def _claude_cache_key(company: str, job_titles: list[str], text: str) -> str:
    raw = f"{company.lower()}|{'|'.join(sorted(job_titles))}|{text}"
    return f"claude:{hashlib.sha256(raw.encode()).hexdigest()}"


def _synonyms_cache_key(job_title: str) -> str:
    return f"synonyms:{hashlib.sha256(job_title.lower().encode()).hexdigest()}"


# ---------------------------------------------------------------------------
# URL pattern generation
# ---------------------------------------------------------------------------

def _make_slugs(company: str) -> list[str]:
    """Derive likely domain slugs from a company name."""
    cleaned     = _CORP_SUFFIXES.sub("", company)
    cleaned     = re.sub(r"[^a-zA-Z0-9\s-]", "", cleaned).strip().lower()
    slug_plain  = re.sub(r"[\s-]+", "", cleaned)
    slug_hyphen = re.sub(r"\s+", "-", cleaned).strip("-")
    seen = []
    for s in [slug_plain, slug_hyphen]:
        if s and s not in seen:
            seen.append(s)
    return seen


def _tier1_url_patterns(company: str) -> list[str]:
    urls = []
    for slug in _make_slugs(company):
        urls += [
            f"https://{slug}.com/careers",
            f"https://{slug}.com/jobs",
            f"https://careers.{slug}.com",
            f"https://jobs.{slug}.com",
            f"https://{slug}.com/work-with-us",
            f"https://{slug}.com/join-us",
        ]
    return urls


def _tier2_url_patterns(company: str) -> list[str]:
    urls = []
    for slug in _make_slugs(company):
        urls += [
            f"https://boards.greenhouse.io/{slug}",
            f"https://job-boards.greenhouse.io/{slug}",
            f"https://jobs.lever.co/{slug}",
            f"https://jobs.ashbyhq.com/{slug}",
        ]
    return urls


# ---------------------------------------------------------------------------
# Fast HTTP pre-check (stdlib only, no extra dependency)
# ---------------------------------------------------------------------------

def _url_likely_exists(url: str) -> bool:
    """
    Quick HEAD request to confirm a URL is reachable before spending time
    on a full Playwright load. Returns False on 4xx/5xx or any network error.
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; warm-job-radar)")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Playwright page loading (shared browser, per-request context)
# ---------------------------------------------------------------------------

def _visit_page_playwright(url: str, timeout: int = 15000) -> str:
    """
    Load a URL inside its own browser context (thread-safe), click any
    relevant nav links, and return the full body text.
    """
    browser = _get_browser()
    context = browser.new_context()
    page    = context.new_page()
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # non-fatal; use whatever has loaded so far

        for keyword in CAREERS_NAV_KEYWORDS:
            try:
                locator = page.get_by_role("link", name=re.compile(keyword, re.IGNORECASE))
                if locator.count() > 0:
                    locator.first.click()
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        return page.inner_text("body") or ""
    except Exception as exc:
        print(f"[Playwright] {url}: {type(exc).__name__}")
        return ""
    finally:
        try:
            context.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Page text pre-filtering
# ---------------------------------------------------------------------------

def _filter_job_text(raw_text: str) -> str:
    """Keep only paragraphs that contain job-related keywords."""
    paragraphs = re.split(r"\n{2,}|\r\n{2,}", raw_text)
    kept = [
        p.strip() for p in paragraphs
        if len(p.strip()) > 40 and any(kw in p.lower() for kw in JOB_TEXT_KEYWORDS)
    ]
    return "\n\n".join(kept)


# ---------------------------------------------------------------------------
# Local keyword pre-screen
# ---------------------------------------------------------------------------

def _text_likely_relevant(text: str, job_titles: list[str]) -> bool:
    """Return True if the text contains any word from any of the job title variants."""
    all_words = set()
    for title in job_titles:
        all_words |= {w.lower() for w in re.split(r"\W+", title) if len(w) > 2}
    lower_text = text.lower()
    return any(word in lower_text for word in all_words)


# ---------------------------------------------------------------------------
# Job title synonym expansion
# ---------------------------------------------------------------------------

def expand_job_title(job_title: str) -> list[str]:
    """
    Call Claude Haiku once to produce 6-8 synonym/variant job titles.
    Results are cached for 24h so repeat searches cost nothing.
    """
    cache_key = _synonyms_cache_key(job_title)
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = anthropic.Anthropic()
    prompt = (
        f"List 6-8 job title variants that are the same or very similar role to: {job_title}\n"
        f"Include abbreviations, alternative names, and closely related titles.\n"
        f'Return ONLY a JSON array of lowercase strings. Example: ["customer success manager", "csm", "account manager"]'
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_EXTRACT,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        synonyms   = json.loads(raw)
        all_titles = list({job_title.lower()} | {s.lower() for s in synonyms if isinstance(s, str)})
        _cache_set(cache_key, all_titles)
        return all_titles
    except Exception as exc:
        print(f"[Claude] Title expansion failed: {exc}")
        return [job_title.lower()]


# ---------------------------------------------------------------------------
# Claude API calls
# ---------------------------------------------------------------------------

def _ask_claude_batch(
    company_texts: list[dict],
    job_titles: list[str],
    location: str,
) -> dict[str, list]:
    """
    Send a batch of companies to Claude Haiku for role extraction.
    job_titles is the expanded list of synonym titles.
    """
    client = anthropic.Anthropic()

    blocks   = [f"### COMPANY: {item['company']}\n\n{item['text'][:2000]}" for item in company_texts]
    combined = "\n\n---\n\n".join(blocks)
    titles   = ", ".join(f'"{t}"' for t in job_titles)

    prompt = (
        f"You are a job-search assistant. Below are careers page extracts from several companies.\n"
        f"The user is looking for open roles matching or similar to ANY of these titles: {titles}.\n"
        f"These are all names for essentially the same type of role — match any of them.\n"
        f"Location filter: only include roles based in, or explicitly open to remote candidates in, "
        f"**{location}**. Ignore EU-only or unlocated roles.\n\n"
        f"For each company return a JSON object: company name as key, list of matched roles as value.\n"
        f'Each role: {{"role_title": str, "url": str}}. Empty list if no match.\n\n'
        f"Return ONLY valid JSON.\n\n"
        f"Page extracts:\n\n{combined}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_EXTRACT,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
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
    job_titles: list[str],
    location: str,
    state: dict,
    lock: threading.Lock,
) -> list[dict]:
    """
    Three-tier search for open roles at `company`.
    job_titles is the expanded synonym list from expand_job_title().
    """
    results = []

    try:
        # --- Tier 1: probe the company's own careers page ---
        page_key    = _page_cache_key(company)
        cached_text = _cache_get(page_key)

        if cached_text:
            raw_text = cached_text
            tier     = "Tier 1 (cached)"
        else:
            raw_text = ""
            tier     = "Tier 1"
            for url in _tier1_url_patterns(company):
                if not _url_likely_exists(url):
                    continue
                text = _visit_page_playwright(url)
                if text and len(text.strip()) > MIN_USEFUL_CONTENT_CHARS:
                    raw_text = text
                    break
            if raw_text:
                _cache_set(page_key, raw_text)

        filtered = _filter_job_text(raw_text)

        # --- Tier 2: try Greenhouse / Lever / Ashby ---
        if len(filtered) < MIN_USEFUL_CONTENT_CHARS:
            tier      = "Tier 2"
            raw_text2 = ""
            for url in _tier2_url_patterns(company):
                if not _url_likely_exists(url):
                    continue
                text = _visit_page_playwright(url)
                if text and len(text.strip()) > MIN_USEFUL_CONTENT_CHARS:
                    raw_text2 += "\n\n" + text
            filtered2 = _filter_job_text(raw_text2)
            if len(filtered2) >= len(filtered):
                filtered = filtered2

        # --- Skip Claude if text is too thin or clearly off-topic ---
        if len(filtered) < MIN_USEFUL_CONTENT_CHARS or not _text_likely_relevant(filtered, job_titles):
            results = _build_rows(company, connections, [], tier + " — no match")
            return results

        # --- Claude extraction (with output cache) ---
        claude_key   = _claude_cache_key(company, job_titles, filtered)
        cached_roles = _cache_get(claude_key)

        if cached_roles is not None:
            matched_roles = cached_roles
        else:
            batch_result  = _ask_claude_batch(
                [{"company": company, "text": filtered}],
                job_titles,
                location,
            )
            matched_roles = batch_result.get(company, [])
            _cache_set(claude_key, matched_roles)

        results = (
            _build_rows(company, connections, matched_roles, tier)
            if matched_roles
            else _build_rows(company, connections, [], tier + " — no match")
        )

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
                "company":          company,
                "role_title":       "—",
                "connection_name":  conn["name"],
                "connection_title": conn["title"],
                "source":           source,
                "job_url":          "",
            })
    else:
        for role in roles:
            for conn in connections:
                rows.append({
                    "company":          company,
                    "role_title":       role.get("role_title", ""),
                    "connection_name":  conn["name"],
                    "connection_title": conn["title"],
                    "source":           source,
                    "job_url":          role.get("url", ""),
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
    max_workers: int = 2,
):
    """
    Expand the job title into synonyms, then process all companies concurrently.
    """
    import concurrent.futures

    # One-time synonym expansion before spawning workers
    job_titles = expand_job_title(job_title)
    print(f"[search] Searching for: {job_titles}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                search_company, company, connections, job_titles, location, state, lock
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
