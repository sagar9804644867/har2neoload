"""Host/noise filtering and transaction grouping."""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from typing import Dict, List

from .models import Exchange, Transaction
from .parsers import page_title_from_html

# Browser telemetry, analytics, ads and OS update traffic seen in real recordings
NOISE_PATTERNS = [
    r"(^|\.)bing\.com$", r"(^|\.)msn\.com$", r"microsoft\.com$", r"(^|\.)live\.com$",
    r"msftconnecttest\.com$", r"azureedge\.net$", r"skype\.com$", r"office\.(com|net)$",
    r"windows\.(com|net)$", r"msedge\.net$", r"edge\.microsoft", r"onecollector",
    r"google-analytics\.com$", r"googletagmanager\.com$", r"doubleclick\.net$",
    r"googlesyndication\.com$", r"gstatic\.com$", r"googleapis\.com$", r"google\.com$",
    r"fonts\.(googleapis|gstatic)\.com$", r"facebook\.(com|net)$", r"hotjar\.com$",
    r"newrelic\.com$", r"nr-data\.net$", r"segment\.(io|com)$", r"mixpanel\.com$",
    r"mozilla\.(com|net|org)$", r"firefox\.com$", r"mcafee\.com$", r"wps\.com$",
    r"keen\.io$", r"prodperfect\.com$", r"clarity\.ms$", r"cloudflareinsights\.com$",
    r"sentry\.io$", r"optimizely\.com$", r"adobe(dtm)?\.com$", r"demdex\.net$",
]
_NOISE_RE = [re.compile(p, re.I) for p in NOISE_PATTERNS]

STATIC_EXT = re.compile(
    r"\.(js|mjs|css|png|jpe?g|gif|svg|ico|webp|avif|bmp|woff2?|ttf|otf|eot|map|mp4|webm|mp3|wav|pdf)$",
    re.I,
)
NOISE_PATHS = re.compile(r"/(OneCollector|autofillservice|componentupdater|filestreamingservice|collect|beacon)\b", re.I)


def is_noise_host(host: str) -> bool:
    return any(r.search(host) for r in _NOISE_RE)


def host_summary(exchanges: List[Exchange]) -> List[dict]:
    counts = Counter(e.host for e in exchanges)
    return [
        {"host": h, "requests": n, "suggested": not is_noise_host(h)}
        for h, n in counts.most_common()
    ]


def is_static(e: Exchange) -> bool:
    if STATIC_EXT.search(e.path):
        return True
    ct = e.resp_content_type
    return any(t in ct for t in ("image/", "font/", "text/css", "javascript", "video/", "audio/"))


def filter_exchanges(
    exchanges: List[Exchange],
    keep_hosts: List[str],
    drop_static: bool = True,
    drop_options: bool = True,
    drop_failed_noise: bool = True,
) -> List[Exchange]:
    keep = set(h.lower() for h in keep_hosts)
    out = []
    for e in exchanges:
        if e.host not in keep:
            continue
        if drop_options and e.method == "OPTIONS":
            continue
        if drop_static and is_static(e):
            continue
        if drop_failed_noise and NOISE_PATHS.search(e.path):
            continue
        out.append(e)
    return out


# ---------------------------------------------------------------- grouping
def _is_page_start(e: Exchange) -> bool:
    ct = e.resp_content_type
    if "text/html" in ct:
        return True
    accept = e.req_header("accept").lower()
    return e.req_header("sec-fetch-mode").lower() == "navigate" or accept.startswith("text/html")


def _slug(text: str, limit: int = 30) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    return (s[:limit] or "Step").strip("_")


def _tx_name(n: int, e: Exchange) -> str:
    title = e.page_title or page_title_from_html(e.resp_body)
    seg = [p for p in e.path.split("/") if p]
    seg = [p for p in seg if not (re.search(r"\d", p) and (len(p) >= 4 or p.isdigit()))] or seg
    label = seg[-1].rsplit(".", 1)[0] if seg else "Home"
    if title and len(title) <= 30 and label == "Home":
        label = title
    return f"T{n:02d}_{_slug(label)}"


def group_transactions(exchanges: List[Exchange], gap_s: float = 1.5, max_think_s: int = 30) -> List[Transaction]:
    """Split the request list into business transactions.

    A new transaction starts when the HAR page reference changes, when a new
    HTML document is requested, or when the user paused longer than `gap_s`.
    """
    groups: List[List[Exchange]] = []
    prev = None
    for e in exchanges:
        new = prev is None
        if prev is not None:
            if e.page_ref and prev.page_ref and e.page_ref != prev.page_ref:
                new = True
            elif _is_page_start(e) and e.method in ("GET", "POST"):
                new = True
            elif e.started_ms and prev.started_ms and (e.started_ms - (prev.started_ms + prev.duration_ms)) > gap_s * 1000:
                new = True
        if new:
            groups.append([e])
        else:
            groups[-1].append(e)
        prev = e

    txs: List[Transaction] = []
    used: Dict[str, int] = {}
    for i, g in enumerate(groups):
        name = _tx_name(i + 1, g[0])
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        think = 0
        if i + 1 < len(groups):
            last = g[-1]
            nxt = groups[i + 1][0]
            if last.started_ms and nxt.started_ms:
                gap = (nxt.started_ms - (last.started_ms + last.duration_ms)) / 1000
                think = int(min(max(gap, 0), max_think_s))
        container = "Actions"
        if i == 0 and len(groups) > 1:
            container = "Init"
        if i == len(groups) - 1 and re.search(r"log_?out|sign_?out", g[0].path, re.I):
            container = "End"
        txs.append(Transaction(name=name, container=container, exchange_ids=[e.idx for e in g], think_time_s=think))
    return txs
