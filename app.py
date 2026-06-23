"""
Warm Job Radar — main Dash application.
Run with: python app.py
Then open http://127.0.0.1:8050
"""

import base64
import io
import json
import os
import threading
from datetime import datetime

import dash
import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, State, callback_context, dash_table, dcc, html
from dotenv import load_dotenv

import search as search_mod

load_dotenv()

DAILY_CSE_LIMIT = 100

# ---------------------------------------------------------------------------
# Shared state (single-user local app — threading is safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()

_state: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": 0,
    "results": [],
    "cse_calls_today": 0,
    "cse_date": "",
    "history": [],  # last 10 completed search sessions
}

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
    items = []
    for h in history:
        items.append(
            html.Div([
                html.Span(h["job_title"], className="fw-bold"),
                html.Br(),
                html.Small(f"{h['timestamp']} — {h['result_count']} results", className="text-muted"),
            ], className="mb-2 border-bottom pb-2")
        )
    return items


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
        # --- Left sidebar: search history ---
        dbc.Col(width=2, children=[
            html.H6("Search History", className="text-muted text-uppercase small fw-bold"),
            html.Div(id="history-sidebar"),
        ]),

        # --- Main panel ---
        dbc.Col(width=10, children=[

            # Upload + inputs
            dbc.Card(className="mb-3", children=[
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label("1. Upload LinkedIn Connections CSV", className="fw-bold"),
                            dcc.Upload(
                                id="csv-upload",
                                children=dbc.Button("Choose file", color="secondary", outline=True, size="sm"),
                                className="mb-1",
                            ),
                            html.Div(id="upload-status", className="text-muted small"),
                        ], width=4),

                        dbc.Col([
                            html.Label("2. Job Title", className="fw-bold"),
                            dbc.Input(
                                id="job-title-input",
                                placeholder="e.g. Product Manager",
                                type="text",
                                debounce=False,
                            ),
                        ], width=3),

                        dbc.Col([
                            html.Label("3. Location Filter", className="fw-bold"),
                            dbc.Input(
                                id="location-input",
                                placeholder="e.g. United States",
                                type="text",
                                value="United States",
                                debounce=False,
                            ),
                            html.Small(
                                "Only roles in this location will be returned.",
                                className="text-muted",
                            ),
                        ], width=3),

                        dbc.Col([
                            html.Label(" ", className="d-block"),  # spacer
                            dbc.Button("Search", id="search-btn", color="primary", className="me-2"),
                            dbc.Button("Export CSV", id="export-btn", color="success", outline=True),
                        ], width=2, className="d-flex align-items-start flex-column"),
                    ]),
                ]),
            ]),

            # Progress
            html.Div(id="progress-area", className="mb-2", children=[
                dbc.Progress(id="progress-bar", value=0, max=100, striped=True, animated=True,
                             className="mb-1", style={"height": "20px"}),
                html.Div(id="progress-label", className="small text-muted"),
                html.Div(id="cse-quota-warning", className="small text-warning"),
            ]),

            # Results table
            dash_table.DataTable(
                id="results-table",
                columns=[
                    {"name": "Company",        "id": "company"},
                    {"name": "Matched Role",   "id": "role_title"},
                    {"name": "Connection",     "id": "connection_name"},
                    {"name": "Their Title",    "id": "connection_title"},
                    {"name": "Source",         "id": "source"},
                    {"name": "Job URL",        "id": "job_url", "presentation": "markdown"},
                ],
                data=[],
                row_selectable="single",
                selected_rows=[],
                page_size=25,
                style_table={"overflowX": "auto"},
                style_cell={"textAlign": "left", "padding": "6px 12px", "fontSize": "13px"},
                style_header={"fontWeight": "bold", "backgroundColor": "#f8f9fa"},
                style_data_conditional=[
                    {
                        "if": {"filter_query": '{role_title} = "—"'},
                        "color": "#adb5bd",
                    }
                ],
                markdown_options={"link_target": "_blank"},
            ),

            # Referral message panel
            html.Div(id="referral-panel", className="mt-3", children=[
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

            html.Div(id="referral-trigger-store", style={"display": "none"}),
        ]),
    ]),
], style={"paddingBottom": "60px"})


# ---------------------------------------------------------------------------
# Callback: parse uploaded CSV
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
        content_type, content_string = contents.split(",")
        decoded = base64.b64decode(content_string).decode("utf-8", errors="replace")
        lines = decoded.splitlines()

        # LinkedIn CSV has metadata lines before the actual header
        header_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("First Name")),
            None,
        )
        if header_idx is None:
            return None, None, dbc.Alert("Could not find 'First Name' column. Is this a LinkedIn connections CSV?", color="danger", className="mb-0 py-1 small")

        clean_csv = "\n".join(lines[header_idx:])
        df = pd.read_csv(io.StringIO(clean_csv))

        # Keep only rows with a Company value
        required = [c for c in ["First Name", "Last Name", "Company", "Position"] if c in df.columns]
        df = df[required].dropna(subset=["Company"])
        df = df.fillna("")

        # Build company_map: {company: [{name, title, location}]}
        company_map: dict = {}
        for _, row in df.iterrows():
            company = str(row.get("Company", "")).strip()
            if not company:
                continue
            name = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
            title = str(row.get("Position", "")).strip()
            company_map.setdefault(company, []).append({"name": name, "title": title})

        total_companies = len(company_map)
        total_connections = sum(len(v) for v in company_map.values())

        status = dbc.Alert(
            f"Loaded: {filename} — {total_connections} connections at {total_companies} companies.",
            color="success", className="mb-0 py-1 small",
        )
        return df.to_json(date_format="iso"), json.dumps(company_map), status

    except Exception as exc:
        return None, None, dbc.Alert(f"Error parsing CSV: {exc}", color="danger", className="mb-0 py-1 small")


# ---------------------------------------------------------------------------
# Callback: start search
# ---------------------------------------------------------------------------

@app.callback(
    Output("poll-interval", "disabled"),
    Output("progress-bar", "value"),
    Output("progress-label", "children"),
    Input("search-btn", "n_clicks"),
    State("company-map-store", "data"),
    State("job-title-input", "value"),
    State("location-input", "value"),
    prevent_initial_call=True,
)
def start_search_callback(n_clicks, company_map_json, job_title, location):
    if not n_clicks:
        raise dash.exceptions.PreventUpdate

    if not company_map_json:
        return True, 0, "Please upload a LinkedIn CSV first."

    if not job_title or not job_title.strip():
        return True, 0, "Please enter a job title."

    location = (location or "United States").strip()
    company_map: dict = json.loads(company_map_json)

    with _lock:
        _state["running"] = True
        _state["total"] = len(company_map)
        _state["done"] = 0
        _state["errors"] = 0
        _state["results"] = []

    max_workers = int(os.getenv("MAX_WORKERS", 3))

    def _run():
        search_mod.run_search(company_map, job_title.strip(), location, _state, _lock, max_workers)
        # Persist search history after completion
        with _lock:
            _state["history"].insert(0, {
                "job_title": job_title.strip(),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "result_count": sum(1 for r in _state["results"] if r["role_title"] != "—"),
            })
            _state["history"] = _state["history"][:10]

    threading.Thread(target=_run, daemon=True).start()
    return False, 0, f"Searching {len(company_map)} companies..."


# ---------------------------------------------------------------------------
# Callback: poll for updates
# ---------------------------------------------------------------------------

@app.callback(
    Output("results-table", "data"),
    Output("progress-bar", "value", allow_duplicate=True),
    Output("progress-label", "children", allow_duplicate=True),
    Output("cse-quota-warning", "children"),
    Output("history-sidebar", "children"),
    Output("poll-interval", "disabled", allow_duplicate=True),
    Input("poll-interval", "n_intervals"),
    prevent_initial_call=True,
)
def update_ui_callback(n_intervals):
    with _lock:
        done = _state["done"]
        total = _state["total"]
        running = _state["running"]
        results = list(_state["results"])
        cse_calls = _state.get("cse_calls_today", 0)
        history = list(_state["history"])

    pct = int(done / total * 100) if total > 0 else 0
    label = f"{done} / {total} companies checked" if total > 0 else ""

    if cse_calls >= DAILY_CSE_LIMIT * 0.9:
        cse_warning = f"Warning: {cse_calls}/{DAILY_CSE_LIMIT} Google CSE queries used today."
    else:
        cse_warning = ""

    # Format job_url as markdown link if it's a real URL
    table_rows = []
    for r in results:
        row = dict(r)
        url = row.get("job_url", "")
        if url and url.startswith("http"):
            row["job_url"] = f"[View]({url})"
        table_rows.append(row)

    interval_disabled = not running

    return (
        table_rows,
        pct,
        label,
        cse_warning,
        _history_items(history),
        interval_disabled,
    )


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
    # Strip markdown link syntax from job_url column for clean CSV export
    if "job_url" in df.columns:
        df["job_url"] = df["job_url"].str.extract(r"\(([^)]+)\)", expand=False).fillna(df["job_url"])

    filename = f"warm-job-radar-results-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return dcc.send_data_frame(df.to_csv, filename, index=False)


# ---------------------------------------------------------------------------
# Callback: show referral message modal on row selection
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

    row = table_data[selected_rows[0]]
    company = row.get("company", "")
    role_title = row.get("role_title", "")
    connection_name = row.get("connection_name", "")
    connection_title = row.get("connection_title", "")

    if role_title == "—" or not role_title:
        return {"display": "none"}, ""

    message = search_mod.generate_referral_message(company, role_title, connection_name, connection_title)

    return {"display": "block"}, html.Pre(message, style={"whiteSpace": "pre-wrap", "fontSize": "13px"})


# ---------------------------------------------------------------------------
# Callback: close referral panel
# ---------------------------------------------------------------------------

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
