"""Exporters: NeoLoad as-code YAML project, Postman collection, report, zip."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import yaml

from .models import Correlation, Exchange, ParamCandidate, Transaction
from .parsers import page_title_from_html

DROP_HEADERS = {"cookie", "host", "content-length", "connection", "proxy-connection", "keep-alive",
                "if-none-match", "if-modified-since", "priority", "te", "upgrade-insecure-requests"}


@dataclass
class ExportOptions:
    project_name: str = "HarToNeoLoad"
    user_path_name: str = "UserPath1"
    use_servers: bool = True
    add_assertions: bool = True
    keep_browser_headers: bool = False
    think_times: bool = True
    load_users: int = 10
    load_duration_min: int = 10
    load_rampup_min: int = 2


# ---------------------------------------------------------------- YAML helpers
class _Dumper(yaml.SafeDumper):
    pass


def _str_repr(dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_repr)


def _dur(minutes: int) -> str:
    minutes = max(1, int(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m}m"
    return f"{h}h" if h else f"{m}m"


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return s or "server"


def server_defs(exchanges: List[Exchange]) -> Dict[tuple, str]:
    names: Dict[tuple, str] = {}
    for e in exchanges:
        p = urlsplit(e.url)
        key = (p.scheme, p.hostname, p.port)
        if key not in names:
            names[key] = _slug(p.hostname or "server")
    return names


def _headers_for(e: Exchange, opts: ExportOptions) -> List[dict]:
    out = []
    for k, v in e.out_headers:
        lk = k.lower()
        if lk in DROP_HEADERS or k.startswith(":"):
            continue
        if not opts.keep_browser_headers and (lk.startswith("sec-") or lk in ("dnt", "accept-language")):
            continue
        out.append({k: v})
    return out


def _assertion_for(e: Exchange) -> Optional[dict]:
    if not (200 <= e.status < 300) or "html" not in e.resp_content_type:
        return None
    title = page_title_from_html(e.resp_body)
    if not title or len(title) > 80 or "${" in title:
        return None
    return {"contains": title}


def build_request(e: Exchange, opts: ExportOptions, servers: Dict[tuple, str],
                  extractors: List[dict]) -> dict:
    p = urlsplit(e.url)
    req: dict = {}
    if opts.use_servers:
        out = urlsplit(e.out_url)
        rel = e.out_url[len(f"{out.scheme}://{out.netloc}"):] or "/"
        req["url"] = rel
        req["server"] = servers[(p.scheme, p.hostname, p.port)]
    else:
        req["url"] = e.out_url
    if e.method != "GET":
        req["method"] = e.method
    headers = _headers_for(e, opts)
    if headers:
        req["headers"] = headers
    if e.out_body and e.method in ("POST", "PUT", "PATCH", "DELETE"):
        req["body"] = e.out_body
    if extractors:
        req["extractors"] = extractors
    if opts.add_assertions:
        a = _assertion_for(e)
        if a:
            req["assertions"] = [a]
    return req


def build_as_code(
    exchanges: List[Exchange],
    transactions: List[Transaction],
    correlations: List[Correlation],
    files: Dict[str, List[Dict[str, str]]],
    opts: ExportOptions,
) -> dict:
    by_id = {e.idx: e for e in exchanges}
    extractors_by_src: Dict[int, List[dict]] = {}
    for c in correlations:
        extractors_by_src.setdefault(c.source_idx, []).append(c.extractor())

    servers = server_defs(exchanges)
    containers: Dict[str, List[dict]] = {"Init": [], "Actions": [], "End": []}
    txs = list(transactions)
    if txs and not any(t.container == "Actions" for t in txs):
        for t in txs:
            t.container = "Actions"
    for t in txs:
        steps = []
        for idx in t.exchange_ids:
            e = by_id.get(idx)
            if e is None:
                continue
            steps.append({"request": build_request(e, opts, servers, extractors_by_src.get(idx, []))})
        if not steps:
            continue
        containers[t.container].append({"transaction": {"name": t.name, "steps": steps}})
        if opts.think_times and t.think_time_s > 0:
            containers[t.container].append({"think_time": f"{min(t.think_time_s, 99)}s"})

    user_path: dict = {"name": opts.user_path_name}
    for key, label in (("init", "Init"), ("actions", "Actions"), ("end", "End")):
        if containers[label]:
            user_path[key] = {"steps": containers[label]}

    doc: dict = {"name": opts.project_name}
    variables = []
    for group, rows in files.items():
        if not rows or not rows[0]:
            continue
        unique = group == "credentials"
        variables.append({"file": {
            "name": group,
            "is_first_line_column_names": True,
            "delimiter": ",",
            "path": f"data/{group}.csv",
            "change_policy": "each_user" if unique else "each_iteration",
            "scope": "unique" if unique else "global",
            "order": "sequential",
            "out_of_value": "cycle",
        }})
    if variables:
        doc["variables"] = variables
    if opts.use_servers:
        srv = []
        for (scheme, host, port), name in servers.items():
            s = {"name": name, "host": host, "scheme": scheme}
            if port:
                s["port"] = port
            srv.append(s)
        doc["servers"] = srv
    doc["user_paths"] = [user_path]
    pop = f"pop_{opts.user_path_name}"
    doc["populations"] = [{"name": pop, "user_paths": [{"name": opts.user_path_name}]}]
    doc["scenarios"] = [
        {"name": "Smoke_1VU", "populations": [{"name": pop, "constant_load": {"users": 1, "duration": "1 iterations"}}]},
        {"name": "Load_Test", "populations": [{"name": pop, "constant_load": {
            "users": int(opts.load_users), "duration": _dur(opts.load_duration_min), "rampup": _dur(opts.load_rampup_min)}}]},
    ]
    return doc


def to_yaml(doc: dict) -> str:
    header = ("# NeoLoad as-code project generated from a HAR/SAZ recording\n"
              "# Schema: https://github.com/Neotys-Labs/neoload-models (as-code v3)\n")
    return header + yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)


def to_csv(rows: List[Dict[str, str]]) -> str:
    buf = io.StringIO()
    if not rows:
        return ""
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# ---------------------------------------------------------------- Postman
_REF = re.compile(r"__encodeURL\((\$\{[^}]+\})\)")
_VAR = re.compile(r"\$\{(?:[A-Za-z0-9_]+\.)?([A-Za-z0-9_]+)\}")


def _pm(text: str) -> str:
    """Keep NeoLoad ${var} syntax: NeoLoad's Postman import keeps literal ${...}
    text, while unknown {{var}} placeholders are imported as empty strings."""
    return _REF.sub(r"\1", text or "")


def _js_accessor(segs: list) -> str:
    return "".join(f"[{s}]" if isinstance(s, int) else f"[{json.dumps(s)}]" for s in segs)


def _pm_script(c: Correlation) -> List[str]:
    if c.kind == "jsonpath" and c.json_segments is not None:
        return [f'pm.collectionVariables.set("{c.var}", String(pm.response.json(){_js_accessor(c.json_segments)}));']
    pattern = c.expression
    flags = "g"
    if pattern.startswith("(?i)"):
        pattern, flags = pattern[4:], "gi"
    src = ("pm.response.headers.map(function(h){return h.key + ': ' + h.value;}).join('\\n')"
           if c.kind == "header" else "pm.response.text()")
    lines = [
        f"(function(){{ var re = new RegExp({json.dumps(pattern)}, {json.dumps(flags)}); var m, n = 0, txt = {src};",
        f"  while ((m = re.exec(txt)) !== null) {{ n++; if (n === {c.match_number}) {{ var v = m[1];",
    ]
    if c.decode == "url":
        lines.append("    v = decodeURIComponent(v.replace(/\\+/g, ' '));")
    elif c.decode == "html":
        lines.append("    v = v.replace(/&amp;/g,'&').replace(/&quot;/g,'\"').replace(/&#x27;|&#39;/g,\"'\").replace(/&lt;/g,'<').replace(/&gt;/g,'>');")
    lines.append(f'    pm.collectionVariables.set("{c.var}", v); break; }} }} }})();')
    return lines


def build_postman(exchanges: List[Exchange], transactions: List[Transaction], correlations: List[Correlation],
                  files: Dict[str, List[Dict[str, str]]], opts: ExportOptions) -> dict:
    by_id = {e.idx: e for e in exchanges}
    corr_by_src: Dict[int, List[Correlation]] = {}
    for c in correlations:
        corr_by_src.setdefault(c.source_idx, []).append(c)
    folders = []
    for t in transactions:
        items = []
        for idx in t.exchange_ids:
            e = by_id.get(idx)
            if e is None:
                continue
            req: dict = {
                "method": e.method,
                "header": [{"key": k, "value": _pm(v)} for d in _headers_for(e, opts) for k, v in d.items()],
                "url": {"raw": _pm(e.out_url)},
            }
            if e.out_body and e.method in ("POST", "PUT", "PATCH", "DELETE"):
                req["body"] = {"mode": "raw", "raw": _pm(e.out_body)}
            item = {"name": f"{e.method} {e.path}", "request": req}
            exec_lines: List[str] = []
            for c in corr_by_src.get(idx, []):
                exec_lines += _pm_script(c)
            if exec_lines:
                item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": exec_lines}}]
            items.append(item)
        if items:
            folders.append({"name": t.name, "item": items})
    variables = [{"key": c.var, "value": ""} for c in {c.var: c for c in correlations}.values()]
    for rows in files.values():
        for col, val in (rows[0] if rows else {}).items():
            variables.append({"key": col, "value": val})
    return {
        "info": {"name": opts.project_name,
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": folders,
        "variable": variables,
    }


# ---------------------------------------------------------------- report
def build_report(exchanges: List[Exchange], transactions: List[Transaction], correlations: List[Correlation],
                 params: List[ParamCandidate], warnings: List[dict], stats: dict) -> str:
    by_id = {e.idx: e for e in exchanges}
    L = [f"# Correlation and parameterization report - {stats.get('project','')}", ""]
    L.append(f"- Recorded requests: {stats.get('total', 0)}; kept after filtering: {len(exchanges)}")
    L.append(f"- Hosts kept: {', '.join(stats.get('hosts', []))}")
    L.append(f"- Transactions: {len(transactions)}; correlations: {len(correlations)}; "
             f"parameterized fields: {sum(1 for p in params if p.enabled)}")
    L += ["", "## Transactions", "", "| Container | Transaction | Requests | Think time |", "|---|---|---|---|"]
    for t in transactions:
        L.append(f"| {t.container} | {t.name} | {len(t.exchange_ids)} | {t.think_time_s}s |")
    L += ["", "## Correlations (variable extractors)", ""]
    if correlations:
        L += ["| Variable | Extracted from | Type | Expression | Match # | Used in |", "|---|---|---|---|---|---|"]
        for c in correlations:
            src = by_id[c.source_idx].short_label() if c.source_idx in by_id else c.source_idx
            expr = c.expression.replace("|", "\\|")
            used = ", ".join(f"#{u}" for u in c.used_in)
            L.append(f"| `{c.var}` | {src} | {c.kind}{' + ' + c.decode + ' decode' if c.decode else ''} | `{expr}` | {c.match_number} | {used} |")
    else:
        L.append("No server-generated values were found to correlate.")
    L += ["", "## Parameterized fields", ""]
    enabled = [p for p in params if p.enabled]
    if enabled:
        L += ["| File variable | Column | Field | Location | Recorded value |", "|---|---|---|---|---|"]
        for p in enabled:
            v = "******" if re.search(r"pass|pwd", p.name, re.I) else p.value
            L.append(f"| {p.group} | `${{{p.group}.{p.column}}}` | {p.name} | {p.location} | {v} |")
    else:
        L.append("No fields parameterized.")
    L += ["", "## Needs manual review", ""]
    if warnings:
        for w in warnings:
            L.append(f"- {w['request']}: `{w['parameter']}` ({w['location']}) - {w['reason']}")
    else:
        L.append("Nothing flagged.")
    L += ["", "Always run **Check User Path** in NeoLoad before a load test and compare it with the recording.", ""]
    return "\n".join(L)


def import_guide(project: str, files: Dict[str, List[Dict[str, str]]]) -> str:
    """HOW_TO_USE.md, written only with ASCII so it renders in any editor."""
    data_lines = []
    if "credentials" in files:
        data_lines.append("- data/credentials.csv: one row per virtual user (scope = unique). "
                          "Add as many rows as the number of VUs you will run.")
    if "testdata" in files:
        data_lines.append("- data/testdata.csv: one row per iteration (scope = global). Add rows for data variety.")
    if not data_lines:
        data_lines.append("- No fields were parameterized, so there are no data files.")
    return f"""# Using {project} in NeoLoad

This zip gives you the same script in two forms:

| File | What it is | How you use it |
|---|---|---|
| neoload_project/default.yaml | NeoLoad as-code project (user path, extractors, variables, servers, scenarios) | NeoLoadCmd or NeoLoad Web |
| postman/collection.json | Same requests as a Postman collection | NeoLoad GUI: User Path > Postman import |

Note: the NeoLoad GUI (Design view) cannot open a YAML file directly. Use Route A to work in the GUI,
or Route B / C to run the YAML as it is.

## Route A - NeoLoad GUI
1. In NeoLoad, create a project, then User Paths > New User Path > Postman import.
2. Select postman/collection.json (and postman/data.csv if asked).
3. Add the variable extractors listed in correlation_report.md (variable name, request, regex or JSONPath).
4. Create the File variables from neoload_project/data/*.csv.
5. Run Check User Path.

## Route B - NeoLoadCmd with your project
1. Copy default.yaml and the data folder next to your .nlp file.
2. Run:
   NeoLoadCmd -project "<path>\\MyProject.nlp" "<path>\\default.yaml" -launch Smoke_1VU -noGUI
   The user paths, variables, servers and scenarios from the YAML are added to the project for that run.

## Route C - NeoLoad Web
Zip the neoload_project folder and upload it in "Run a test". default.yaml is loaded automatically.

## Scenarios in default.yaml
- Smoke_1VU: 1 user, 1 iteration. Run this first to validate the script.
- Load_Test: constant load with ramp-up (values chosen in the app).

## Test data
{chr(10).join(data_lines)}

## Before you load test
- Read correlation_report.md, especially "Needs manual review".
- Always validate with 1 VU and compare responses with the recording.
"""


def build_zip(files_map: Dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files_map.items():
            z.writestr(path, content)
    return buf.getvalue()
