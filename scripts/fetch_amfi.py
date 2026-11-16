"""Validated, bounded downloader for official AMFI Excel files.

Design rules (from the approved Gate B plan):
- HTTPS only, official hosts only, port 443 or none, no userinfo, .xlsx path only.
- Every redirect target is validated before it is followed (max 3 redirects).
- Retries only for transient failures: HTTP 5xx, timeouts, connection resets.
  Never retried: 403 (policy denial), other 4xx, redirect refusal, size limit, bad content.
- Size limit enforced while streaming. SHA-256 computed over the exact bytes received.
- A response is accepted only if the Content-Type is a spreadsheet type (or the generic
  binary type), the body is not HTML, and the body is a valid ZIP-based workbook.
- Writes only inside a caller-supplied, empty destination directory.

This module performs NO network I/O at import time. The CLI refuses to run unless
--i-understand-permission-hold is given, because downloads are on hold (see README).

Usage (blocked for now; kept for the approved backfill stage):
  python3 scripts/fetch_amfi.py URL DEST_DIR --i-understand-permission-hold
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({"www.amfiindia.com", "portal.amfiindia.com"})
ALLOWED_PORTS = (None, 443)
MAX_REDIRECTS = 3
MAX_BYTES = 25 * 1024 * 1024          # 25 MiB hard cap; AMFI Excel files are ~50-100 KB
CHUNK = 64 * 1024
TIMEOUT_SECONDS = 30                   # per socket operation
MAX_SECONDS = 120                      # total wall-clock budget for one attempt
RETRY_BACKOFF = (2, 4)                 # 3 attempts total: 1 + 2 retries
ACCEPTED_CONTENT_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "application/vnd.ms-excel",
    "application/excel",
})
TRANSIENT_HTTP = range(500, 600)


class FetchError(Exception):
    """Failure of one download attempt. `transient` marks retry-eligible errors."""

    def __init__(self, message, transient=False, http_status=None):
        super().__init__(message)
        self.transient = transient
        self.http_status = http_status


def check_url(url):
    """Return (ok, reason). Pure function; no network."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "unparseable URL"
    if parts.scheme != "https":
        return False, "scheme must be https"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False, "userinfo not allowed"
    if parts.hostname not in ALLOWED_HOSTS:
        return False, f"host not allowed: {parts.hostname}"
    try:
        port = parts.port
    except ValueError:
        return False, "invalid port"
    if port not in ALLOWED_PORTS:
        return False, f"port not allowed: {port}"
    if not parts.path.lower().endswith(".xlsx"):
        return False, "path must end in .xlsx"
    return True, "ok"


class ValidatedRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect whose target fails check_url(); cap the chain length."""

    max_redirections = MAX_REDIRECTS
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = check_url(newurl)
        if not ok:
            raise urllib.error.URLError(f"redirect refused ({reason})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener():
    return urllib.request.build_opener(ValidatedRedirects)


def is_html(head):
    """True if the first bytes look like an HTML/XML error page."""
    s = head.lstrip()[:512].lower()
    return s.startswith(b"<!doctype") or s.startswith(b"<html") or s.startswith(b"<?xml") or b"<html" in s


def is_valid_xlsx(path):
    """ZIP container with a workbook part, and no corrupt members."""
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return False, "corrupt zip member"
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as e:
        return False, f"not a zip container: {type(e).__name__}"
    if "xl/workbook.xml" not in names:
        return False, "missing xl/workbook.xml"
    return True, "ok"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _content_type(resp):
    raw = resp.headers.get("Content-Type", "") if resp.headers is not None else ""
    return raw.split(";", 1)[0].strip().lower()


def _status(resp):
    code = getattr(resp, "status", None)
    if code is None and hasattr(resp, "getcode"):
        code = resp.getcode()
    return code


def _download_once(opener, url, dest_dir, max_bytes, timeout, max_seconds=MAX_SECONDS, clock=time.monotonic):
    """One attempt. Returns a result dict; raises FetchError(transient=bool) on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "amfi-top1000-validator/0.1"})
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        code = e.code
        if code in TRANSIENT_HTTP:
            raise FetchError(f"HTTP {code}", transient=True, http_status=code)
        raise FetchError(f"HTTP {code}", transient=False, http_status=code)
    except urllib.error.URLError as e:
        # covers redirect refusal and DNS/connection problems
        msg = str(e.reason)
        transient = "redirect refused" not in msg and isinstance(e.reason, (socket.timeout, ConnectionError, OSError))
        raise FetchError(f"URL error: {msg}", transient=transient)
    except (socket.timeout, TimeoutError) as e:
        raise FetchError(f"timeout: {e}", transient=True)
    except ConnectionError as e:
        raise FetchError(f"connection error: {e}", transient=True)

    status = _status(resp)
    final_url = resp.geturl() if hasattr(resp, "geturl") else url
    ok, reason = check_url(final_url)
    if not ok:
        raise FetchError(f"final URL rejected: {reason}", transient=False, http_status=status)
    if status != 200:
        raise FetchError(f"unexpected HTTP status {status}", transient=status in TRANSIENT_HTTP, http_status=status)
    ctype = _content_type(resp)
    if ctype not in ACCEPTED_CONTENT_TYPES:
        raise FetchError(f"content type rejected: {ctype or 'missing'}", transient=False, http_status=status)

    fd, tmp_path = tempfile.mkstemp(prefix="amfi_", suffix=".xlsx.part", dir=dest_dir)
    h = hashlib.sha256()
    size = 0
    head = b""
    started = clock()
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                if clock() - started > max_seconds:
                    raise FetchError(f"download time limit exceeded (> {max_seconds}s)", transient=False, http_status=status)
                block = resp.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    raise FetchError(f"size limit exceeded (> {max_bytes} bytes)", transient=False, http_status=status)
                if len(head) < 512:
                    head += block[:512]
                h.update(block)
                out.write(block)
    except BaseException:
        _safe_remove(tmp_path)
        raise
    if size == 0:
        _safe_remove(tmp_path)
        raise FetchError("empty response body", transient=True, http_status=status)
    if is_html(head):
        _safe_remove(tmp_path)
        raise FetchError("HTML body received instead of a workbook", transient=False, http_status=status)
    valid, why = is_valid_xlsx(tmp_path)
    if not valid:
        _safe_remove(tmp_path)
        raise FetchError(f"invalid workbook: {why}", transient=False, http_status=status)
    return {
        "url": url,
        "final_url": final_url,
        "http_status": status,
        "content_type": ctype,
        "size_bytes": size,
        "sha256": h.hexdigest(),
        "path": tmp_path,
        "status": "DOWNLOAD_VERIFIED",
    }


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def fetch(url, dest_dir, opener=None, max_bytes=MAX_BYTES, timeout=TIMEOUT_SECONDS,
          attempts=1 + len(RETRY_BACKOFF), sleep=time.sleep, max_seconds=MAX_SECONDS, clock=time.monotonic):
    """Download one AMFI workbook into dest_dir. Never raises; returns a status dict.

    status is DOWNLOAD_VERIFIED on success, otherwise DOWNLOAD_FAILED (with 'error').
    dest_dir must already exist and be empty (caller's responsibility; checked here).
    """
    ok, reason = check_url(url)
    if not ok:
        return {"url": url, "status": "DOWNLOAD_FAILED", "error": f"URL rejected: {reason}", "attempts": 0}
    if not os.path.isdir(dest_dir) or os.listdir(dest_dir):
        return {"url": url, "status": "DOWNLOAD_FAILED", "error": "dest_dir must be an existing empty directory", "attempts": 0}
    opener = opener or build_opener()
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            result = _download_once(opener, url, dest_dir, max_bytes, timeout, max_seconds, clock)
            result["attempts"] = attempt
            return result
        except FetchError as e:
            last_error = e
            if not e.transient or attempt == attempts:
                break
            sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
    return {
        "url": url,
        "status": "DOWNLOAD_FAILED",
        "error": str(last_error),
        "http_status": getattr(last_error, "http_status", None),
        "attempts": attempt,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validated AMFI Excel downloader (on hold; see README).")
    ap.add_argument("url")
    ap.add_argument("dest_dir")
    ap.add_argument("--i-understand-permission-hold", action="store_true",
                    help="required acknowledgement: AMFI downloads are on permission hold")
    args = ap.parse_args(argv)
    if not args.i_understand_permission_hold:
        print(json.dumps({"status": "REFUSED", "error": "AMFI downloads are on permission hold; "
                          "pass --i-understand-permission-hold only after written clearance"}, indent=1))
        return 3
    res = fetch(args.url, args.dest_dir)
    print(json.dumps({k: v for k, v in res.items() if k != "path"}, indent=1))
    return 0 if res["status"] == "DOWNLOAD_VERIFIED" else 2


if __name__ == "__main__":
    sys.exit(main())
