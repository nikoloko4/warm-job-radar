# Warm Job Radar

Find open job roles at companies where you already have LinkedIn connections — so every application can come with a warm introduction.

---

## What it does

1. You upload your LinkedIn connections CSV (exported from LinkedIn's full data export)
2. You enter a job title (e.g. "Customer Success Manager") and a location (default: United States)
3. The app searches each company's job board using public APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workable) and — when needed — directly scrapes their careers page
4. Results appear in a table alongside the connection you have at each company
5. Click **Export CSV** to download everything

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

### 5. Export your LinkedIn data

LinkedIn buries the connections file inside a full data export. Here's how to get it:

1. Go to LinkedIn → **Me** (top right) → **Settings & Privacy**
2. Click **Data Privacy** in the left sidebar
3. Click **Get a copy of your data**
4. Select **"Want something in particular? Select the data files you're most interested in"**
5. Tick **Connections** and click **Request archive**
6. LinkedIn will email you a download link — this usually takes a few minutes but can take up to 24 hours
7. Download and unzip the archive
8. Inside the zip you'll find a file called **`Connections.csv`** — that's the one to upload

> The zip contains many other files (messages, jobs, etc.) — you only need `Connections.csv`.

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
5. Results populate in real time
6. Click any row to see all connections at that company if there are more than three
7. Click **Export CSV** to download all results

---

## How the search works

### Job board APIs first

The app checks five public job board APIs in parallel for every company:

| Platform | Coverage |
|----------|----------|
| Greenhouse | Most common among tech/SaaS companies |
| Lever | Widely used by startups |
| Ashby | Popular with newer/smaller startups |
| SmartRecruiters | Common among mid-size companies |
| Workable | Common among SMBs and European companies |

These API calls are fast (under a second each) and return clean structured data.

### Fallback for other companies

If a company isn't on any of those platforms, the app tries fetching their careers page directly via HTTP and — for JavaScript-heavy pages — via a real headless browser (Playwright/Chromium). The page text is passed to Claude Haiku to extract matching roles.

### Synonym expansion

When you type a job title, Claude expands it into 10–15 variants used across the industry (e.g. "Customer Success Manager" → CSM, Client Success Manager, Technical Account Manager, etc.) so you catch roles listed under different names.

### Location filtering

Roles are filtered to the specified location. Region-specific indicators like EMEA, DACH, London, Tokyo etc. in either the job title or location field are excluded when searching for US roles.

---

## Cost expectations

The app uses Claude Haiku for role extraction and synonym expansion — one of the cheapest models available.

| Action | Approximate cost |
|--------|-----------------|
| Synonym expansion (once, then cached) | < $0.001 |
| Searching 100 companies via API | ~$0.00 (no Claude needed) |
| Extracting roles from 20 companies via page scrape | ~$0.01–0.02 |
| Repeat search same day (all cached) | $0.00 |

A typical full run over 500+ companies costs **under $0.05**. Cached re-runs are free.

---

## Privacy

- Your `.env` file is excluded from git — it will never be committed
- The app runs entirely locally; data is only sent to the Anthropic API (role extraction and synonym expansion) and to the job board APIs / careers pages being searched
- No connection names, email addresses, or personal data are logged or stored in any file that could be committed to git

---

## Configuration

All settings go in your `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required. Get one at console.anthropic.com |
| `MAX_WORKERS` | `5` | How many companies to search in parallel |

---

## License

MIT — see [LICENSE](LICENSE)
