"""Common data model shared by parsers, engines and exporters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

Header = Tuple[str, str]


@dataclass
class Exchange:
    """One recorded HTTP request/response pair."""

    idx: int
    started_ms: float               # epoch milliseconds (0 when unknown)
    duration_ms: float
    method: str
    url: str
    req_headers: List[Header]
    req_body: str
    status: int
    resp_headers: List[Header]
    resp_body: str
    mime: str = ""
    page_ref: Optional[str] = None
    page_title: Optional[str] = None

    # Working copies that the correlation / parameterization engines rewrite.
    out_url: str = ""
    out_headers: List[Header] = field(default_factory=list)
    out_body: str = ""

    def __post_init__(self) -> None:
        if not self.out_url:
            self.out_url = self.url
        if not self.out_headers:
            self.out_headers = list(self.req_headers)
        if not self.out_body:
            self.out_body = self.req_body

    # ---- helpers -------------------------------------------------------
    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    def req_header(self, name: str) -> str:
        name = name.lower()
        for k, v in self.req_headers:
            if k.lower() == name:
                return v
        return ""

    def resp_header(self, name: str) -> str:
        name = name.lower()
        for k, v in self.resp_headers:
            if k.lower() == name:
                return v
        return ""

    @property
    def resp_content_type(self) -> str:
        return (self.mime or self.resp_header("content-type")).lower()

    @property
    def req_content_type(self) -> str:
        return self.req_header("content-type").lower()

    def resp_headers_text(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.resp_headers)

    def short_label(self) -> str:
        return f"#{self.idx} {self.method} {self.path}"


@dataclass
class Transaction:
    name: str
    container: str                  # Init | Actions | End
    exchange_ids: List[int]
    think_time_s: int = 0           # pause after this transaction


@dataclass
class Correlation:
    var: str
    value: str
    source_idx: int                 # exchange whose response holds the value
    kind: str                       # regexp | jsonpath | header
    expression: str
    match_number: int = 1
    decode: Optional[str] = None    # html | url
    used_in: List[int] = field(default_factory=list)
    param_name: str = ""
    json_segments: Optional[list] = None   # for Postman export
    enabled: bool = True

    def extractor(self) -> dict:
        ex: dict = {"name": self.var}
        if self.kind == "jsonpath":
            ex["jsonpath"] = self.expression
        else:
            ex["from"] = "header" if self.kind == "header" else "body"
            ex["regexp"] = self.expression
            ex["template"] = "$1$"
            if self.match_number != 1:
                ex["match_number"] = self.match_number
        if self.decode:
            ex["decode"] = self.decode
        ex["default"] = f"{self.var}_NOT_FOUND"
        return ex


@dataclass
class ParamCandidate:
    key: str                        # stable id: location|name|value
    location: str                   # query | form | json
    name: str
    value: str
    exchange_ids: List[int]
    group: str                      # credentials | testdata
    column: str
    enabled: bool = True
