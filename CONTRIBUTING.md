# Contributing to Warm Job Radar

## Adding a new fallback job platform

When Tier 1 (the company's own careers page) doesn't yield results, the app falls back to searching known job platforms (Greenhouse, Lever, Ashby). You can add more platforms in one line.

### Step 1: Open `search.py`

Find the `FALLBACK_PLATFORMS` list near the top of the file:

```python
FALLBACK_PLATFORMS = [
    {"name": "Greenhouse", "domain": "greenhouse.io"},
    {"name": "Lever",      "domain": "lever.co"},
    {"name": "Ashby",      "domain": "jobs.ashbyhq.com"},
]
```

### Step 2: Add your platform

```python
FALLBACK_PLATFORMS = [
    {"name": "Greenhouse", "domain": "greenhouse.io"},
    {"name": "Lever",      "domain": "lever.co"},
    {"name": "Ashby",      "domain": "jobs.ashbyhq.com"},
    {"name": "Workday",    "domain": "myworkdayjobs.com"},  # <-- new entry
]
```

The `domain` value is used to construct direct URL candidates (e.g. `boards.greenhouse.io/{slug}`). The `name` value is shown in the Source column of the results table.

### Step 3: Verify it works

Search for a company you know uses that platform, with a job title you know they're hiring for. If the result shows up with the platform name in the Source column, it's working.

Note: each additional platform means more URLs to probe per company in Tier 2. Keep the list reasonably short to avoid slowing down searches.

---

## Other contributions

- Bug reports and pull requests are welcome via GitHub Issues and PRs
- Please do not commit `.env`, `cache.json`, `search_history.json`, or any `.csv` files — they are git-ignored for a reason
- Keep personal data out of all committed files
