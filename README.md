# Warm Job Radar

Find open job roles at companies where you already have LinkedIn connections — so every application can come with a warm introduction.

---

## What it does

1. You upload your LinkedIn connections CSV export
2. You enter a job title (e.g. "Product Manager") and a location (default: United States)
3. The app probes each company's careers page directly using a browser and extracts open roles
4. Results appear in a table alongside the connection you have at each company
5. Click any result to get a personalised LinkedIn referral message drafted by Claude, ready to copy and send

---

## Prerequisites

- Python 3.10 or higher
- An [Anthropic API key](https://console.anthropic.com) — that's it

No other accounts or API keys required.

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/nikoloko4/warm-job-radar.git
cd warm-job-radar
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate      # macOS / Linux
venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Add your Anthropic API key

```bash
cp .env.example .env
```

Open `.env` and fill in your key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Get your key at [console.anthropic.com](https://console.anthropic.com) → API Keys → Create key.

### 5. Export your LinkedIn connections

1. Go to LinkedIn → **Me** → **Settings & Privacy**
2. **Data Privacy** → **Get a copy of your data**
3. Select **Connections** and request the export
4. LinkedIn will email you a download link within minutes
5. Unzip and use the `Connections.csv` file

### 6. Run the app

```bash
python app.py
```

Open [http://127.0.0.1:8050](http://127.0.0.1:8050) in your browser.

---

## How to use

1. Click **Choose file** and upload your `Connections.csv`
2. Enter the job title you're looking for
3. Adjust the location filter if needed (defaults to "United States")
4. Click **Search** — a progress bar shows how many companies have been checked
5. Results populate in real time as each company is processed
6. Click any row to generate a personalised referral message for that connection
7. Click **Export CSV** to download all results

---

## How the search works

### Three-tier search strategy

**Tier 1 — Company's own careers page**
The app derives likely URL patterns from the company name (e.g. `acme.com/careers`, `careers.acme.com`) and uses a real browser (Playwright/Chromium) to load them. It waits for JavaScript to render, clicks through to job listings, and extracts the full visible text. The first URL that returns real content is used.

**Tier 2 — Fallback platforms**
If the company's own page yields too little content, the app tries the company's profile on Greenhouse, Lever, and Ashby using the same direct URL pattern approach.

**Tier 3 — No results**
If neither tier finds anything, the row shows "no match" — the app never invents fake results.

### Location filtering

The location filter is passed to Claude as a constraint: only roles based in, or explicitly open to remote candidates in, the specified location are returned. EU-only or unlocated roles are excluded.

### Cost optimisations built in

- **Page text cache**: Scraped page text is cached for 24 hours. Re-running a search for the same companies costs no extra time.
- **Claude output cache**: If the same company + job title + page content is seen again, the cached Claude response is reused — no API call.
- **Pre-filtering**: Navigation menus, cookie banners, footers, and legal text are stripped before sending to Claude. Only job-relevant paragraphs are sent.
- **Haiku for extraction**: Claude Haiku (cheap) extracts role titles; Claude Sonnet (quality) only runs when you click "Draft referral message".
- **Local keyword screen**: If the filtered page text contains no words from the job title, Claude is skipped entirely.

### Referral message generator

Clicking a result row sends the connection's name, their title, the company, and the matched role to Claude Sonnet, which drafts a short, specific LinkedIn message. The message references the exact role and connection — not a generic template.

---

## Cost expectations

| Action | Approximate cost |
|--------|-----------------|
| Extracting roles from 50 companies | ~$0.01–0.05 (Haiku) |
| One referral message draft | ~$0.001 (Sonnet) |
| Repeat search same day (cached) | $0 |

Anthropic charges per token at very low rates. A typical personal-use session costs less than $0.10.

---

## Privacy

- Your `.env` file is excluded from git via `.gitignore` — it will never be committed
- The app runs entirely locally; data is only sent to the Anthropic API (for role extraction and referral messages) and to the careers pages loaded by Playwright
- No connection names, email addresses, or personal data are ever logged or stored in files that could be committed

---

## Configuration

Optional environment variable in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_WORKERS` | `3` | Concurrent company searches. Lower = gentler on your machine |

---

## License

MIT — see [LICENSE](LICENSE)
