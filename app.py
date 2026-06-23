"""
Warm Job Radar — main Dash application.
Run with: python app.py
Then open http://127.0.0.1:8050
"""

import base64
import io
import json
import os
import re
import threading
from datetime import datetime

import dash
import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, State, dash_table, dcc, html
from dotenv import load_dotenv

import search as search_mod

load_dotenv()

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_lock = threading.Lock()

_state: dict = {
    "running":        False,
    "total":          0,
    "done":           0,
    "errors":         0,
    "results":        [],
    "done_companies": [],
    "job_title":      "",
    "location":       "",
    "history":        [],
}

# ---------------------------------------------------------------------------
# Company name normalisation (for fuzzy deduplication)
# ---------------------------------------------------------------------------

_NORM_SUFFIXES = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|corporation|company|group|"
    r"holdings?|international|technologies?|solutions?|services?|"
    r"systems?|enterprises?|ventures?|labs?)\b\.?",
    re.IGNORECASE,
)


def _normalise_company(name: str) -> str:
    """Return a lowercase, suffix-stripped, whitespace-collapsed version for dedup."""
    n = _NORM_SUFFIXES.sub("", name)
    n = re.sub(r"[^a-z0-9\s]", "", n.lower())
    return re.sub(r"\s+", " ", n).strip()


# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY],
    title="Warm Job Radar",
)

# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _history_items(history: list) -> list:
    if not history:
        return [html.P("No searches yet.", className="text-muted small")]
    return [
        html.Div([
            html.Span(h["job_title"], className="fw-bold"),
            html.Br(),
            html.Small(f"{h['timestamp']} — {h['result_count']} results", className="text-muted"),
        ], className="mb-2 border-bottom pb-2")
        for h in history
    ]


def _resume_banner():
    """Return a banner if a checkpoint exists, otherwise None."""
    cp = search_mod.load_checkpoint()
    if not cp:
        return {"display": "none"}, ""
    done = len(cp.get("done_companies", []))
    label = (
        f"Unfinished search found: \"{cp['job_title']}\" — "
        f"{done} companies done. Resume where you left off?"
    )
    return {"display": "block"}, label


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

app.layout = dbc.Container(fluid=True, children=[

    dcc.Store(id="csv-store"),
    dcc.Store(id="company-map-store"),
    dcc.Download(id="download-csv"),
    dcc.Interval(id="poll-interval", interval=2000, disabled=True),

    dbc.Row(className="mt-3 mb-2", children=[
        dbc.Col(html.H2("Warm Job Radar", className="text-primary fw-bold"), width="auto"),
        dbc.Col(
            html.P("Find open roles at companies where you have LinkedIn connections.",
                   className="text-muted mt-2"),
            width="auto"
        ),
    ]),

    dbc.Row([
        # --- Left sidebar ---
        dbc.Col(width=2, children=[
            html.H6("Search History", className="text-muted text-uppercase small fw-bold"),
            html.Div(id="history-sidebar"),
        ]),

        # --- Main panel ---
        dbc.Col(width=10, children=[

            # Resume banner (hidden when no checkpoint)
            dbc.Alert(
                id="resume-banner",
                color="warning",
                is_open=False,
                className="mb-2 py-2",
                children=[
                    html.Span(id="resume-banner-text", className="me-3"),
                    dbc.Button("Resume", id="resume-btn", color="warning",
                               size="sm", className="me-2"),
                    dbc.Button("Dismiss", id="dismiss-resume-btn", color="link",
                               size="sm", className="text-muted"),
                ],
            ),

            # Upload + inputs
            dbc.Card(className="mb-3", children=[
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label("1. Upload LinkedIn Connections CSV", className="fw-bold"),
                            dcc.Upload(
                                id="csv-upload",
                                children=dbc.Button("Choose file", color="secondary",
                                                    outline=True, size="sm"),
                                className="mb-1",
                            ),
                            html.Div(id="upload-status", className="text-muted small"),
                        ], width=4),

                        dbc.Col([
                            html.Label("2. Job Title", className="fw-bold"),
                            dbc.Input(id="job-title-input",
                                      placeholder="e.g. Product Manager",
                                      type="text", debounce=False),
                        ], width=3),

                        dbc.Col([
                            html.Label("3. Location Filter", className="fw-bold"),
                            dbc.Input(id="location-input",
                                      placeholder="e.g. United States",
                                      type="text", value="United States",
                                      debounce=False),
                            html.Small("Only roles in this location will be returned.",
                                       className="text-muted"),
                        ], width=3),

                        dbc.Col([
                            html.Label(" ", className="d-block"),
                            dbc.Button("Search", id="search-btn", color="primary",
                                       className="me-2 mb-1"),
                            dbc.Button("Export CSV", id="export-btn", color="success",
                                       outline=True),
                        ], width=2, className="d-flex align-items-start flex-column"),
                    ]),
                ]),
            ]),

            # Progress
            html.Div(id="progress-area", className="mb-2", children=[
                dbc.Progress(id="progress-bar", value=0, max=100, striped=True,
                             animated=True, className="mb-1", style={"height": "20px"}),
                html.Div(id="progress-label", className="small text-muted"),
            ]),

            # Results table
            dash_table.DataTable(
                id="results-table",
                columns=[
                    {"name": "Company",       "id": "company"},
                    {"name": "Matched Role",  "id": "role_title"},
                    {"name": "Connection",    "id": "connection_name"},
                    {"name": "Their Title",   "id": "connection_title"},
                    {"name": "Source",        "id": "source"},
                    {"name": "Job URL",       "id": "job_url", "presentation": "markdown"},
                ],
                data=[],
                row_selectable="single",
                selected_rows=[],
                page_size=25,
                style_table={"overflowX": "auto"},
                style_cell={"textAlign": "left", "padding": "6px 12px", "fontSize": "13px"},
                style_header={"fontWeight": "bold", "backgroundColor": "#f8f9fa"},
                style_data_conditional=[{
                    "if": {"filter_query": '{role_title} = "—"'},
                    "color": "#adb5bd",
                }],
                markdown_options={"link_target": "_blank"},
            ),

            # Referral message panel
            html.Div(className="mt-3", children=[
                dbc.Card(id="referral-card", style={"display": "none"}, children=[
                    dbc.CardHeader([
                        html.Span("Draft Referral Message", className="fw-bold"),
                        dbc.Button("×", id="close-referral-btn", color="link",
                                   className="float-end p-0 text-muted"),
                    ]),
                    dbc.CardBody([
                        dbc.Spinner(html.Div(id="referral-text"), color="primary"),
                    ]),
                ]),
            ]),
        ]),
    ]),
], style={"paddingBottom": "60px"})


# ---------------------------------------------------------------------------
# Callback: show resume banner on page load
# ---------------------------------------------------------------------------

@app.callback(
    Output("resume-banner", "is_open"),
    Output("resume-banner-text", "children"),
    Input("poll-interval", "id"),  # fires once on load
)
def check_checkpoint_on_load(_):
    cp = search_mod.load_checkpoint()
    if not cp:
        return False, ""
    done  = len(cp.get("done_companies", []))
    label = (
        f"Unfinished search: \"{cp['job_title']}\" in {cp['location']} — "
        f"{done} companies done. Resume where you left off?"
    )
    return True, label


# ---------------------------------------------------------------------------
# Callback: parse uploaded CSV (with fuzzy deduplication)
# ---------------------------------------------------------------------------

@app.callback(
    Output("csv-store", "data"),
    Output("company-map-store", "data"),
    Output("upload-status", "children"),
    Input("csv-upload", "contents"),
    State("csv-upload", "filename"),
    prevent_initial_call=True,
)
def parse_csv_callback(contents, filename):
    if not contents:
        return None, None, ""

    try:
        _, content_string = contents.split(",")
        decoded = base64.b64decode(content_string).decode("utf-8", errors="replace")
        lines   = decoded.splitlines()

        header_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("First Name")),
            None,
        )
        if header_idx is None:
            return None, None, dbc.Alert(
                "Could not find 'First Name' column. Is this a LinkedIn connections CSV?",
                color="danger", className="mb-0 py-1 small",
            )

        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
        required = [c for c in ["First Name", "Last Name", "Company", "Position"] if c in df.columns]
        df = df[required].dropna(subset=["Company"]).fillna("")

        # Build company_map with fuzzy deduplication:
        # normalised name → canonical display name + connections
        norm_to_canonical: dict[str, str]       = {}
        company_map:       dict[str, list[dict]] = {}

        for _, row in df.iterrows():
            raw_company = str(row.get("Company", "")).strip()
            if not raw_company:
                continue
            norm = _normalise_company(raw_company)
            if norm not in norm_to_canonical:
                norm_to_canonical[norm] = raw_company  # first seen = canonical name
            canonical = norm_to_canonical[norm]
            name  = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
            title = str(row.get("Position", "")).strip()
            company_map.setdefault(canonical, []).append({"name": name, "title": title})

        total_raw        = df["Company"].nunique()
        total_deduped    = len(company_map)
        total_connections = sum(len(v) for v in company_map.values())
        dedup_note       = (
            f" ({total_raw - total_deduped} merged as duplicates)" if total_raw > total_deduped else ""
        )

        status = dbc.Alert(
            f"Loaded: {filename} — {total_connections} connections at "
            f"{total_deduped} companies{dedup_note}.",
            color="success", className="mb-0 py-1 small",
        )
        return df.to_json(date_format="iso"), json.dumps(company_map), status

    except Exception as exc:
        return None, None, dbc.Alert(
            f"Error parsing CSV: {exc}", color="danger", className="mb-0 py-1 small",
        )


# ---------------------------------------------------------------------------
# Shared helper: kick off search (used by both Search and Resume buttons)
# ---------------------------------------------------------------------------

def _launch_search(company_map: dict, job_title: str, location: str,
                   resume_from: dict | None = None):
    max_workers = int(os.getenv("MAX_WORKERS", 5))

    with _lock:
        _state["running"]        = True
        _state["total"]          = len(company_map)
        _state["errors"]         = 0
        _state["results"]        = []
        _state["done_companies"] = []
        if resume_from:
            _state["results"]        = list(resume_from.get("results", []))
            _state["done"]           = len(resume_from.get("done_companies", []))
            _state["done_companies"] = list(resume_from.get("done_companies", []))
        else:
            _state["done"] = 0

    def _run():
        search_mod.run_search(
            company_map, job_title, location, _state, _lock, max_workers, resume_from,
        )
        with _lock:
            _state["history"].insert(0, {
                "job_title":    job_title,
                "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "result_count": sum(1 for r in _state["results"] if r["role_title"] != "—"),
            })
            _state["history"] = _state["history"][:10]

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Callback: start fresh search
# ---------------------------------------------------------------------------

@app.callback(
    Output("poll-interval", "disabled"),
    Output("progress-bar", "value"),
    Output("progress-label", "children"),
    Output("resume-banner", "is_open", allow_duplicate=True),
    Input("search-btn", "n_clicks"),
    State("company-map-store", "data"),
    State("job-title-input", "value"),
    State("location-input", "value"),
    prevent_initial_call=True,
)
def start_search_callback(n_clicks, company_map_json, job_title, location):
    if not company_map_json:
        return True, 0, "Please upload a LinkedIn CSV first.", False
    if not job_title or not job_title.strip():
        return True, 0, "Please enter a job title.", False

    location    = (location or "United States").strip()
    company_map = json.loads(company_map_json)
    _launch_search(company_map, job_title.strip(), location)
    return False, 0, f"Searching {len(company_map)} companies...", False


# ---------------------------------------------------------------------------
# Callback: resume from checkpoint
# ---------------------------------------------------------------------------

@app.callback(
    Output("poll-interval", "disabled", allow_duplicate=True),
    Output("progress-bar", "value", allow_duplicate=True),
    Output("progress-label", "children", allow_duplicate=True),
    Output("resume-banner", "is_open", allow_duplicate=True),
    Input("resume-btn", "n_clicks"),
    State("company-map-store", "data"),
    prevent_initial_call=True,
)
def resume_search_callback(n_clicks, company_map_json):
    cp = search_mod.load_checkpoint()
    if not cp:
        return True, 0, "No checkpoint found.", False

    company_map = json.loads(company_map_json) if company_map_json else {}
    if not company_map:
        return True, 0, "Please upload your LinkedIn CSV first, then resume.", False

    _launch_search(company_map, cp["job_title"], cp["location"], resume_from=cp)
    remaining = len(company_map) - len(cp.get("done_companies", []))
    return False, 0, f"Resuming — {remaining} companies remaining...", False


# ---------------------------------------------------------------------------
# Callback: dismiss resume banner
# ---------------------------------------------------------------------------

@app.callback(
    Output("resume-banner", "is_open", allow_duplicate=True),
    Input("dismiss-resume-btn", "n_clicks"),
    prevent_initial_call=True,
)
def dismiss_resume_callback(n_clicks):
    search_mod.clear_checkpoint()
    return False


# ---------------------------------------------------------------------------
# Callback: poll for updates
# ---------------------------------------------------------------------------

@app.callback(
    Output("results-table", "data"),
    Output("progress-bar", "value", allow_duplicate=True),
    Output("progress-label", "children", allow_duplicate=True),
    Output("history-sidebar", "children"),
    Output("poll-interval", "disabled", allow_duplicate=True),
    Input("poll-interval", "n_intervals"),
    prevent_initial_call=True,
)
def update_ui_callback(n_intervals):
    with _lock:
        done    = _state["done"]
        total   = _state["total"]
        running = _state["running"]
        results = list(_state["results"])
        history = list(_state["history"])

    pct   = int(done / total * 100) if total > 0 else 0
    label = f"{done} / {total} companies checked" if total > 0 else ""

    table_rows = []
    for r in results:
        row = dict(r)
        url = row.get("job_url", "")
        if url and url.startswith("http"):
            row["job_url"] = f"[View]({url})"
        table_rows.append(row)

    return table_rows, pct, label, _history_items(history), not running


# ---------------------------------------------------------------------------
# Callback: export CSV
# ---------------------------------------------------------------------------

@app.callback(
    Output("download-csv", "data"),
    Input("export-btn", "n_clicks"),
    State("results-table", "data"),
    prevent_initial_call=True,
)
def export_csv_callback(n_clicks, table_data):
    if not table_data:
        raise dash.exceptions.PreventUpdate

    df = pd.DataFrame(table_data)
    if "job_url" in df.columns:
        df["job_url"] = df["job_url"].str.extract(r"\(([^)]+)\)", expand=False).fillna(df["job_url"])

    filename = f"warm-job-radar-results-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return dcc.send_data_frame(df.to_csv, filename, index=False)


# ---------------------------------------------------------------------------
# Callback: referral message
# ---------------------------------------------------------------------------

@app.callback(
    Output("referral-card", "style"),
    Output("referral-text", "children"),
    Input("results-table", "selected_rows"),
    State("results-table", "data"),
    prevent_initial_call=True,
)
def open_referral_callback(selected_rows, table_data):
    if not selected_rows or not table_data:
        return {"display": "none"}, ""
    row        = table_data[selected_rows[0]]
    role_title = row.get("role_title", "")
    if role_title == "—" or not role_title:
        return {"display": "none"}, ""
    message = search_mod.generate_referral_message(
        row.get("company", ""), role_title,
        row.get("connection_name", ""), row.get("connection_title", ""),
    )
    return {"display": "block"}, html.Pre(message, style={"whiteSpace": "pre-wrap", "fontSize": "13px"})


@app.callback(
    Output("referral-card", "style", allow_duplicate=True),
    Output("results-table", "selected_rows"),
    Input("close-referral-btn", "n_clicks"),
    prevent_initial_call=True,
)
def close_referral_callback(n_clicks):
    return {"display": "none"}, []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
