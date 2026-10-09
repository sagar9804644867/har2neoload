"""Validate generated NeoLoad XML against the internal DTD, ignoring the errors that
NeoLoad's own saved reference files also produce."""
import re
from lxml import etree

def errors(data: bytes):
    parser = etree.XMLParser(dtd_validation=True, load_dtd=True, recover=True)
    etree.fromstring(data, parser)
    out = set()
    for e in parser.error_log:
        if e.level_name == "WARNING":
            continue
        msg = re.sub(r"line \d+.*", "", e.message)
        out.add(msg)
    return out
