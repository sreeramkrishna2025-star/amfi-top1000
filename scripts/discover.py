"""Discover official AMFI categorisation file links from the AMFI page HTML.

Read-only. Parses <a href> tags; never guesses or constructs URLs.
Only HTTPS links on the official AMFI hosts are accepted (see ALLOWED_HOSTS).
Usage:
  python3 scripts/discover.py page.html            # print JSON list of discovered files
  python3 scripts/discover.py page.html --out x.json

Limitation (VERIFIED in browser snapshot): collapsed year sections may not expose
their links in the rendered DOM. Results must be reconciled against expected
half-year periods before trusting "no new period".
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

BASE = "https://www.amfiindia.com/otherdata/categorisation-of-stocks"
ALLOWED_HOSTS = frozenset({"www.amfiindia.com", "portal.amfiindia.com"})
ALLOWED_EXTENSIONS = (".xlsx", ".pdf")
# Matches e.g. AverageMarketCapitalization30Jun2026, ...ended31Dec2024, ...30June2022
PERIOD_RE = re.compile(r"(30\s*june?|31\s*dec)\s*(\d{4})", re.IGNORECASE)
# Matches e.g. "Jan-June 2021", "Jul - Dec 2017" (month-name style file titles)
MONTH_RE = re.compile(r"(jan\s*-?\s*(?:june?|jun)|jul\s*-?\s*dec)\s*(\d{4})", re.IGNORECASE)
# Matches "Average Market Capitalisation", "AverageMarketCapitalization", "Avg. Market Capitalization", underscores
FILE_RE = re.compile(r"(average|avg)[\s_.\-]*market[\s_.\-]*capitali[sz]ation", re.IGNORECASE)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []  # (href, text)
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._buf).split())))
            self._href = None


def check_official_url(url):
    """Return (ok, reason). Accepts only HTTPS links on ALLOWED_HOSTS ending in .xlsx/.pdf.

    The extension is checked on the URL PATH only, so a query string is allowed.
    """
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False, "malformed URL"
    if parts.scheme.lower() != "https":
        return False, f"scheme not https: {parts.scheme or 'none'}"
    if parts.username is not None or parts.password is not None:
        return False, "credentials in URL"
    if port not in (None, 443):
        return False, f"non-standard port: {port}"
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return False, f"host not allowed: {host or 'none'}"
    path = unquote(parts.path).lower()
    if not path.endswith(ALLOWED_EXTENSIONS):
        return False, "path does not end in .xlsx or .pdf"
    return True, "ok"


def period_from(text):
    """Return (period_end_date 'YYYY-MM-DD', period_label) or (None, None)."""
    clean = unquote(text).replace("_", " ").replace("-", " ")
    m = PERIOD_RE.search(clean)
    if m:
        h1 = m.group(1).lower().startswith("30")
        year = m.group(2)
    else:
        m = MONTH_RE.search(clean)
        if not m:
            return None, None
        h1 = m.group(1).lower().startswith("jan")
        year = m.group(2)
    if h1:
        return f"{year}-06-30", f"{year}H1"
    return f"{year}-12-31", f"{year}H2"


def discover(html, base_url=BASE, rejected=None):
    """Return official AMFI file entries found in html.

    If `rejected` is a list, links that were skipped are appended as (url, reason).
    """
    parser = _Links()
    parser.feed(html)
    found = []
    seen = set()
    for raw_href, text in parser.links:
        # Trim surrounding whitespace only; whitespace inside the path is never changed.
        href = raw_href.strip()
        if not href:
            continue
        url = urljoin(base_url, href)
        ok, reason = check_official_url(url)
        if not ok:
            if rejected is not None and (href.lower().endswith(ALLOWED_EXTENSIONS) or FILE_RE.search(text)):
                rejected.append((url, reason))
            continue
        if not FILE_RE.search(unquote(url)) and not FILE_RE.search(text):
            # Skip unrelated PDFs (e.g. the SEBI circular)
            continue
        if url in seen:
            continue
        seen.add(url)
        is_excel = urlsplit(url).path.lower().endswith(".xlsx")
        end, label = period_from(url)
        if end is None:
            end, label = period_from(text)
        found.append({
            "url": url,
            "raw_href": raw_href,
            "kind": "excel" if is_excel else "pdf",
            "period_end_date": end,
            "period_label": label,
            "link_text": text,
        })
    # Excel first within each period (primary source)
    found.sort(key=lambda f: (f["period_end_date"] or "", f["kind"] == "excel"), reverse=True)
    return found


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("html_file")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    with open(args.html_file, encoding="utf-8", errors="replace") as fh:
        rejected = []
        files = discover(fh.read(), rejected=rejected)
    payload = {"files": files, "rejected": [{"url": u, "reason": r} for u, r in rejected]}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if files else 1


if __name__ == "__main__":
    sys.exit(main())
