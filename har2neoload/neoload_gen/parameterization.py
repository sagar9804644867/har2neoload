"""Automatic parameterization of user-entered data into NeoLoad file variables."""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Dict, List
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from .correlation import is_form_body, json_leaves, try_json
from .models import Exchange, ParamCandidate

CREDENTIAL_RE = re.compile(r"user(name)?|login|e-?mail|passw(or)?d|pwd|pass$|^pass|account|userid|uid$", re.I)
USER_INPUT_RE = re.compile(
    r"user|login|e-?mail|pass|pwd|search|query|^q$|keyword|term|from|to(port)?$|city|country|date|"
    r"name|first|last|phone|mobile|address|street|zip|postal|card|cvv|cvc|expir|amount|qty|quantity|"
    r"product|sku|item|price|comment|message|title|description|dob|age|gender|seat|passenger|"
    r"origin|destination|depart|return|guest",
    re.I,
)
SKIP_NAMES = re.compile(r"^(submit|button|btn|action|_?method|lang|locale|format|callback|_)$", re.I)


def _col(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    if not s or s[0].isdigit():
        s = "p_" + s
    return s[:40]


def detect_parameters(exchanges: List[Exchange]) -> List[ParamCandidate]:
    found: "OrderedDict[str, ParamCandidate]" = OrderedDict()

    def add(loc: str, name: str, value: str, e: Exchange) -> None:
        if not name or value is None or "${" in value or len(value) > 200 or value == "":
            return
        key = f"{loc}|{name}|{value}"
        if key in found:
            if e.idx not in found[key].exchange_ids:
                found[key].exchange_ids.append(e.idx)
            return
        group = "credentials" if CREDENTIAL_RE.search(name) else "testdata"
        enabled = bool(USER_INPUT_RE.search(name)) and not SKIP_NAMES.match(name)
        found[key] = ParamCandidate(key=key, location=loc, name=name, value=value, exchange_ids=[e.idx],
                                    group=group, column=_col(name), enabled=enabled)

    for e in exchanges:
        for n, v in parse_qsl(urlsplit(e.out_url).query, keep_blank_values=True):
            add("query", n, v, e)
        js = try_json(e.out_body)
        if js is not None:
            for segs, v in json_leaves(js):
                if isinstance(v, str) and segs and isinstance(segs[-1], str):
                    add("json", segs[-1], v, e)
        elif e.out_body and is_form_body(e, e.out_body):
            for n, v in parse_qsl(e.out_body, keep_blank_values=True):
                add("form", n, v, e)

    # unique column names per group
    seen: Dict[str, set] = {}
    for p in found.values():
        cols = seen.setdefault(p.group, set())
        base, col, n = p.column, p.column, 2
        while col in cols:
            col = f"{base}_{n}"
            n += 1
        p.column = col
        cols.add(col)
    return list(found.values())


def _replace_pairs(qs: str, name: str, value: str, ref: str) -> str:
    out = []
    for pair in qs.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if unquote_plus(k) == name and unquote_plus(v) == value:
                pair = f"{k}=__encodeURL({ref})"
        out.append(pair)
    return "&".join(out)


def apply_parameters(exchanges: List[Exchange], params: List[ParamCandidate]) -> None:
    by_id = {e.idx: e for e in exchanges}
    for p in params:
        if not p.enabled:
            continue
        ref = "${" + f"{p.group}.{p.column}" + "}"
        for idx in p.exchange_ids:
            e = by_id.get(idx)
            if e is None:
                continue
            if p.location == "query":
                parts = urlsplit(e.out_url)
                if parts.query:
                    q = _replace_pairs(parts.query, p.name, p.value, ref)
                    e.out_url = e.out_url[: len(e.out_url) - len(parts.query)] + q
            elif p.location == "form":
                e.out_body = _replace_pairs(e.out_body, p.name, p.value, ref)
            elif p.location == "json":
                pat = re.compile(r'("' + re.escape(p.name) + r'"\s*:\s*)' + re.escape(json.dumps(p.value)))
                e.out_body = pat.sub(lambda m: m.group(1) + '"' + ref + '"', e.out_body)


def data_files(params: List[ParamCandidate], extra_rows: Dict[str, List[Dict[str, str]]] | None = None) -> Dict[str, List[Dict[str, str]]]:
    """Group -> rows (first row = recorded values, then user-supplied rows)."""
    files: Dict[str, List[Dict[str, str]]] = {}
    for p in params:
        if not p.enabled:
            continue
        rows = files.setdefault(p.group, [{}])
        rows[0][p.column] = p.value
    for group, rows in (extra_rows or {}).items():
        if group in files:
            for r in rows:
                files[group].append({c: str(r.get(c, "")) for c in files[group][0]})
    return files
