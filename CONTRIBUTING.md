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

The `domain` value is used in the Google CSE query as `site:<domain>`. The `name` value is shown in the Source column of the results table.

### Step 3: Verify it works

Search for a company you know uses that platform, with a job title you know they're hiring for. If the result shows up with the platform name in the Source column, it's working.

Note: each additional platform uses one Google CSE query per company when Tier 2 is triggered. With the 100/day free limit in mind, keep the list short or consider upgrading to a paid CSE plan.

---

## Other contributions

- Bug reports and pull requests are welcome via GitHub Issues and PRs
- Please do not commit `.env`, `cache.json`, `search_history.json`, or any `.csv` files — they are git-ignored for a reason
- Keep personal data out of all committed files
