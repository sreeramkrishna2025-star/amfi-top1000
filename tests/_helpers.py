"""Shared synthetic fixtures. No real AMFI data; no network."""
import io
import os
import sys
import zipfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def minimal_xlsx_bytes():
    """A tiny ZIP container that looks like an .xlsx workbook (synthetic)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()
