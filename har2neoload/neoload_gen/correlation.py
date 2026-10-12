"""Automatic correlation: find server-generated values reused in later requests.

For every value sent by the client (query/form/JSON parameters, auth headers,
dynamic-looking URL path segments) we look backwards through earlier
responses. If the value was first produced by the server, we create a NeoLoad
variable extractor on that response and replace the hard-coded value in every
later request with ``${variable}``.
"""
from __future__ import annotations

import html
import json
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, quote_plus, unquote_plus, urlsplit

from .models import Correlation, Exchange

TOKEN_NAME_RE = re.compile(
    r"csrf|xsrf|token|nonce|viewstate|eventvalidation|session|sid$|^sid|state$|signature|"
    r"verification|authenticity|ticket|jsessionid|auth|code_verifier|^code$|requestid|correlation|txn|wpnonce",
    re.I,
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SKIP_HEADERS = {"cookie", "host", "content-length", "connection", "user-agent", "accept", "accept-language",
                "accept-encoding", "referer", "origin", "content-type", "cache-control", "pragma",
                "if-none-match", "if-modified-since", "upgrade-insecure-requests", "priority", "dnt"}
SPECIAL = set("\\.^$|?*+()[]{}")


# ---------------------------------------------------------------- helpers
def looks_dynamic(name: str, value: str) -> bool:
    v = (value or "").strip()
    if len(v) < 4 or len(v) > 4000 or "${" in v:
        return False
    if v.lower() in {"true", "false", "null", "none", "undefined", "on", "off"}:
        return False
    if TOKEN_NAME_RE.search(name or ""):
        return True
    if UUID_RE.match(v):
        return True
    has_digit = any(c.isdigit() for c in v)
    has_alpha = any(c.isalpha() for c in v)
    if " " in v:
        return False
    if v.isdigit():
        return len(v) >= 5
    if len(v) >= 16 and (has_digit or not v.isalpha()):
        return True
    return len(v) >= 6 and has_digit and has_alpha


def regex_escape(text: str) -> str:
    """Java- and Python-compatible literal escaping; whitespace runs become \\s+."""
    out: List[str] = []
    in_ws = False
    for ch in text:
        if ch.isspace():
            if not in_ws:
                out.append(r"\s+")
            in_ws = True
            continue
        in_ws = False
        out.append("\\" + ch if ch in SPECIAL else ch)
    return "".join(out)


def json_leaves(obj, segs=None) -> Iterable[Tuple[list, object]]:
    segs = segs or []
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from json_leaves(v, segs + [k])
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from json_leaves(v, segs + [i])
    else:
        yield segs, obj


def jsonpath_of(segs: list) -> str:
    p = "$"
    for s in segs:
        if isinstance(s, int):
            p += f"[{s}]"
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s):
            p += f".{s}"
        else:
            p += "['" + s.replace("'", "\\'") + "']"
    return p


def try_json(text: str):
    t = (text or "").lstrip()
    if not t or t[0] not in "{[":
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def is_form_body(e: Exchange, body: str) -> bool:
    if "x-www-form-urlencoded" in e.req_content_type:
        return True
    b = (body or "").strip()
    return bool(b) and b[0] not in "{[<" and "=" in b and "\n" not in b


def request_candidates(e: Exchange) -> List[Tuple[str, str, str]]:
    """(location, name, value) of client-sent values in the working copy."""
    out: List[Tuple[str, str, str]] = []
    parts = urlsplit(e.out_url)
    for n, v in parse_qsl(parts.query, keep_blank_values=True):
        out.append(("query", n, v))
    segs = [unquote_plus(s) for s in parts.path.split("/") if s]
    for n, seg in enumerate(segs):
        if UUID_RE.match(seg) or (seg.isdigit() and len(seg) >= 4) or (
                len(seg) >= 8 and any(c.isdigit() for c in seg) and "." not in seg):
            prev = segs[n - 1] if n else "path"
            out.append(("path", f"{prev}_id", seg))
    body = e.out_body or ""
    js = try_json(body)
    if js is not None:
        for segs, v in json_leaves(js):
            if isinstance(v, (str, int)) and not isinstance(v, bool):
                out.append(("json", str(segs[-1]) if segs else "body", str(v)))
    elif is_form_body(e, body):
        for n, v in parse_qsl(body, keep_blank_values=True):
            out.append(("form", n, v))
    for k, v in e.out_headers:
        lk = k.lower()
        if lk in SKIP_HEADERS or lk.startswith("sec-"):
            continue
        if lk == "authorization":
            m = re.match(r"(?i)(bearer|token)\s+(.+)", v)
            if m:
                out.append(("header", "auth_token", m.group(2).strip()))
            continue
        out.append(("header", k, v))
    return out


def variants(value: str) -> List[Tuple[str, Optional[str]]]:
    vs: List[Tuple[str, Optional[str]]] = [(value, None)]
    for alt, dec in ((quote(value, safe=""), "url"), (quote_plus(value), "url"),
                     (html.escape(value, quote=True), "html"), (value.replace("/", "\\/"), "json")):
        if alt != value and all(alt != x for x, _ in vs):
            vs.append((alt, dec))
    return vs


# ---------------------------------------------------------------- extractor builders
def _boundary_regex(text: str, needle: str, case_insensitive: bool = False) -> Optional[Tuple[str, int]]:
    k = text.find(needle)
    if k < 0:
        return None
    end = k + len(needle)
    best = None
    for lb_len in (24, 32, 48, 12):
        lb = text[max(0, k - lb_len):k]
        lb = lb.split("\n")[-1]
        cut = next((n for n, ch in enumerate(lb) if ch in ' <>&?/,;"\''), -1)
        if 0 <= cut and len(lb) - cut - 1 >= 6:
            lb = lb[cut + 1:]
        if not lb.strip():
            continue
        rb = text[end:end + 1]
        if rb in ("", "\r", "\n"):
            core = r"([^\s\"'<>&;,]+)"
            pattern = regex_escape(lb) + core
        elif rb.isalnum():
            rb = text[end:end + 3].split("\n")[0]
            pattern = regex_escape(lb) + "(.+?)" + regex_escape(rb)
        else:
            pattern = regex_escape(lb) + "(.+?)" + regex_escape(rb)
        if case_insensitive:
            pattern = "(?i)" + pattern
        try:
            matches = [m.group(1) for m in re.finditer(pattern, text)]
        except re.error:
            continue
        if needle in matches:
            mn = matches.index(needle) + 1
            if best is None or mn < best[1]:
                best = (pattern, mn)
            if mn == 1:
                break
    return best


def _json_extract(resp_body: str, value: str) -> Optional[list]:
    js = try_json(resp_body)
    if js is None:
        return None
    for segs, v in json_leaves(js):
        if not isinstance(v, bool) and v is not None and str(v) == value and segs:
            return segs
    return None


def build_extractor(src: Exchange, value: str) -> Optional[dict]:
    segs = _json_extract(src.resp_body, value)
    if segs is not None:
        return {"kind": "jsonpath", "expression": jsonpath_of(segs), "match": 1, "decode": None, "segs": segs}
    for needle, dec in variants(value):
        if dec == "json":
            continue
        if needle in src.resp_body:
            r = _boundary_regex(src.resp_body, needle)
            if r:
                return {"kind": "regexp", "expression": r[0], "match": r[1], "decode": dec, "segs": None}
    htext = src.resp_headers_text()
    for needle, dec in variants(value):
        if dec == "json":
            continue
        if needle in htext:
            r = _boundary_regex(htext, needle, case_insensitive=True)
            if r:
                return {"kind": "header", "expression": r[0], "match": r[1], "decode": dec, "segs": None}
    return None


def hidden_field_extractor(src: Exchange, name: str, value: str) -> Optional[dict]:
    """Form-field correlation: a request value that the previous page served as an <input>."""
    body = src.resp_body
    if "<input" not in body.lower():
        return None
    n, v = re.escape(name), re.escape(html.escape(value, quote=True))
    patterns = [
        r'name="' + regex_escape(name) + r'"[^>]*?value="([^"]*)"',
        r'value="([^"]*)"[^>]*?name="' + regex_escape(name) + '"',
    ]
    for pat, check in zip(patterns, (rf'name="{n}"[^>]*?value="{v}"', rf'value="{v}"[^>]*?name="{n}"')):
        if not re.search(check, body):
            continue
        matches = [html.unescape(m.group(1)) for m in re.finditer(pat, body)]
        if value in matches:
            return {"kind": "regexp", "expression": pat, "match": matches.index(value) + 1,
                    "decode": "html" if html.escape(value, quote=True) != value else None, "segs": None}
    return None


def _found_in_response(src: Exchange, value: str) -> bool:
    hay = src.resp_body + "\n" + src.resp_headers_text()
    return any(n in hay for n, _ in variants(value))


def _sent_before(exchanges: List[Exchange], upto_pos: int, value: str) -> bool:
    for e in exchanges[:upto_pos + 1]:
        if value in e.req_body or value in e.url or any(value in v for _, v in e.req_headers):
            return True
    return False


# ---------------------------------------------------------------- substitution
def _sub(text: str, value: str, var: str, encoded_ok: bool) -> Tuple[str, bool]:
    changed = False
    ref = "${" + var + "}"
    if encoded_ok:
        for enc in (quote(value, safe=""), quote_plus(value)):
            if enc != value and enc in text:
                text = text.replace(enc, f"__encodeURL({ref})")
                changed = True
    if value in text:
        text = text.replace(value, ref)
        changed = True
    jv = value.replace("/", "\\/")
    if jv != value and jv in text:
        text = text.replace(jv, ref)
        changed = True
    return text, changed


def substitute(e: Exchange, value: str, var: str) -> bool:
    changed = False
    parts = urlsplit(e.out_url)
    base = e.out_url[: len(e.out_url) - len(parts.query) - (1 if parts.query else 0)]
    base, c1 = _sub(base, value, var, encoded_ok=False)
    query, c2 = _sub(parts.query, value, var, encoded_ok=True)
    if c1 or c2:
        e.out_url = base + ("?" + query if query else "")
        changed = True
    if e.out_body:
        body, c3 = _sub(e.out_body, value, var, encoded_ok=is_form_body(e, e.out_body))
        if c3:
            e.out_body = body
            changed = True
    new_headers = []
    for k, v in e.out_headers:
        if k.lower() != "cookie":
            v2, c4 = _sub(v, value, var, encoded_ok=False)
            if c4:
                changed = True
                v = v2
        new_headers.append((k, v))
    e.out_headers = new_headers
    return changed


def _sub_param(e: Exchange, name: str, value: str, var: str) -> bool:
    """Replace only the value of form/query field `name` (values like 'UA954' may appear elsewhere)."""
    ref = "__encodeURL(${" + var + "})"
    changed = False

    def rep(qs: str) -> str:
        nonlocal changed
        out = []
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if unquote_plus(k) == name and unquote_plus(v) == value:
                    pair = f"{k}={ref}"
                    changed = True
            out.append(pair)
        return "&".join(out)

    parts = urlsplit(e.out_url)
    if parts.query:
        e.out_url = e.out_url[: len(e.out_url) - len(parts.query)] + rep(parts.query)
    if e.out_body and is_form_body(e, e.out_body):
        e.out_body = rep(e.out_body)
    return changed


def _var_name(param: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", param or "").strip("_").lower()
    if not s or s[0].isdigit():
        s = "v_" + s
    return "c_" + s[:40]


# ---------------------------------------------------------------- main entry
def correlate(exchanges: List[Exchange], disabled_values: Optional[set] = None) -> Tuple[List[Correlation], List[dict]]:
    """Mutates the exchanges' out_* fields. Returns (correlations, warnings).

    ``disabled_values`` lets the user switch off individual correlations.
    """
    pos = {e.idx: i for i, e in enumerate(exchanges)}
    correlations: List[Correlation] = []
    handled: set = set(disabled_values or ())
    last_use: Dict[str, int] = {}
    warnings: List[dict] = []
    warned: set = set()

    for i, e in enumerate(exchanges):
        for loc, name, value in request_candidates(e):
            if value in handled or not value or "${" in value:
                continue
            if loc in ("form", "query"):
                hidden = None
                for j in range(i - 1, -1, -1):
                    hidden = hidden_field_extractor(exchanges[j], name, value)
                    if hidden:
                        break
                if hidden:
                    src = exchanges[j]
                    var = _var_name(name)
                    corr = Correlation(var=var, value=value, source_idx=src.idx, kind="regexp",
                                       expression=hidden["expression"], match_number=hidden["match"],
                                       decode=hidden["decode"], param_name=name)
                    for later in exchanges[i:]:
                        if _sub_param(later, name, value, var):
                            corr.used_in.append(later.idx)
                    if corr.used_in:
                        correlations.append(corr)
                        last_use[var] = max(corr.used_in)
                    continue
            if not looks_dynamic(name, value):
                continue
            src_pos = None
            for j in range(i - 1, -1, -1):
                if _found_in_response(exchanges[j], value):
                    src_pos = j
                    break
            if src_pos is None or _sent_before(exchanges, src_pos, value):
                if TOKEN_NAME_RE.search(name) and (name, value) not in warned and src_pos is None:
                    warned.add((name, value))
                    warnings.append({
                        "request": e.short_label(), "parameter": name, "location": loc,
                        "value": value[:60],
                        "reason": "Looks dynamic but was not found in any earlier response "
                                  "(may be generated by JavaScript or come from an excluded host).",
                    })
                continue
            src = exchanges[src_pos]
            ex = build_extractor(src, value)
            if ex is None:
                continue
            var = _var_name(name)
            base, n = var, 2
            while var in last_use and last_use[var] > src.idx:
                var = f"{base}_{n}"
                n += 1
            corr = Correlation(
                var=var, value=value, source_idx=src.idx, kind=ex["kind"], expression=ex["expression"],
                match_number=ex["match"], decode=ex["decode"], param_name=name, json_segments=ex["segs"],
            )
            for later in exchanges[i:]:
                if substitute(later, value, var):
                    corr.used_in.append(later.idx)
            if corr.used_in:
                correlations.append(corr)
                last_use[var] = max(corr.used_in)
            handled.add(value)
    return correlations, warnings


def reset_working_copies(exchanges: List[Exchange]) -> None:
    for e in exchanges:
        e.out_url, e.out_headers, e.out_body = e.url, list(e.req_headers), e.req_body
