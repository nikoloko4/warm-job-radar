"""
Warm Job Radar — Dash application.
Run with: python app.py  →  http://127.0.0.1:8050
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

_lock  = threading.Lock()
_state : dict = {
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
# Company normalisation
# ---------------------------------------------------------------------------

_NORM_SUFFIXES = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|corporation|company|group|"
    r"holdings?|international|technologies?|solutions?|services?|"
    r"systems?|enterprises?|ventures?|labs?)\b\.?",
    re.IGNORECASE,
)

def _normalise_company(name: str) -> str:
    n = _NORM_SUFFIXES.sub("", name)
    n = re.sub(r"[^a-z0-9\s]", "", n.lower())
    return re.sub(r"\s+", " ", n).strip()

# ---------------------------------------------------------------------------
# App
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
            html.Div(h["job_title"], className="fw-semibold small"),
            html.Div(
                f"{h['timestamp']} · {h['result_count']} matches",
                className="text-muted", style={"fontSize": "11px"},
            ),
        ], className="mb-3")
        for h in history
    ]

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

app.layout = dbc.Container(fluid=True, style={"maxWidth": "1400px", "paddingBottom": "60px"}, children=[

    dcc.Store(id="csv-store"),
    dcc.Store(id="company-map-store"),
    dcc.Download(id="download-csv"),
    dcc.Interval(id="poll-interval", interval=2000, disabled=True),

    # Header
    dbc.Row(className="align-items-center border-bottom py-3 mb-4", children=[
        dbc.Col(html.H4("Warm Job Radar", className="fw-bold text-primary mb-0"), width="auto"),
        dbc.Col(html.Span("Find open roles at companies where you have LinkedIn connections",
                          className="text-muted"), width="auto"),
    ]),

    dbc.Row([

        # ── Left sidebar: history ─────────────────────────────────────────
        dbc.Col(width=2, children=[
            html.Div("Search History",
                     className="text-uppercase text-muted fw-semibold mb-2",
                     style={"fontSize": "11px", "letterSpacing": "0.05em"}),
            html.Div(id="history-sidebar"),
        ]),

        # ── Main ──────────────────────────────────────────────────────────
        dbc.Col(width=10, children=[

            # Resume banner
            dbc.Alert(
                id="resume-banner", color="warning", is_open=False,
                className="mb-3 py-2 d-flex align-items-center gap-3",
                children=[
                    html.Span(id="resume-banner-text", className="flex-grow-1 small"),
                    dbc.Button("Resume", id="resume-btn",
                               color="warning", size="sm"),
                    dbc.Button("Dismiss", id="dismiss-resume-btn",
                               color="link", size="sm", className="text-muted p-0"),
                ],
            ),

            # Controls card
            dbc.Card(className="mb-4 shadow-sm", children=[
                dbc.CardBody(className="py-3", children=[
                    dbc.Row(className="g-3 align-items-end", children=[
                        dbc.Col(width=4, children=[
                            dbc.Label("LinkedIn Connections CSV", className="fw-semibold small mb-1"),
                            dcc.Upload(
                                id="csv-upload",
                                children=dbc.Button("Choose file…", color="secondary",
                                                    outline=True, size="sm"),
                            ),
                            html.Div(id="upload-status", className="mt-1"),
                        ]),
                        dbc.Col(width=3, children=[
                            dbc.Label("Job Title", className="fw-semibold small mb-1"),
                            dbc.Input(id="job-title-input",
                                      placeholder="e.g. Customer Success Manager",
                                      type="text", size="sm"),
                        ]),
                        dbc.Col(width=3, children=[
                            dbc.Label("Location", className="fw-semibold small mb-1"),
                            dbc.Input(id="location-input",
                                      placeholder="e.g. United States",
                                      type="text", value="United States", size="sm"),
                        ]),
                        dbc.Col(width=2, children=[
                            dbc.Button("Search", id="search-btn",
                                       color="primary", size="sm", className="me-2"),
                            dbc.Button("Export", id="export-btn",
                                       color="success", outline=True, size="sm"),
                        ]),
                    ]),
                ]),
            ]),

            # Progress
            html.Div(id="progress-area", className="mb-3", children=[
                dbc.Progress(id="progress-bar", value=0, striped=True, animated=True,
                             style={"height": "6px"}, className="mb-1"),
                html.Div(id="progress-label", className="text-muted", style={"fontSize": "12px"}),
            ]),

            # Results table
            dash_table.DataTable(
                id="results-table",
                columns=[
                    {"name": "Company",      "id": "company"},
                    {"name": "Role",         "id": "role_title"},
                    {"name": "Connections",  "id": "connection_name"},
                    {"name": "Source",       "id": "source"},
                    {"name": "Link",         "id": "job_url", "presentation": "markdown"},
                ],
                data=[],
                page_size=20,
                style_table={"overflowX": "auto"},
                style_header={
                    "fontWeight": "600",
                    "backgroundColor": "#f1f3f5",
                    "fontSize": "12px",
                    "textTransform": "uppercase",
                    "letterSpacing": "0.04em",
                    "padding": "8px 12px",
                    "borderBottom": "2px solid #dee2e6",
                },
                style_cell={
                    "textAlign": "left",
                    "padding": "8px 12px",
                    "fontSize": "13px",
                    "borderBottom": "1px solid #f1f3f5",
                    "verticalAlign": "top",
                    "maxWidth": "0",        # required for textOverflow to work
                    "overflow": "hidden",
                    "textOverflow": "ellipsis",
                    "whiteSpace": "nowrap",
                },
                style_cell_conditional=[
                    {"if": {"column_id": "company"},         "width": "14%", "minWidth": "120px"},
                    {"if": {"column_id": "role_title"},      "width": "28%", "minWidth": "200px",
                     "whiteSpace": "normal", "overflow": "visible"},
                    {"if": {"column_id": "connection_name"}, "width": "32%", "minWidth": "180px",
                     "whiteSpace": "normal", "overflow": "visible"},
                    {"if": {"column_id": "source"},          "width": "14%", "minWidth": "100px"},
                    {"if": {"column_id": "job_url"},         "width": "6%",  "minWidth": "50px"},
                ],
                style_data_conditional=[
                    {"if": {"state": "active"},
                     "backgroundColor": "#e8f4fd", "border": "1px solid #0d6efd"},
                ],
                markdown_options={"link_target": "_blank"},
                active_cell=None,
            ),

            # Connections expand panel
            dbc.Collapse(id="connections-expand", is_open=False, className="mt-2", children=[
                dbc.Card(className="border-0 bg-light", children=[
                    dbc.CardBody(className="py-2 px-3", children=[
                        html.Span("All connections at this company: ",
                                  className="fw-semibold small"),
                        html.Div(id="connections-expand-text",
                                 className="text-muted small mt-1"),
                    ]),
                ]),
            ]),

        ]),
    ]),
])


# ---------------------------------------------------------------------------
# Callback: resume banner on load
# ---------------------------------------------------------------------------

@app.callback(
    Output("resume-banner", "is_open"),
    Output("resume-banner-text", "children"),
    Input("poll-interval", "id"),
)
def check_checkpoint_on_load(_):
    cp = search_mod.load_checkpoint()
    if not cp:
        return False, ""
    done  = len(cp.get("done_companies", []))
    label = (
        f"Unfinished search: \"{cp['job_title']}\" in {cp['location']} — "
        f"{done} companies done."
    )
    return True, label


# ---------------------------------------------------------------------------
# Callback: parse CSV
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
        decoded   = base64.b64decode(content_string).decode("utf-8", errors="replace")
        lines     = decoded.splitlines()
        header_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("First Name")), None,
        )
        if header_idx is None:
            return None, None, dbc.Alert(
                "Could not find 'First Name' column. Is this a LinkedIn connections CSV?",
                color="danger", className="mb-0 py-1 small",
            )

        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
        required = [c for c in ["First Name", "Last Name", "Company", "Position"] if c in df.columns]
        df = df[required].dropna(subset=["Company"]).fillna("")

        norm_to_canonical: dict[str, str]        = {}
        company_map:       dict[str, list[dict]] = {}

        for _, row in df.iterrows():
            raw = str(row.get("Company", "")).strip()
            if not raw:
                continue
            norm = _normalise_company(raw)
            if norm not in norm_to_canonical:
                norm_to_canonical[norm] = raw
            canonical = norm_to_canonical[norm]
            name  = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
            title = str(row.get("Position", "")).strip()
            company_map.setdefault(canonical, []).append({"name": name, "title": title})

        total_raw     = df["Company"].nunique()
        total_deduped = len(company_map)
        total_conns   = sum(len(v) for v in company_map.values())
        dedup_note    = (
            f" · {total_raw - total_deduped} duplicates merged"
            if total_raw > total_deduped else ""
        )
        status = html.Small(
            f"{filename} · {total_conns} connections at {total_deduped} companies{dedup_note}",
            className="text-success",
        )
        return df.to_json(date_format="iso"), json.dumps(company_map), status

    except Exception as exc:
        return None, None, dbc.Alert(
            f"Error: {exc}", color="danger", className="mb-0 py-1 small",
        )


# ---------------------------------------------------------------------------
# Helper: launch search
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
# Callback: start search
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
    return False, 0, f"Searching {len(company_map)} companies…", False


# ---------------------------------------------------------------------------
# Callback: resume
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
        return True, 0, "Please upload your LinkedIn CSV first.", False
    _launch_search(company_map, cp["job_title"], cp["location"], resume_from=cp)
    remaining = len(company_map) - len(cp.get("done_companies", []))
    return False, 0, f"Resuming — {remaining} companies remaining…", False


# ---------------------------------------------------------------------------
# Callback: dismiss resume banner
# ---------------------------------------------------------------------------

@app.callback(
    Output("resume-banner", "is_open", allow_duplicate=True),
    Input("dismiss-resume-btn", "n_clicks"),
    prevent_initial_call=True,
)
def dismiss_resume_callback(_):
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

    done  = min(done, total)
    pct   = int(done / total * 100) if total > 0 else 0
    label = f"{done} / {total} companies checked" if total > 0 else ""

    # One row per (company, role) — deduplicate connections
    seen: dict[tuple, dict] = {}
    for r in results:
        if r.get("role_title", "—") == "—":
            continue
        key = (r["company"], r["role_title"])
        if key not in seen:
            seen[key] = dict(r)
            seen[key]["_conn_set"] = set()
            seen[key]["_conn_list"] = []
        label_full = r["connection_name"]
        if r.get("connection_title"):
            label_full += f" ({r['connection_title']})"
        if label_full not in seen[key]["_conn_set"]:
            seen[key]["_conn_set"].add(label_full)
            seen[key]["_conn_list"].append(label_full)

    table_rows = []
    for row in seen.values():
        conns = row.pop("_conn_list")
        row.pop("_conn_set", None)
        # Store full list with newline separator (safe for commas in titles)
        row["all_connections"] = "\n".join(conns)
        display = conns[:3]
        if len(conns) > 3:
            display.append(f"+{len(conns) - 3} more")
        row["connection_name"] = ", ".join(display)
        row.pop("connection_title", None)
        url = row.get("job_url", "")
        if url and url.startswith("http"):
            row["job_url"] = f"[↗]({url})"
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
# Callback: expand connections on cell click
# ---------------------------------------------------------------------------

@app.callback(
    Output("connections-expand", "is_open"),
    Output("connections-expand-text", "children"),
    Input("results-table", "active_cell"),
    State("results-table", "data"),
    prevent_initial_call=True,
)
def on_cell_click(active_cell, table_data):
    if not active_cell or not table_data:
        return False, ""

    row       = table_data[active_cell["row"]]
    all_conns = row.get("all_connections", "")
    conns     = [c for c in all_conns.split("\n") if c]

    if len(conns) <= 3:
        return False, ""

    expand_content = [html.Div(c, className="py-1 border-bottom") for c in conns]
    return True, expand_content


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
