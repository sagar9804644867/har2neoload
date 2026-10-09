"""Generate a NeoLoad Desktop project (.nlp + config.zip) that opens directly in the NeoLoad GUI.

The XML layout and attribute codes were taken from projects saved by NeoLoad 2026.2.1
(project.version 8.11). Static parts (DTD, monitors, zones, settings) come from
``nl_template/``; the user path, servers, variables, population and scenarios are generated.

Verified codes (from NeoLoad-saved projects):
  variable-extractor extractType: 0 = body regex, 1 = header regex, 4 = JSONPath
  variable-file policy: 1 = each iteration, 4 = each virtual user instance
  variable-file range:  2 = global, 4 = unique ; order 1 = sequential
  rampup-volume-policy delayTypeIncrement 1 = seconds ; duration type 2 = by time
"""
from __future__ import annotations

import io
import pathlib
import re
import uuid
import zipfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from xml.sax.saxutils import escape as _esc

from .models import Correlation, Exchange, Transaction
from .parsers import page_title_from_html

TPL = pathlib.Path(__file__).parent / "nl_template"
_ENC = re.compile(r"__encodeURL\((\$\{[^}]+\})\)")
DROP_HEADERS = {"cookie", "host", "content-length", "connection", "proxy-connection", "keep-alive",
                "if-none-match", "if-modified-since", "priority", "te", "upgrade-insecure-requests",
                "accept-encoding", "dnt", "accept-language"}


def _a(value) -> str:
    """XML attribute value (double-quoted)."""
    return _esc(str(value), {'"': "&quot;", "\n": "&#xa;", "\r": "&#xd;", "\t": "&#x9;"})


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _uid() -> str:
    return str(uuid.uuid4())


def _plain(text: str) -> str:
    """NeoLoad encodes parameter values itself, so drop the as-code __encodeURL() wrapper."""
    return _ENC.sub(r"\1", text or "")


def _server_key(url: str) -> Tuple[str, int, bool]:
    p = urlsplit(url)
    ssl = p.scheme == "https"
    return (p.hostname or "localhost", p.port or (443 if ssl else 80), ssl)


def _server_uid(key: Tuple[str, int, bool]) -> str:
    host, port, ssl = key
    default = 443 if ssl else 80
    return host if port == default else f"{host}:{port}"


# ---------------------------------------------------------------- elements
def _extractor_xml(c: Correlation, referer: str) -> str:
    common = (f'assertionOnNoMatch="true" defaultValue="{_a(c.var + "_NOT_FOUND")}" '
              f'endsWithSimple="" extractFromVarNameAdv="" extractFromVarNameSimple="" '
              f'matchNumber="{c.match_number}" matchNumberAdv="{c.match_number}" matchNumberSimple="{c.match_number}" '
              f'name="{_a(c.var)}" refererUid="{referer}" setDefaultValue="true" startsWithSimple="" '
              f'template="$1$" templateAdv="$1$" uid="{_uid()}" xpath=""')
    html = ' valueHtmlEncoded="true"' if c.decode == "html" else ""
    if c.kind == "jsonpath":
        return (f'    <variable-extractor {common} displayMode="0" extractType="4" extractTypeAdv="0" '
                f'extractTypeSimple="4" jsonpath="{_a(c.expression)}" regExp="(.*)" regExpAdv=""{html}>\n'
                f'        <variable-extractor-group extract="true" occurs="1" pattern="" type="4"/>\n'
                f'    </variable-extractor>\n')
    etype = "1" if c.kind == "header" else "0"
    rx = _a(c.expression)
    return (f'    <variable-extractor {common} displayMode="1" extractType="{etype}" extractTypeAdv="{etype}" '
            f'extractTypeSimple="0" regExp="{rx}" regExpAdv="{rx}"{html}>\n'
            f'        <variable-extractor-group extract="true" occurs="1" pattern="" type="4"/>\n'
            f'    </variable-extractor>\n')


def _param_xml(tag: str, name: str, value: str) -> str:
    v = _a(_plain(value))
    return (f'    <{tag} encodeName="true" encodeValue="true" name="{_a(name)}" recordRawValue="{v}" '
            f'separator="=" value="{v}" valueMode="USE_VALUE"/>\n')


def _query_pairs(query: str) -> List[Tuple[str, str]]:
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((unquote_plus(k), _plain(v) if "${" in v else unquote_plus(v)))
    return pairs


def _action_xml(e: Exchange, uid: str, server_uid: str, extractors: List[Correlation], assertion: bool) -> str:
    out = urlsplit(e.out_url)
    path = out.path or "/"
    has_body = bool(e.out_body) and e.method in ("POST", "PUT", "PATCH", "DELETE")
    method = e.method if e.method in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE") else "POST"
    custom = f' customMethod="{_a(e.method)}"' if method != e.method else ""
    attrs = (f'actionType="1" extractorInjectPathPolicy="2" extractorPathPolicy="1" followRedirects="false" '
             f'generatedByClient="false" linkExtractorSeveralMatchOccurrence="1" linkExtractorSeveralMatchPolicy="1" '
             f'linkExtractorType="6" method="{method}"{custom} name="{_a(path)}" path="{_a(path)}" '
             f'postType="{4 if has_body else 1}" serverUid="{_a(server_uid)}" slaProfileEnabled="false" '
             f'uid="{uid}" useKeepAlive="false"')
    xml = [f"<http-action {attrs}>\n"]
    for c in extractors:
        xml.append(_extractor_xml(c, uid))
    if assertion:
        title = page_title_from_html(e.resp_body)
        if title and "${" not in title and len(title) <= 80 and 200 <= e.status < 300:
            xml.append(f'    <assertions>\n        <assertion-content name="assertion_1" notType="false" '
                       f'pattern="{_a(title)}"/>\n    </assertions>\n')
    tag = "urlPostParameter" if has_body else "parameter"
    for k, v in _query_pairs(out.query):
        xml.append(_param_xml(tag, k, v))
    for k, v in e.out_headers:
        lk = k.lower()
        if lk in DROP_HEADERS or lk.startswith(("sec-", ":")):
            continue
        xml.append(f'    <header name="{_a(k)}" value="{_a(v)}"/>\n')
    xml.append("    <responseHeaders/>\n")
    xml.append('    <record-html-infos extractorRegExp="false" htmlType="0"/>\n')
    if has_body:
        xml.append(f"    <textPostContent>{_cdata(_plain(e.out_body))}</textPostContent>\n")
    xml.append("</http-action>\n")
    return "".join(xml)


def _container_xml(name: str, uid: str, children: List[str]) -> str:
    kids = "".join(f'    <weighted-embedded-action uid="{c}"/>\n' for c in children)
    return (f'<basic-logical-action-container element-number="1" execution-type="0" name="{_a(name)}" '
            f'pacingEnd="0" pacingMode="MODE_NO_PACING" pacingStart="0" pacingValue="0" '
            f'slaProfileEnabled="false" uid="{uid}" weightsEnabled="false">\n{kids}</basic-logical-action-container>\n')


def _delay_xml(uid: str, seconds: int) -> str:
    return (f'<delay-action duration="{int(seconds) * 1000}" isThinkTime="true" name="think time" '
            f'timeMode="MODE_SIMPLE_THINK_TIME" timeRangeEnd="" timeRangeStart="" uid="{uid}"/>\n')


def _vu_container(tag: str, desc: str, children: List[str], extra: str = "") -> str:
    kids = "".join(f'        <weighted-embedded-action uid="{c}"/>\n' for c in children)
    return (f'    <{tag} {extra}element-number="1" execution-type="0" pacingEnd="0" pacingMode="MODE_NO_PACING" '
            f'pacingStart="0" pacingValue="0" slaProfileEnabled="false" weightsEnabled="false">\n'
            f'        <description>{desc}</description>\n{kids}    </{tag}>\n')


def _population_xml(name: str, vu: str) -> str:
    return f"""<population uid="{_a(name)}">
    <split cache="1" factor="100.0" virtualUserUid="{_a(vu)}">
        <browser-profile acceptEncoding="GZIP,DEFLATE,BROTLI" connections="6" cookies="true" http2="true" name="browser.recorded"/>
        <wan-emulation-profile averageDownloadBandwidth="bandwidth.unlimited" averageDownloadLatency="0ms" averageDownloadPacketLoss="0%" averageUploadBandwidth="bandwidth.unlimited" averageUploadLatency="0ms" averageUploadPacketLoss="0%" downloadBandwidth="bandwidth.unlimited" downloadLatency="0ms" downloadPacketLoss="0%" isRadio="false" name="bandwidth.unlimited" poorDownloadBandwidth="bandwidth.unlimited" poorDownloadLatency="0ms" poorDownloadPacketLoss="0%" poorUploadBandwidth="bandwidth.unlimited" poorUploadLatency="0ms" poorUploadPacketLoss="0%" signalStrength="GOOD" uploadBandwidth="bandwidth.unlimited" uploadLatency="0ms" uploadPacketLoss="0%"/>
    </split>
</population>
"""


def _file_var_xml(name: str, columns: List[str], unique: bool) -> str:
    cols = "".join(f'    <column name="{_a(c)}" number="{i}"/>\n' for i, c in enumerate(columns))
    policy, rng = ("4", "4") if unique else ("1", "2")
    return (f'<variable-file delimiters="," filename="data/{_a(name)}.csv" name="{_a(name)}" offset="1" order="1" '
            f'policy="{policy}" range="{rng}" uid="{_uid()}" useFirstLine="true" whenOutOfValues="CYCLE_VALUES">\n'
            f'{cols}</variable-file>\n')


def _scenario_xml(name: str, population: str, policy_xml: str, duration_s: int, body: str) -> str:
    return (f'<scenario postMonitoringTime="-1" preMonitoringTime="-1" slaProfileEnabled="false" '
            f'traceVariables="true" uid="{_a(name)}" virtualUsersStates="true">\n'
            f'<!--****** POPULATION POLICY ******-->\n<population-policy name="{_a(population)}">\n'
            f'<duration-policy-entry iterations="1" time="{int(duration_s)}" timeUnit="0" type="2"/>\n'
            f'<volume-policy-entry>\n{policy_xml}\n</volume-policy-entry>\n'
            f'<start-stop-policy-entry start-delay="0" start-type="0" stop-delay="60000" stop-type="0"/>\n'
            f'<runtime-policy continueOnError="true" thinktimePolicy="0" thinktimeValue="5000" vuStartDelay="0" vuStartMode="0"/>\n'
            f'<!--****** POPULATION LG HOSTS ******-->\n<lg-hosts>\n'
            f'<lg-host-entry>$zoneID=Default zone;$lgID=localhost:7100</lg-host-entry>\n</lg-hosts>\n'
            f'</population-policy>\n{body}</scenario>\n')


# ---------------------------------------------------------------- main
def build_nlp_project(
    project: str,
    user_path: str,
    exchanges: List[Exchange],
    transactions: List[Transaction],
    correlations: List[Correlation],
    files: Dict[str, List[Dict[str, str]]],
    users: int,
    duration_min: int,
    rampup_min: int,
    add_assertions: bool = True,
    think_times: bool = True,
) -> Tuple[Dict[str, bytes], List[str]]:
    """Return ({relative path: bytes}, notes) for the project folder."""
    notes: List[str] = []
    by_id = {e.idx: e for e in exchanges}
    extr: Dict[int, List[Correlation]] = {}
    for c in correlations:
        extr.setdefault(c.source_idx, []).append(c)

    servers: Dict[Tuple[str, int, bool], str] = {}
    actions_xml, containers_xml = [], []
    placement: Dict[str, List[str]] = {"Init": [], "Actions": [], "End": []}
    txs = list(transactions)
    if txs and not any(t.container == "Actions" for t in txs):
        for t in txs:
            t.container = "Actions"

    for t in txs:
        child_uids = []
        for idx in t.exchange_ids:
            e = by_id.get(idx)
            if e is None:
                continue
            key = _server_key(e.url)
            servers.setdefault(key, _server_uid(key))
            uid = _uid()
            actions_xml.append(_action_xml(e, uid, servers[key], extr.get(idx, []), add_assertions))
            child_uids.append(uid)
        if not child_uids:
            continue
        cuid = _uid()
        containers_xml.append(_container_xml(t.name, cuid, child_uids))
        placement[t.container].append(cuid)
        if think_times and t.think_time_s > 0:
            duid = _uid()
            containers_xml.append(_delay_xml(duid, min(t.think_time_s, 99)))
            placement[t.container].append(duid)

    vu = (f'<!--****** VIRTUAL USERS ******-->\n<!--****** VIRTUAL USER : \'{user_path}\' ******-->\n'
          f'<virtual-user errorPolicy="DO_NOTHING" failedAssertionPolicy="DO_NOTHING" slaProfileEnabled="false" '
          f'type="LEGACY" uid="{_a(user_path)}">\n'
          + _vu_container("init-container", "Elements executed once when the Virtual User starts.", placement["Init"])
          + _vu_container("actions-container", "Elements repeated until the Virtual User stops.", placement["Actions"],
                          'clearUserIterationDataMode="AUTO" ')
          + _vu_container("end-container", "Elements executed before the Virtual User stops.", placement["End"])
          + '    <thinktime-policy thinkTimeMode="MODE_SIMPLE_THINK_TIME" thinktimeFactorValue="100" '
            'thinktimePolicy="0" thinktimeRangeValue="0" thinktimeValue="5000"/>\n</virtual-user>\n')

    population = "Population1"
    srv_xml = "".join(
        f'<http-server hostname="{_a(h)}" port="{p}" ssl="{str(s).lower()}" uid="{_a(u)}" '
        f'urlrewriting-argument-name="" urlrewriting-enabled="false" urlrewriting-path-extension="false" '
        f'urlrewriting-path-not-equals="false" urlrewriting-path-separator=";"/>\n'
        for (h, p, s), u in servers.items())

    var_xml = []
    data_files: Dict[str, bytes] = {}
    from .exporters import to_csv  # local import to avoid a cycle
    for group, rows in files.items():
        if not rows or not rows[0]:
            continue
        unique = group == "credentials"
        if unique and len(rows) < users:
            unique = False
            notes.append(f"data/{group}.csv has {len(rows)} row(s) for {users} users, so it is shared (global) "
                         f"instead of unique. Add one row per user to make it unique.")
        var_xml.append(_file_var_xml(group, list(rows[0].keys()), unique))
        data_files[f"data/{group}.csv"] = to_csv(rows).encode("utf-8")

    repo = ((TPL / "repository_head.xml").read_text(encoding="utf-8") + "\n" + vu
            + "".join(containers_xml) + "".join(actions_xml)
            + f"<!--****** END VIRTUAL USER : '{user_path}' ******-->\n"
            + "<!--****** POPULATIONS ******-->\n" + _population_xml(population, user_path)
            + "<!--****** END POPULATIONS ******-->\n<!--****** SERVERS ******-->\n" + srv_xml
            + "<!--****** END SERVERS ******-->\n<!--****** VARIABLES ******-->\n" + "".join(var_xml)
            + "<!--****** END VARIABLES ******-->\n"
            + (TPL / "repository_tail.xml").read_text(encoding="utf-8"))

    body = (TPL / "scenario_body.xml").read_text(encoding="utf-8")
    users = max(1, int(users))
    rampup_s = max(1, int(rampup_min)) * 60
    every = max(1, round(rampup_s / max(1, users - 1))) if users > 1 else 30
    load_policy = (f'<rampup-volume-policy delayIncrement="{float(every)}" delayTypeIncrement="1" initialUserNumber="1" '
                   f'iterationNumber="1" maxUserNumber="{users}" userIncrement="1"/>')
    smoke_policy = '<constant-volume-policy iterationNumber="1" userNumber="1"/>'
    scen = ((TPL / "scenario_head.xml").read_text(encoding="utf-8") + "<scenarios>\n"
            + _scenario_xml("Load_Test", population, load_policy, max(1, int(duration_min)) * 60, body)
            + _scenario_xml("Smoke_1VU", population, smoke_policy, 60, body)
            + "</scenarios>\n")

    cfg = io.BytesIO()
    with zipfile.ZipFile(cfg, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("repository.xml", repo)
        z.writestr("scenario.xml", scen)
        z.writestr("settings.xml", (TPL / "settings.xml").read_text(encoding="utf-8"))

    nlp = ("# Project description file\n\n"
           f"project.name={project}\nproject.version=8.11\nproject.original.version=8.11\n"
           "product.name=NeoLoad\nproduct.original.name=NeoLoad\nssh.trustUnknownHosts.MONITORS=false\n"
           "monitor.tls.insecure=false\nproduct.version=2026.2.1\nproduct.original.version=2026.2.1\n"
           "project.is-open-test-settings-after-upload=false\n"
           f"project.id={_uid()}\nproject.config.path=config.zip\nproject.config.storage=ZIP\n"
           "team.server.enabled=false\n")

    out = {f"{project}.nlp": nlp.encode("utf-8"), "config.zip": cfg.getvalue()}
    out.update(data_files)
    for d in ("recorded-requests", "recorded-responses", "recorded-screenshots", "results", "scripts",
              "custom-resources", "lib/extlib", "lib/jslib", "lib/plugins"):
        out[d + "/"] = b""
    return out, notes
