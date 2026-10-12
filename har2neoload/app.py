"""PerfPilot HAR2NeoLoad — turn a HAR/SAZ recording into a correlated, parameterized NeoLoad project.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pandas as pd
import streamlit as st

from neoload_gen import ExportOptions, RecordingError, parse_recording, run
from neoload_gen.filtering import filter_exchanges, group_transactions, host_summary
from neoload_gen.models import Transaction

HERE = pathlib.Path(__file__).parent
SAMPLE = HERE / "samples" / "sample_blazedemo.har"

st.set_page_config(page_title="HAR2NeoLoad", page_icon="⚡", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; max-width: 1250px;}
      .step {font-size: 0.78rem; letter-spacing: .08em; text-transform: uppercase; color: #6b7280; margin-bottom: -0.4rem;}
      div[data-testid="stMetricValue"] {font-size: 1.6rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner="Parsing recording…", max_entries=5)
def _parse(name: str, data: bytes):
    return parse_recording(name, data)


def _slug(s: str, default: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s or "").strip("_")
    return s or default


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    project = _slug(st.text_input("Project name", "MyApp_PerfTest"), "MyApp_PerfTest")
    user_path = _slug(st.text_input("User Path name", "UserPath1"), "UserPath1")
    st.subheader("Filtering")
    drop_static = st.checkbox("Exclude static resources (js, css, images, fonts)", True)
    drop_options = st.checkbox("Exclude CORS preflight (OPTIONS)", True)
    gap_s = st.slider("New transaction after a pause of (s)", 0.5, 10.0, 1.5, 0.5,
                      help="Requests are also split on every new HTML page / HAR page.")
    st.subheader("Script options")
    use_servers = st.checkbox("Create NeoLoad Server objects (relative URLs)", True,
                              help="Lets you switch environments by changing the server host only.")
    add_assertions = st.checkbox("Add page-title validations", True)
    think_times = st.checkbox("Keep recorded think times", True)
    keep_browser_headers = st.checkbox("Keep browser-only headers (sec-*, accept-language)", False)
    st.subheader("Load scenario")
    users = st.number_input("Virtual users", 1, 100000, 10)
    duration = st.number_input("Duration (minutes)", 1, 1440, 10)
    rampup = st.number_input("Ramp-up (minutes)", 1, 240, 2)
    st.caption("Recordings are processed in memory and are not stored.")

opts = ExportOptions(project_name=project, user_path_name=user_path, use_servers=use_servers,
                     add_assertions=add_assertions, keep_browser_headers=keep_browser_headers,
                     think_times=think_times, load_users=int(users), load_duration_min=int(duration),
                     load_rampup_min=int(rampup))

# ---------------------------------------------------------------- header + upload
st.title("⚡ HAR2NeoLoad")
st.write("Upload a **HAR** (browser DevTools) or **SAZ** (Fiddler) recording. The app removes noise, "
         "auto-correlates dynamic values, parameterizes user input and gives you a ready NeoLoad as-code project.")

c1, c2 = st.columns([3, 1])
with c1:
    up = st.file_uploader("Recording file", type=["har", "saz", "json"], label_visibility="collapsed")
with c2:
    use_sample = st.toggle("Use sample recording", value=False, help="BlazeDemo flow with CSRF, session, JWT and IDs.")

if up is not None:
    name, data = up.name, up.getvalue()
elif use_sample and SAMPLE.exists():
    name, data = SAMPLE.name, SAMPLE.read_bytes()
else:
    st.info("**How to capture a HAR:** open DevTools (F12) → Network → tick *Preserve log* → run your business flow → "
            "right-click the request list → *Save all as HAR with content*.  \n"
            "**Fiddler:** File → Save → All Sessions (.saz).")
    st.stop()

try:
    recorded = _parse(name, data)
except RecordingError as exc:
    st.error(str(exc))
    st.stop()

rec_key = hashlib.sha1(data).hexdigest()[:12]
if st.session_state.get("rec_key") != rec_key:
    for k in list(st.session_state.keys()):
        if k.startswith(("tx_", "corr_", "par_", "data_", "hosts_")):
            del st.session_state[k]
    st.session_state["rec_key"] = rec_key

# ---------------------------------------------------------------- step 1: hosts
st.markdown('<p class="step">Step 1</p>', unsafe_allow_html=True)
st.subheader("Select the application hosts")
hs = pd.DataFrame(host_summary(recorded)).rename(columns={"suggested": "include"})
hs = hs[["include", "host", "requests"]]
hosts_df = st.data_editor(
    hs, key=f"hosts_{rec_key}", hide_index=True, width="stretch",
    column_config={"include": st.column_config.CheckboxColumn("Include", width="small"),
                   "host": st.column_config.TextColumn("Host", disabled=True),
                   "requests": st.column_config.NumberColumn("Requests", disabled=True, width="small")},
)
keep_hosts = hosts_df.loc[hosts_df["include"], "host"].tolist()
st.caption("Browser telemetry, analytics and update hosts are unticked automatically — keep only the system under test.")
if not keep_hosts:
    st.warning("Select at least one host.")
    st.stop()

filtered = filter_exchanges(recorded, keep_hosts, drop_static, drop_options)
if not filtered:
    st.warning("No requests left after filtering. Include more hosts or allow static resources.")
    st.stop()

# ---------------------------------------------------------------- step 2: transactions
st.markdown('<p class="step">Step 2</p>', unsafe_allow_html=True)
st.subheader("Review transactions")
sig = f"tx_{rec_key}_{'|'.join(sorted(keep_hosts))}_{drop_static}_{drop_options}_{gap_s}"
txs = group_transactions(filtered, gap_s)
by_id = {e.idx: e for e in filtered}
tx_df = pd.DataFrame([{
    "container": t.container, "name": t.name, "think_s": t.think_time_s,
    "requests": len(t.exchange_ids),
    "first_request": by_id[t.exchange_ids[0]].method + " " + by_id[t.exchange_ids[0]].path,
} for t in txs])
tx_edit = st.data_editor(
    tx_df, key=sig, hide_index=True, width="stretch",
    column_config={
        "container": st.column_config.SelectboxColumn("Container", options=["Init", "Actions", "End"], width="small"),
        "name": st.column_config.TextColumn("Transaction name"),
        "think_s": st.column_config.NumberColumn("Think time after (s)", min_value=0, max_value=99, width="small"),
        "requests": st.column_config.NumberColumn("Requests", disabled=True, width="small"),
        "first_request": st.column_config.TextColumn("First request", disabled=True),
    },
)
st.caption("Tip: put login in **Init**, the business flow in **Actions**, logout in **End**. Rename transactions to business steps.")
transactions = [
    Transaction(name=_slug(str(row["name"]), t.name), container=row["container"] or "Actions",
                exchange_ids=t.exchange_ids, think_time_s=int(row["think_s"] or 0))
    for t, (_, row) in zip(txs, tx_edit.iterrows())
]

# ---------------------------------------------------------------- step 3: correlations
st.markdown('<p class="step">Step 3</p>', unsafe_allow_html=True)
st.subheader("Auto-correlation")
probe = run(recorded, keep_hosts, opts, drop_static, drop_options, gap_s, transactions)
corr_df = pd.DataFrame([{
    "enabled": True, "variable": c.var, "parameter": c.param_name,
    "source": by_id[c.source_idx].short_label() if c.source_idx in by_id else c.source_idx,
    "type": c.kind + (f" ({c.decode} decode)" if c.decode else ""), "expression": c.expression,
    "match": c.match_number, "used_in": ", ".join(f"#{u}" for u in c.used_in),
    "sample_value": c.value[:40] + ("…" if len(c.value) > 40 else ""), "_value": c.value,
} for c in probe.correlations])

disabled = set()
if corr_df.empty:
    st.info("No server-generated values were reused in later requests.")
else:
    corr_edit = st.data_editor(
        corr_df.drop(columns=["_value"]), key=f"corr_{sig}", hide_index=True, width="stretch",
        disabled=[c for c in corr_df.columns if c != "enabled"],
        column_config={"enabled": st.column_config.CheckboxColumn("Use", width="small")},
    )
    disabled = set(corr_df.loc[~corr_edit["enabled"].values, "_value"])
if probe.warnings:
    with st.expander(f"⚠️ {len(probe.warnings)} value(s) need manual review", expanded=True):
        st.dataframe(pd.DataFrame(probe.warnings), hide_index=True, width="stretch")

# ---------------------------------------------------------------- step 4: parameters
st.markdown('<p class="step">Step 4</p>', unsafe_allow_html=True)
st.subheader("Auto-parameterization")
base = run(recorded, keep_hosts, opts, drop_static, drop_options, gap_s, transactions, disabled)
par_df = pd.DataFrame([{
    "enabled": p.enabled, "field": p.name, "location": p.location,
    "recorded_value": "******" if re.search(r"pass|pwd", p.name, re.I) else p.value,
    "file_variable": p.group, "column": p.column, "requests": len(p.exchange_ids), "_key": p.key,
} for p in base.params])
overrides = {}
if par_df.empty:
    st.info("No user-input fields detected.")
else:
    par_edit = st.data_editor(
        par_df.drop(columns=["_key"]), key=f"par_{sig}", hide_index=True, width="stretch",
        disabled=["field", "location", "recorded_value", "requests"],
        column_config={
            "enabled": st.column_config.CheckboxColumn("Parameterize", width="small"),
            "file_variable": st.column_config.SelectboxColumn("File variable", options=["credentials", "testdata"]),
            "column": st.column_config.TextColumn("Column"),
        },
    )
    for key, (_, row) in zip(par_df["_key"], par_edit.iterrows()):
        overrides[key] = {"enabled": bool(row["enabled"]), "group": row["file_variable"],
                          "column": _slug(str(row["column"]), "col")}
    st.caption("`credentials` → unique value per virtual user (each_user). `testdata` → new row every iteration.")

mid = run(recorded, keep_hosts, opts, drop_static, drop_options, gap_s, transactions, disabled, overrides)
data_overrides = {}
if mid.files:
    st.markdown("**Test data** — add rows for more users and data variety (first row = recorded values).")
    cols = st.columns(len(mid.files))
    for col, (group, rows) in zip(cols, mid.files.items()):
        with col:
            st.caption(f"data/{group}.csv")
            edited = st.data_editor(pd.DataFrame(rows), key=f"data_{sig}_{group}_{'|'.join(rows[0].keys())}",
                                    num_rows="dynamic", hide_index=True, width="stretch")
            data_overrides[group] = edited.fillna("").to_dict("records")

final = run(recorded, keep_hosts, opts, drop_static, drop_options, gap_s, transactions, disabled, overrides,
            data_overrides=data_overrides)

# ---------------------------------------------------------------- step 5: output
st.markdown('<p class="step">Step 5</p>', unsafe_allow_html=True)
st.subheader("Download your NeoLoad project")
m = st.columns(5)
m[0].metric("Recorded", len(recorded))
m[1].metric("Kept", len(final.exchanges))
m[2].metric("Transactions", len(final.transactions))
m[3].metric("Correlations", len(final.correlations))
m[4].metric("Parameters", sum(1 for p in final.params if p.enabled))

st.download_button("⬇️ Download NeoLoad project (.zip)", final.zip_bytes, file_name=f"{project}_neoload.zip",
                   mime="application/zip", type="primary", width="stretch")

t1, t2, t3, t4 = st.tabs(["default.yaml", "Correlation report", "Postman collection", "Request details"])
with t1:
    st.code(final.yaml_text, language="yaml")
with t2:
    st.markdown(final.report)
with t3:
    st.code(json.dumps(final.postman, indent=2)[:60000], language="json")
with t4:
    pick = st.selectbox("Request", final.exchanges, format_func=lambda e: e.short_label())
    a, b = st.columns(2)
    with a:
        st.markdown("**Recorded**")
        st.code(f"{pick.method} {pick.url}\n\n" + "\n".join(f"{k}: {v}" for k, v in pick.req_headers)
                + (f"\n\n{pick.req_body}" if pick.req_body else ""), language="http")
    with b:
        st.markdown("**Generated**")
        st.code(f"{pick.method} {pick.out_url}\n\n" + "\n".join(f"{k}: {v}" for k, v in pick.out_headers)
                + (f"\n\n{pick.out_body}" if pick.out_body else ""), language="http")
    st.markdown(f"**Response** — HTTP {pick.status}, {pick.resp_content_type or 'n/a'}")
    st.code(pick.resp_body[:5000] or "(no text body)", language="html")

with st.expander("How to use the project in NeoLoad", expanded=True):
    st.markdown(
        "**Open directly in NeoLoad (recommended):** unzip, then in NeoLoad use *File > Open* and pick "
        f"`NeoLoad_GUI_Project/{project}/{project}.nlp`. The user path (Init / Actions / End), extractors, "
        "File variables, population and the **Load_Test** and **Smoke_1VU** scenarios are already in place. "
        "Run **Smoke_1VU** (or Check User Path) first, then **Load_Test**.\n\n"
        "Generated in the project format of NeoLoad 2026.2 (project version 8.11).\n\n"
        "Other formats in the zip: `neoload_project/default.yaml` (as-code, for NeoLoadCmd / NeoLoad Web) and "
        "`postman/collection.json`."
    )
