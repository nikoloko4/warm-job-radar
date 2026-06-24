"""
Core search logic: job board APIs, plain HTTP, Playwright (last resort), Claude, caching.

Tier 1 — Greenhouse / Lever / Ashby JSON APIs: no browser, structured data.
Tier 2 — Plain requests HTTP fetch of company.com/careers etc.
Tier 3 — Single-thread Playwright queue for JS-only SPAs.
Claude is used for synonym expansion, HTML text extraction, and referral messages.
"""

import hashlib
import json
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests as _requests
from anthropic import Anthropic
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_MODEL_EXTRACT  = "claude-haiku-4-5-20251001"
CLAUDE_MODEL_REFERRAL = "claude-sonnet-4-6"

MIN_USEFUL_CONTENT_CHARS = 300

CACHE_FILE        = Path("cache.json")
CACHE_TTL_SECONDS = 86400
CHECKPOINT_FILE   = Path("checkpoint.json")

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_CORP_SUFFIXES = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|corporation|company|group|holdings?|"
    r"international|technologies?|solutions?|services?|systems?|enterprises?|"
    r"ventures?|labs?)\b",
    re.IGNORECASE,
)

JOB_TEXT_KEYWORDS = {
    "apply", "role", "position", "experience", "requirements", "full-time",
    "part-time", "qualifications", "responsibilities", "salary", "remote",
    "hybrid", "location", "opening", "vacancy", "hire", "hiring",
}

# ---------------------------------------------------------------------------
# Playwright — single dedicated thread with a task queue
# Playwright's sync API uses greenlets bound to one thread.
# All browser calls go through this queue so there's exactly one Chromium process.
# ---------------------------------------------------------------------------

_pw_queue   : queue.Queue = queue.Queue()
_pw_results : dict        = {}
_pw_lock                  = threading.Lock()
_pw_thread  : threading.Thread | None = None


def _playwright_worker():
    """Runs in its own thread; restarts Chromium automatically if it crashes."""
    while True:
        try:
            pw      = sync_playwright().start()
            browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            print("[playwright] Browser started")
            while True:
                item = _pw_queue.get()
                if item is None:          # sentinel — shut down
                    browser.close()
                    pw.stop()
                    return
                req_id, url, event = item
                text = ""
                try:
                    ctx  = browser.new_context()
                    page = ctx.new_page()
                    try:
                        page.goto(url, timeout=10000, wait_until="domcontentloaded")
                        try:
                            page.wait_for_load_state("networkidle", timeout=3000)
                        except Exception:
                            pass
                        text = page.inner_text("body") or ""
                    except Exception as exc:
                        print(f"[playwright] {url}: {type(exc).__name__}")
                    finally:
                        ctx.close()
                except Exception:
                    pass
                with _pw_lock:
                    _pw_results[req_id] = text
                event.set()
        except Exception as exc:
            print(f"[playwright] Worker crashed ({exc}), restarting in 3s…")
            time.sleep(3)


def _ensure_playwright():
    global _pw_thread
    if _pw_thread is None or not _pw_thread.is_alive():
        _pw_thread = threading.Thread(target=_playwright_worker, daemon=True, name="playwright")
        _pw_thread.start()


def _playwright_fetch(url: str, timeout: float = 30.0) -> str:
    _ensure_playwright()
    req_id = f"{time.monotonic()}:{url}"
    event  = threading.Event()
    _pw_queue.put((req_id, url, event))
    if not event.wait(timeout=timeout):
        with _pw_lock:
            _pw_results.pop(req_id, None)
        return ""
    with _pw_lock:
        return _pw_results.pop(req_id, "")


# ---------------------------------------------------------------------------
# Disk cache (cache.json, 24 h TTL)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()


_cache_data        : dict = {}
_cache_write_count : int  = 0
_CACHE_FLUSH_EVERY        = 50


def _init_cache():
    global _cache_data
    if CACHE_FILE.exists():
        try:
            _cache_data = json.loads(CACHE_FILE.read_text())
        except Exception:
            _cache_data = {}


_init_cache()


def _cache_get(key: str):
    with _cache_lock:
        entry = _cache_data.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > CACHE_TTL_SECONDS:
            return None
        return entry.get("value")


def _cache_set(key: str, value):
    global _cache_write_count
    with _cache_lock:
        _cache_data[key] = {"value": value, "ts": time.time()}
        _cache_write_count += 1
        if _cache_write_count % _CACHE_FLUSH_EVERY == 0:
            CACHE_FILE.write_text(json.dumps(_cache_data, indent=2))


def flush_cache():
    with _cache_lock:
        if _cache_data:
            CACHE_FILE.write_text(json.dumps(_cache_data, indent=2))


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def save_checkpoint(job_title: str, location: str, done_companies: list, results: list):
    try:
        CHECKPOINT_FILE.write_text(json.dumps({
            "job_title":      job_title,
            "location":       location,
            "done_companies": done_companies,
            "results":        results,
        }, indent=2))
    except Exception as exc:
        print(f"[checkpoint] Save failed: {exc}")


def load_checkpoint() -> dict | None:
    if not CHECKPOINT_FILE.exists():
        return None
    try:
        return json.loads(CHECKPOINT_FILE.read_text())
    except Exception:
        return None


def clear_checkpoint():
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def _make_slugs(company: str) -> list[str]:
    cleaned     = _CORP_SUFFIXES.sub("", company)
    cleaned     = re.sub(r"[^a-zA-Z0-9\s-]", "", cleaned).strip().lower()
    slug_plain  = re.sub(r"[\s-]+", "", cleaned)
    slug_hyphen = re.sub(r"\s+", "-", cleaned).strip("-")
    seen = []
    for s in [slug_plain, slug_hyphen]:
        if s and s not in seen:
            seen.append(s)
    return seen


# ---------------------------------------------------------------------------
# Title and location matching
# ---------------------------------------------------------------------------

# Generic seniority/level words that don't define the role's function.
# These are treated as optional when matching — ALL non-level words must match.
_LEVEL_WORDS = {
    "senior", "junior", "lead", "principal", "staff", "associate", "executive",
    "director", "manager", "head", "president", "chief", "officer", "coordinator",
    "specialist", "analyst", "consultant", "advisor", "vp", "svp", "evp",
}


def _title_matches(title: str, job_titles: list[str]) -> bool:
    """
    Returns True if a job title is a plausible match for any of the searched titles.
    Requires ALL non-level words to appear in the candidate title so that e.g.
    "Engineering Manager" does NOT match a search for "Customer Success Manager".
    Abbreviations (csm, tam, ae) are matched as whole words.
    """
    title_lower = title.lower()
    for jt in job_titles:
        words = [w for w in re.split(r"\W+", jt.lower()) if w]
        # Short abbreviations: match the whole token exactly
        if len(words) == 1 and len(words[0]) <= 4:
            if re.search(r"\b" + re.escape(words[0]) + r"\b", title_lower):
                return True
            continue
        # Multi-word titles: require all content (non-level) words to appear
        content = [w for w in words if w not in _LEVEL_WORDS and len(w) > 2]
        if not content:
            # Fallback: nothing left after stripping level words, check any match
            if any(w in title_lower for w in words if len(w) > 2):
                return True
            continue
        if all(w in title_lower for w in content):
            return True
    return False


# Region qualifiers that indicate the role is NOT available globally.
# "Remote (EMEA)" means remote-within-EMEA, NOT worldwide remote.
_NON_US_REGIONS = frozenset([
    # Macro regions
    "emea", "neur", "dach", "apac", "latam", "latin america",
    "europe", "european", "middle east", "africa", "asia", "asia pacific", "oceania",
    # Europe — countries
    "uk", "united kingdom", "germany", "france", "netherlands", "spain", "italy",
    "sweden", "norway", "denmark", "finland", "ireland", "belgium", "austria",
    "switzerland", "poland", "portugal", "czech", "hungary", "romania", "greece",
    "turkey", "ukraine", "croatia", "serbia", "slovakia", "bulgaria",
    # Europe — cities
    "london", "amsterdam", "berlin", "paris", "dublin", "madrid", "stockholm",
    "oslo", "copenhagen", "helsinki", "warsaw", "lisbon", "vienna", "zurich",
    "munich", "hamburg", "frankfurt", "barcelona", "milan", "rome", "brussels",
    "prague", "budapest", "bucharest", "athens",
    "nantes", "lyon", "bordeaux", "toulouse", "strasbourg", "lille", "nice", "rennes",
    # Asia-Pacific — countries & cities
    "australia", "new zealand", "singapore", "india", "japan", "china",
    "south korea", "taiwan", "hong kong", "thailand", "vietnam", "philippines",
    "malaysia", "indonesia", "pakistan", "bangladesh",
    "tokyo", "osaka", "seoul", "beijing", "shanghai", "shenzhen",
    "mumbai", "bangalore", "delhi", "sydney", "melbourne",
    # Middle East & Africa
    "israel", "uae", "dubai", "saudi", "qatar", "egypt", "nigeria", "kenya",
    "south africa", "tel aviv",
    # Americas (non-US)
    "canada", "toronto", "vancouver", "montreal",
    "brazil", "mexico", "argentina", "colombia", "chile",
])


def _location_ok(job_location: str, search_location: str) -> bool:
    if not search_location.strip():
        return True
    if not job_location.strip():
        return True   # unknown — don't exclude
    loc = job_location.lower()

    # Exclude only if a non-US regional indicator is present
    if any(r in loc for r in _NON_US_REGIONS):
        # Override: if the search location words explicitly appear too (e.g. "Remote, US & UK")
        search_words = [w for w in re.split(r"\W+", search_location.lower()) if len(w) > 2]
        if search_words and any(w in loc for w in search_words):
            return True
        return False

    # No non-US indicator found — include (covers "New York, NY", "Remote", "Worldwide", etc.)
    return True


# ---------------------------------------------------------------------------
# Tier 1 — Job board JSON APIs
# ---------------------------------------------------------------------------

def _search_greenhouse(slug: str, job_titles: list[str], location: str) -> list[dict] | None:
    """Returns matched roles, or None if the company is not on Greenhouse."""
    neg_key = f"gh404:{slug}"
    if _cache_get(neg_key) is not None:
        return None
    try:
        resp = _requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            headers=_HTTP_HEADERS, timeout=2,
        )
        if resp.status_code == 404:
            _cache_set(neg_key, True)
            return None
        if resp.status_code != 200:
            return None
        jobs = resp.json().get("jobs", [])
        return [
            {"role_title": j["title"], "url": j.get("absolute_url", "")}
            for j in jobs
            if _title_matches(j.get("title", ""), job_titles)
            and _location_ok(
                j.get("location", {}).get("name", "") + " " + j.get("title", ""),
                location,
            )
        ]
    except Exception:
        return None


def _search_lever(slug: str, job_titles: list[str], location: str) -> list[dict] | None:
    """Returns matched roles, or None if the company is not on Lever."""
    neg_key = f"lv404:{slug}"
    if _cache_get(neg_key) is not None:
        return None
    try:
        resp = _requests.get(
            f"https://api.lever.co/v0/postings/{slug}",
            headers=_HTTP_HEADERS, timeout=2,
        )
        if resp.status_code == 404:
            _cache_set(neg_key, True)
            return None
        if resp.status_code != 200:
            return None
        postings = resp.json()
        if not isinstance(postings, list):
            return None
        return [
            {"role_title": p.get("text", ""), "url": p.get("hostedUrl", "")}
            for p in postings
            if _title_matches(p.get("text", ""), job_titles)
            and _location_ok(
                (
                    p.get("categories", {}).get("location", "")
                    or p.get("categories", {}).get("allLocations", [""])[0]
                ) + " " + p.get("text", ""),
                location,
            )
        ]
    except Exception:
        return None


def _search_ashby(slug: str, job_titles: list[str], location: str) -> list[dict] | None:
    """Returns matched roles, or None if the company is not on Ashby."""
    neg_key = f"ab404:{slug}"
    if _cache_get(neg_key) is not None:
        return None
    try:
        resp = _requests.post(
            "https://jobs.ashbyhq.com/api/non-user-graphql",
            json={
                "operationName": "ApiJobBoardWithTeams",
                "variables":     {"organizationHostedJobsPageName": slug},
                "query": (
                    "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {"
                    "  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {"
                    "    jobPostings { title locationName applicationLink }"
                    "  }"
                    "}"
                ),
            },
            headers={**_HTTP_HEADERS, "Content-Type": "application/json"},
            timeout=2,
        )
        if resp.status_code != 200:
            return None
        board = (resp.json().get("data") or {}).get("jobBoard")
        if board is None:
            _cache_set(neg_key, True)
            return None
        postings = board.get("jobPostings") or []
        return [
            {"role_title": p.get("title", ""), "url": p.get("applicationLink", "")}
            for p in postings
            if _title_matches(p.get("title", ""), job_titles)
            and _location_ok(p.get("locationName", "") + " " + p.get("title", ""), location)
        ]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tier 1 parallel runner — all platforms × all slugs simultaneously
# ---------------------------------------------------------------------------

def _search_platforms_parallel(
    company: str,
    job_titles: list[str],
    location: str,
) -> tuple[list[dict], str]:
    """
    Fire every platform+slug combination at once and pick the highest-priority
    result. Priority order: Greenhouse > Lever > Ashby.
    Returns (matched_roles, source_label) or ([], "") if not on any platform.
    """
    slugs     = _make_slugs(company)
    platforms = [
        ("Greenhouse", _search_greenhouse),
        ("Lever",      _search_lever),
        ("Ashby",      _search_ashby),
    ]
    tasks = [
        (prio, slug_i, name, fn, slug)
        for prio, (name, fn) in enumerate(platforms)
        for slug_i, slug in enumerate(slugs)
    ]

    hits: dict[tuple, tuple] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {
            ex.submit(fn, slug, job_titles, location): (prio, slug_i, name)
            for prio, slug_i, name, fn, slug in tasks
        }
        for future in as_completed(futures):
            prio, slug_i, name = futures[future]
            try:
                result = future.result()
                if result is not None:       # None means "company not on this platform"
                    hits[(prio, slug_i)] = (result, name)
            except Exception:
                pass

    # Return the highest-priority (lowest prio number, then lowest slug_i) hit
    for prio in range(len(platforms)):
        for slug_i in range(len(slugs)):
            if (prio, slug_i) in hits:
                roles, name = hits[(prio, slug_i)]
                source = name if roles else f"{name} — no match"
                return roles, source

    return [], ""


# ---------------------------------------------------------------------------
# Tier 2 — Plain HTTP fetch (no browser)
# ---------------------------------------------------------------------------

def _http_fetch_text(url: str) -> str:
    try:
        resp = _requests.get(
            url, headers=_HTTP_HEADERS, timeout=6, allow_redirects=True,
        )
        if resp.status_code >= 400:
            return ""
        html = resp.text
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>",   " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", html).strip()
    except Exception:
        return ""


def _candidate_careers_urls(company: str) -> list[str]:
    urls = []
    for slug in _make_slugs(company):
        urls += [
            f"https://{slug}.com/careers",
            f"https://{slug}.com/jobs",
            f"https://careers.{slug}.com",
            f"https://jobs.{slug}.com",
        ]
    return urls


# ---------------------------------------------------------------------------
# Page text pre-filtering
# ---------------------------------------------------------------------------

def _filter_job_text(raw_text: str) -> str:
    paragraphs = re.split(r"\s{3,}|\n{2,}", raw_text)
    kept = [
        p.strip() for p in paragraphs
        if len(p.strip()) > 40 and any(kw in p.lower() for kw in JOB_TEXT_KEYWORDS)
    ]
    return "\n\n".join(kept)


def _text_likely_relevant(text: str, job_titles: list[str]) -> bool:
    all_words  = set()
    for title in job_titles:
        all_words |= {w.lower() for w in re.split(r"\W+", title) if len(w) > 2}
    lower_text = text.lower()
    return any(word in lower_text for word in all_words)


# ---------------------------------------------------------------------------
# Job title synonym expansion
# ---------------------------------------------------------------------------

def expand_job_title(job_title: str) -> list[str]:
    cache_key = f"synonyms:{hashlib.sha256(job_title.lower().encode()).hexdigest()}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = Anthropic()
    prompt = (
        f"List 6-8 job title variants that are the same or very similar role to: {job_title}\n"
        f"Include abbreviations, alternative names, and closely related titles.\n"
        f'Return ONLY a JSON array of lowercase strings. '
        f'Example: ["customer success manager", "csm", "account manager"]'
    )
    try:
        response  = client.messages.create(
            model=CLAUDE_MODEL_EXTRACT, max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw       = response.content[0].text.strip()
        raw       = re.sub(r"^```(?:json)?\s*", "", raw)
        raw       = re.sub(r"\s*```$", "", raw)
        synonyms  = json.loads(raw)
        result    = list({job_title.lower()} | {s.lower() for s in synonyms if isinstance(s, str)})
        _cache_set(cache_key, result)
        return result
    except Exception as exc:
        print(f"[claude] Title expansion failed: {exc}")
        return [job_title.lower()]


# ---------------------------------------------------------------------------
# Claude — role extraction from HTML text
# ---------------------------------------------------------------------------

def _ask_claude_batch(
    company_texts: list[dict],
    job_titles: list[str],
    location: str,
) -> dict[str, list]:
    client   = Anthropic()
    blocks   = [
        f"### COMPANY: {item['company']}\n\n{item['text'][:2000]}"
        for item in company_texts
    ]
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
        f"Return ONLY valid JSON.\n\nPage extracts:\n\n{combined}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_EXTRACT, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as exc:
        print(f"[claude] Batch extraction failed: {type(exc).__name__}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Referral message generation
# ---------------------------------------------------------------------------

def generate_referral_message(
    company: str,
    role_title: str,
    connection_name: str,
    connection_title: str,
) -> str:
    client = Anthropic()
    prompt = (
        f"Write a warm, concise LinkedIn message (150 words max) asking "
        f"{connection_name} ({connection_title} at {company}) for a referral "
        f"for the role: {role_title} at {company}. "
        f"Be genuine and specific — reference the role and their position. "
        f"Do not include a subject line. Do not use placeholders."
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_REFERRAL, max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        return f"Could not generate message: {exc}"


# ---------------------------------------------------------------------------
# Per-company search
# ---------------------------------------------------------------------------

def search_company(
    company: str,
    connections: list[dict],
    job_titles: list[str],
    location: str,
    state: dict,
    lock: threading.Lock,
) -> list[dict]:
    results: list[dict] = []

    try:
        matched_roles : list[dict] = []
        source        = ""

        # --- Tier 1: All job board APIs in parallel ---
        matched_roles, source = _search_platforms_parallel(company, job_titles, location)

        # --- Tier 2 + 3: Plain HTTP (parallel URL checks) → Playwright for JS SPAs ---
        if not source:
            page_key    = f"page:{hashlib.sha256(company.lower().encode()).hexdigest()}"
            cached_text = _cache_get(page_key)

            if cached_text is not None:
                raw_text  = cached_text
                http_tier = "HTTP (cached)"
            else:
                raw_text  = ""
                http_tier = "HTTP"
                candidate_urls = _candidate_careers_urls(company)

                # Fetch all candidate URLs in parallel
                url_texts: dict[str, str] = {}
                with ThreadPoolExecutor(max_workers=len(candidate_urls)) as ex:
                    fs = {ex.submit(_http_fetch_text, u): u for u in candidate_urls}
                    for f in as_completed(fs):
                        url_texts[fs[f]] = f.result()

                # Pick first (priority-ordered) URL with useful content
                pw_candidate = ""
                for url in candidate_urls:
                    text = url_texts.get(url, "")
                    if len(text) > MIN_USEFUL_CONTENT_CHARS:
                        raw_text = text
                        break
                    if 50 < len(text) <= MIN_USEFUL_CONTENT_CHARS and not pw_candidate:
                        pw_candidate = url  # thin — probably a JS SPA

                # Fall back to Playwright only if all HTTP fetches were too thin
                if not raw_text and pw_candidate:
                    pw_text = _playwright_fetch(pw_candidate)
                    if len(pw_text) > MIN_USEFUL_CONTENT_CHARS:
                        raw_text  = pw_text
                        http_tier = "HTTP+JS"

                if raw_text:
                    _cache_set(page_key, raw_text)

            filtered = _filter_job_text(raw_text)

            if len(filtered) >= MIN_USEFUL_CONTENT_CHARS and _text_likely_relevant(filtered, job_titles):
                claude_key   = f"claude:{hashlib.sha256((company + '|' + '|'.join(sorted(job_titles)) + '|' + filtered).encode()).hexdigest()}"
                cached_roles = _cache_get(claude_key)
                if cached_roles is not None:
                    matched_roles = cached_roles
                else:
                    batch         = _ask_claude_batch(
                        [{"company": company, "text": filtered}], job_titles, location,
                    )
                    matched_roles = batch.get(company, [])
                    _cache_set(claude_key, matched_roles)
                source = http_tier if matched_roles else f"{http_tier} — no match"
            elif raw_text:
                source = f"{http_tier} — no match"
            else:
                source = "Not found"

        results = _build_rows(company, connections, matched_roles, source)

    except Exception as exc:
        print(f"[search] Error processing {company}: {type(exc).__name__}: {exc}")
        results = _build_rows(company, connections, [], "Error")
        with lock:
            state["errors"] = state.get("errors", 0) + 1

    finally:
        with lock:
            state["results"].extend(results)
            state["done"] += 1
            state.setdefault("done_companies", []).append(company)
            # Save checkpoint every 10 companies to avoid serialising thousands
            # of result rows on every single completion.
            if state["done"] % 10 == 0:
                save_checkpoint(
                    state.get("job_title", ""),
                    state.get("location", ""),
                    list(state["done_companies"]),
                    list(state["results"]),
                )

    return results


def _build_rows(company, connections, roles, source):
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
# Batch runner
# ---------------------------------------------------------------------------

def run_search(
    company_map: dict[str, list[dict]],
    job_title: str,
    location: str,
    state: dict,
    lock: threading.Lock,
    max_workers: int = 5,
    resume_from: dict | None = None,
):
    job_titles = expand_job_title(job_title)
    print(f"[search] Searching for: {job_titles}")

    with lock:
        state["job_title"]      = job_title
        state["location"]       = location
        state["done_companies"] = []

    if resume_from:
        done_set = set(resume_from.get("done_companies", []))
        with lock:
            state["results"]        = list(resume_from.get("results", []))
            state["done"]           = len(done_set)
            state["done_companies"] = list(done_set)
        remaining = {c: v for c, v in company_map.items() if c not in done_set}
    else:
        clear_checkpoint()
        remaining = company_map

    # Start the Playwright thread proactively so the first SPA hit doesn't wait for startup
    _ensure_playwright()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                search_company, company, connections, job_titles, location, state, lock
            ): company
            for company, connections in remaining.items()
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                future.result()
            except Exception as exc:
                print(f"[runner] Unhandled error for {company}: {exc}")

    with lock:
        state["running"] = False

    flush_cache()
    clear_checkpoint()
