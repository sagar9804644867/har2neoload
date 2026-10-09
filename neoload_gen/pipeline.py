"""End-to-end orchestration used by the Streamlit app and the tests."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .correlation import correlate, reset_working_copies
from .exporters import (IMPORT_GUIDE, ExportOptions, build_as_code, build_postman, build_report, build_zip,
                        to_csv, to_yaml)
from .filtering import filter_exchanges, group_transactions, host_summary
from .models import Correlation, Exchange, ParamCandidate, Transaction
from .parameterization import apply_parameters, data_files, detect_parameters


@dataclass
class Result:
    exchanges: List[Exchange]
    transactions: List[Transaction]
    correlations: List[Correlation]
    warnings: List[dict]
    params: List[ParamCandidate]
    files: Dict[str, List[Dict[str, str]]]
    yaml_text: str
    postman: dict
    report: str
    zip_bytes: bytes


def run(
    recorded: List[Exchange],
    keep_hosts: List[str],
    opts: ExportOptions,
    drop_static: bool = True,
    drop_options: bool = True,
    gap_s: float = 1.5,
    transactions: Optional[List[Transaction]] = None,
    disabled_correlations: Optional[set] = None,
    param_overrides: Optional[Dict[str, dict]] = None,
    extra_rows: Optional[Dict[str, List[Dict[str, str]]]] = None,
    data_overrides: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> Result:
    exchanges = [copy.deepcopy(e) for e in filter_exchanges(recorded, keep_hosts, drop_static, drop_options)]
    reset_working_copies(exchanges)
    txs = transactions if transactions is not None else group_transactions(exchanges, gap_s)

    correlations, warnings = correlate(exchanges, disabled_correlations)
    params = detect_parameters(exchanges)
    for p in params:
        ov = (param_overrides or {}).get(p.key)
        if ov:
            p.enabled = ov.get("enabled", p.enabled)
            p.group = ov.get("group", p.group) or p.group
            p.column = ov.get("column", p.column) or p.column
    apply_parameters(exchanges, params)
    files = data_files(params, extra_rows)
    for group, rows in (data_overrides or {}).items():
        if group in files and files[group]:
            cols = list(files[group][0].keys())
            clean = [{c: "" if r.get(c) is None else str(r.get(c)) for c in cols} for r in rows]
            clean = [r for r in clean if any(v.strip() for v in r.values())]
            if clean:
                files[group] = clean

    doc = build_as_code(exchanges, txs, correlations, files, opts)
    yaml_text = to_yaml(doc)
    postman = build_postman(exchanges, txs, correlations, files, opts)
    stats = {"project": opts.project_name, "total": len(recorded), "hosts": keep_hosts}
    report = build_report(exchanges, txs, correlations, params, warnings, stats)

    root = opts.project_name
    out = {
        f"{root}/neoload_project/default.yaml": yaml_text,
        f"{root}/neoload_project/HOW_TO_USE.md": IMPORT_GUIDE,
        f"{root}/postman/collection.json": json.dumps(postman, indent=2),
        f"{root}/correlation_report.md": report,
    }
    merged_row: Dict[str, str] = {}
    for group, rows in files.items():
        out[f"{root}/neoload_project/data/{group}.csv"] = to_csv(rows)
        merged_row.update(rows[0] if rows else {})
    if merged_row:
        out[f"{root}/postman/data.csv"] = to_csv([merged_row])
    return Result(exchanges, txs, correlations, warnings, params, files, yaml_text, postman, report, build_zip(out))


__all__ = ["run", "Result", "ExportOptions", "host_summary", "group_transactions", "filter_exchanges"]
