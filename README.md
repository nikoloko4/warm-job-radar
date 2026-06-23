# Warm Job Radar

Find open job roles at companies where you already have LinkedIn connections — so every application can come with a warm introduction.

---

## What it does

1. You upload your LinkedIn connections CSV export
2. You enter a job title (e.g. "Product Manager") and a location (default: United States)
3. The app searches each company's careers page for matching roles using a three-tier strategy
4. Results appear in a table alongside the connection you have at each company
5. Click any result to get a personalised LinkedIn referral message drafted by Claude, ready to copy and send

---

## Prerequisites

- Python 3.10 or higher
- An [Anthropic API key](https://console.anthropic.com) (pay-per-use, fractions of a cent per company search)
- A [Google Custom Search API key](https://console.cloud.google.com) and a [Custom Search Engine ID](https://programmablesearchengine.google.com) (100 free searches/day)

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

### 4. Configure your API keys

```bash
cp .env.example .env
```

Open `.env` and fill in the three values:

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AIza...
GOOGLE_CSE_ID=1234567890abc
```

#### How to get each key

| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `GOOGLE_API_KEY` | [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials → Create API key, then enable the **Custom Search API** |
| `GOOGLE_CSE_ID` | [programmablesearchengine.google.com](https://programmablesearchengine.google.com) → New search engine → Search the entire web → copy the **Search engine ID** |

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

**Tier 1 — Company careers page**
Google Custom Search finds the company's own careers page URL. A real browser (Playwright/Chromium) loads the page, waits for JavaScript to render, and clicks through to job listings. The full visible text is sent to Claude.

**Tier 2 — Fallback platforms**
If the careers page yields too little text or Claude finds no matching roles, the app searches Greenhouse, Lever, and Ashby for the company name and job title. Each result URL is loaded with Playwright and sent to Claude.

**Tier 3 — No results**
If neither tier finds anything, the row shows "no match" — the app never invents fake results.

### Location filtering

The location filter is passed to Claude as a constraint: only roles based in, or explicitly open to remote candidates in, the specified location are returned. EU-only or unlocated roles are excluded.

### Cost optimisations built in

- **Page text cache**: Scraped page text is cached for 24 hours. Re-running a search for the same companies costs no Playwright time and no Google CSE quota.
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
| Google CSE searches (Tier 1 only, 50 companies) | 50 of your 100 free/day |
| Repeat search same day (cached) | $0 / 0 CSE calls |

Google Custom Search: 100 free queries per day. Each Tier 1 search uses 1 query; each Tier 2 search uses up to 3 (one per platform). The UI shows a warning when you approach the limit.

---

## Privacy

- Your `.env` file and `search_history.json` are excluded from git via `.gitignore` — they will never be committed
- The app runs entirely locally; no data is sent anywhere except the three APIs (Anthropic, Google, and the careers pages loaded by Playwright)
- No connection names, email addresses, or personal data are ever logged or stored in files that could be committed

---

## Configuration

Optional environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_WORKERS` | `3` | Concurrent company searches. Lower = gentler on APIs |

---

## License

MIT — see [LICENSE](LICENSE)
