"""Parse HAR (browser DevTools / proxies) and SAZ (Fiddler) recordings."""
from __future__ import annotations

import base64
import gzip
import io
import json
import re
import zipfile
import zlib
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from .models import Exchange, Header

try:  # optional
    import brotli  # type: ignore
except Exception:  # pragma: no cover
    brotli = None

MAX_BODY_CHARS = 2_000_000
TEXT_HINTS = ("text", "json", "xml", "javascript", "html", "x-www-form-urlencoded", "graphql")


class RecordingError(ValueError):
    pass


def _is_text(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return not ct or any(h in ct for h in TEXT_HINTS)


def _parse_iso_ms(value: str) -> float:
    if not value:
        return 0.0
    v = value.strip().replace("Z", "+00:00")
    # trim fractional seconds to 6 digits for fromisoformat
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    try:
        return datetime.fromisoformat(v).timestamp() * 1000
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- HAR
def parse_har(data: bytes) -> List[Exchange]:
    try:
        har = json.loads(data.decode("utf-8-sig", errors="replace"))
        log = har["log"]
        entries = log.get("entries", [])
    except Exception as exc:
        raise RecordingError(f"Not a valid HAR file: {exc}") from exc

    titles = {p.get("id"): p.get("title") for p in log.get("pages", []) or []}
    out: List[Exchange] = []
    for e in entries:
        req, resp = e.get("request", {}), e.get("response", {})
        method = req.get("method", "GET").upper()
        url = req.get("url", "")
        if not url or method == "CONNECT":
            continue
        req_headers = [(h.get("name", ""), h.get("value", "")) for h in req.get("headers", [])]
        req_headers = [(k, v) for k, v in req_headers if k and not k.startswith(":")]
        body = ""
        pd = req.get("postData") or {}
        if pd.get("text") is not None:
            body = pd.get("text") or ""
        elif pd.get("params"):
            from urllib.parse import quote_plus

            body = "&".join(f"{quote_plus(p.get('name',''))}={quote_plus(p.get('value',''))}" for p in pd["params"])

        content = resp.get("content", {}) or {}
        mime = content.get("mimeType", "") or ""
        text = content.get("text") or ""
        if text and content.get("encoding") == "base64":
            if _is_text(mime):
                try:
                    text = base64.b64decode(text).decode("utf-8", errors="replace")
                except Exception:
                    text = ""
            else:
                text = ""
        if not _is_text(mime):
            text = ""
        resp_headers = [(h.get("name", ""), h.get("value", "")) for h in resp.get("headers", [])]
        out.append(
            Exchange(
                idx=len(out) + 1,
                started_ms=_parse_iso_ms(e.get("startedDateTime", "")),
                duration_ms=float(e.get("time") or 0),
                method=method,
                url=url,
                req_headers=req_headers,
                req_body=body[:MAX_BODY_CHARS],
                status=int(resp.get("status") or 0),
                resp_headers=[(k, v) for k, v in resp_headers if k and not k.startswith(":")],
                resp_body=text[:MAX_BODY_CHARS],
                mime=mime,
                page_ref=e.get("pageref"),
                page_title=titles.get(e.get("pageref")),
            )
        )
    if not out:
        raise RecordingError("The HAR file contains no HTTP entries.")
    return out


# ---------------------------------------------------------------- SAZ
def _split_http(raw: bytes) -> Tuple[str, List[Header], bytes]:
    sep = raw.find(b"\r\n\r\n")
    sep_len = 4
    if sep < 0:
        sep = raw.find(b"\n\n")
        sep_len = 2
    if sep < 0:
        head, body = raw, b""
    else:
        head, body = raw[:sep], raw[sep + sep_len:]
    lines = head.decode("iso-8859-1").splitlines()
    first = lines[0] if lines else ""
    headers: List[Header] = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append((k.strip(), v.strip()))
    return first, headers, body


def _hget(headers: List[Header], name: str) -> str:
    for k, v in headers:
        if k.lower() == name.lower():
            return v
    return ""


def _dechunk(body: bytes) -> bytes:
    out, pos = bytearray(), 0
    while pos < len(body):
        end = body.find(b"\r\n", pos)
        if end < 0:
            break
        size_txt = body[pos:end].split(b";")[0].strip()
        try:
            size = int(size_txt, 16)
        except ValueError:
            return body
        if size == 0:
            break
        out += body[end + 2:end + 2 + size]
        pos = end + 2 + size + 2
    return bytes(out)


def _decode_body(headers: List[Header], body: bytes) -> str:
    if "chunked" in _hget(headers, "transfer-encoding").lower():
        body = _dechunk(body)
    enc = _hget(headers, "content-encoding").lower()
    try:
        if "gzip" in enc:
            body = gzip.decompress(body)
        elif "deflate" in enc:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        elif "br" in enc and brotli is not None:
            body = brotli.decompress(body)
    except Exception:
        return ""
    if not _is_text(_hget(headers, "content-type")):
        return ""
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", _hget(headers, "content-type"), re.I)
    if m:
        charset = m.group(1)
    try:
        return body.decode(charset, errors="replace")[:MAX_BODY_CHARS]
    except LookupError:
        return body.decode("utf-8", errors="replace")[:MAX_BODY_CHARS]


def parse_saz(data: bytes) -> List[Exchange]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RecordingError("Not a valid SAZ file (Fiddler archives are zip files).") from exc

    names = zf.namelist()
    ids = sorted(
        {m.group(1) for n in names if (m := re.match(r"raw/(\d+)_c\.txt$", n))},
        key=int,
    )
    out: List[Exchange] = []
    for sid in ids:
        c_raw = zf.read(f"raw/{sid}_c.txt")
        s_name = f"raw/{sid}_s.txt"
        s_raw = zf.read(s_name) if s_name in names else b""
        m_name = f"raw/{sid}_m.xml"
        meta = zf.read(m_name).decode("utf-8", errors="replace") if m_name in names else ""

        first, req_headers, req_body = _split_http(c_raw)
        parts = first.split(" ")
        if len(parts) < 2:
            continue
        method, target = parts[0].upper(), parts[1]
        if method == "CONNECT":
            continue
        if target.startswith("http://") or target.startswith("https://"):
            url = target
        else:
            host = _hget(req_headers, "host")
            scheme = "https" if re.search(r'x-(?:client|server)port"?\s*[^>]*443|https', meta, re.I) else "http"
            url = f"{scheme}://{host}{target}"

        status, resp_headers, resp_body_txt = 0, [], ""
        if s_raw:
            sfirst, resp_headers, sbody = _split_http(s_raw)
            sp = sfirst.split(" ")
            if len(sp) >= 2 and sp[1].isdigit():
                status = int(sp[1])
            resp_body_txt = _decode_body(resp_headers, sbody)

        req_txt = _decode_body(req_headers, req_body) if req_body else ""

        started = 0.0
        ended = 0.0
        mt = re.search(r'ClientBeginRequest="([^"]+)"', meta)
        if mt:
            started = _parse_iso_ms(mt.group(1))
        me = re.search(r'ClientDoneResponse="([^"]+)"', meta)
        if me:
            ended = _parse_iso_ms(me.group(1))

        out.append(
            Exchange(
                idx=len(out) + 1,
                started_ms=started,
                duration_ms=max(0.0, ended - started) if started and ended else 0.0,
                method=method,
                url=url,
                req_headers=req_headers,
                req_body=req_txt,
                status=status,
                resp_headers=resp_headers,
                resp_body=resp_body_txt,
                mime=_hget(resp_headers, "content-type"),
            )
        )
    if not out:
        raise RecordingError("The SAZ archive contains no sessions.")
    return out


def parse_recording(filename: str, data: bytes) -> List[Exchange]:
    name = filename.lower()
    if name.endswith(".saz") or data[:2] == b"PK":
        return parse_saz(data)
    return parse_har(data)


def page_title_from_html(html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if not m:
        return None
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    return t or None


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
