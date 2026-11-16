import hashlib
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from unittest import mock

import _helpers  # noqa: F401  (sets sys.path)
import fetch_amfi as fa

GOOD_URL = "https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FakeResp:
    def __init__(self, body=b"", status=200, ctype=XLSX_CT, final_url=GOOD_URL):
        self._buf = BytesIO(body)
        self.status = status
        self.headers = {"Content-Type": ctype} if ctype is not None else {}
        self._final = final_url

    def read(self, n=-1):
        return self._buf.read(n)

    def geturl(self):
        return self._final


class FakeOpener:
    """Returns queued responses; an Exception instance in the queue is raised."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def http_error(code):
    return urllib.error.HTTPError(GOOD_URL, code, "x", {}, None)


class UrlValidationTests(unittest.TestCase):
    def test_official_hosts_accepted(self):
        self.assertTrue(fa.check_url(GOOD_URL)[0])
        self.assertTrue(fa.check_url("https://www.amfiindia.com/a/b.xlsx")[0])

    def test_https_enforced(self):
        ok, why = fa.check_url(GOOD_URL.replace("https://", "http://"))
        self.assertFalse(ok)
        self.assertIn("https", why)

    def test_disallowed_host_rejected(self):
        ok, why = fa.check_url("https://example.com/x.xlsx")
        self.assertFalse(ok)
        self.assertIn("host", why)

    def test_lookalike_host_rejected(self):
        self.assertFalse(fa.check_url("https://www.amfiindia.com.evil.example/x.xlsx")[0])
        self.assertFalse(fa.check_url("https://amfiindia.com.attacker.net/x.xlsx")[0])

    def test_userinfo_rejected(self):
        self.assertFalse(fa.check_url("https://user:pw@portal.amfiindia.com/x.xlsx")[0])

    def test_port_other_than_443_rejected(self):
        self.assertFalse(fa.check_url("https://portal.amfiindia.com:8443/x.xlsx")[0])

    def test_port_443_explicit_accepted(self):
        self.assertTrue(fa.check_url("https://portal.amfiindia.com:443/x.xlsx")[0])

    def test_non_xlsx_path_rejected(self):
        self.assertFalse(fa.check_url("https://portal.amfiindia.com/x.pdf")[0])
        self.assertFalse(fa.check_url("https://portal.amfiindia.com/x.xlsx.html")[0])

    def test_unparseable_url_rejected(self):
        self.assertFalse(fa.check_url("https://[::1")[0])


class RedirectTests(unittest.TestCase):
    def _req(self):
        return urllib.request.Request(GOOD_URL)

    def test_allowed_redirect_target_followed(self):
        h = fa.ValidatedRedirects()
        new = h.redirect_request(self._req(), None, 302, "Found", {}, GOOD_URL.replace("2026", "2026x"))
        self.assertIsNotNone(new)

    def test_external_redirect_refused(self):
        h = fa.ValidatedRedirects()
        with self.assertRaises(urllib.error.URLError) as cm:
            h.redirect_request(self._req(), None, 302, "Found", {}, "https://evil.example/x.xlsx")
        self.assertIn("redirect refused", str(cm.exception))

    def test_http_downgrade_refused(self):
        h = fa.ValidatedRedirects()
        with self.assertRaises(urllib.error.URLError):
            h.redirect_request(self._req(), None, 302, "Found", {}, GOOD_URL.replace("https", "http"))

    def test_redirect_limit_is_three(self):
        self.assertEqual(fa.ValidatedRedirects.max_redirections, 3)


class FetchSuccessTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="amfi_test_dest_")

    def tearDown(self):
        for n in os.listdir(self.dest):
            os.remove(os.path.join(self.dest, n))
        os.rmdir(self.dest)

    def test_success_records_checksum_size_and_status(self):
        from _helpers import minimal_xlsx_bytes
        body = minimal_xlsx_bytes()
        res = fa.fetch(GOOD_URL, self.dest, opener=FakeOpener(FakeResp(body)), sleep=lambda s: None)
        self.assertEqual(res["status"], "DOWNLOAD_VERIFIED")
        self.assertEqual(res["size_bytes"], len(body))
        self.assertEqual(res["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(res["content_type"], XLSX_CT)
        self.assertEqual(res["http_status"], 200)
        self.assertEqual(res["attempts"], 1)
        self.assertTrue(os.path.exists(res["path"]))
        self.assertTrue(os.path.dirname(res["path"]) == self.dest)

    def test_octet_stream_content_type_accepted(self):
        from _helpers import minimal_xlsx_bytes
        res = fa.fetch(GOOD_URL, self.dest, opener=FakeOpener(FakeResp(minimal_xlsx_bytes(), ctype="application/octet-stream")))
        self.assertEqual(res["status"], "DOWNLOAD_VERIFIED")

    def test_content_type_with_charset_parameter_accepted(self):
        from _helpers import minimal_xlsx_bytes
        res = fa.fetch(GOOD_URL, self.dest, opener=FakeOpener(FakeResp(minimal_xlsx_bytes(), ctype=XLSX_CT + "; charset=binary")))
        self.assertEqual(res["status"], "DOWNLOAD_VERIFIED")

    def test_sha256_file_matches_hashlib(self):
        p = os.path.join(self.dest, "f.bin")
        with open(p, "wb") as fh:
            fh.write(b"abc" * 100000)
        self.assertEqual(fa.sha256_file(p), hashlib.sha256(b"abc" * 100000).hexdigest())


class FetchFailureTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="amfi_test_dest_")
        self.sleeps = []

    def tearDown(self):
        for n in os.listdir(self.dest):
            os.remove(os.path.join(self.dest, n))
        os.rmdir(self.dest)

    def run_fetch(self, opener, **kw):
        return fa.fetch(GOOD_URL, self.dest, opener=opener, sleep=self.sleeps.append, **kw)

    def assertNoFilesLeft(self):
        self.assertEqual(os.listdir(self.dest), [])

    def test_html_error_page_disguised_as_excel_rejected(self):
        res = self.run_fetch(FakeOpener(FakeResp(b"<!DOCTYPE html><html>denied</html>")))
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertIn("HTML", res["error"])
        self.assertNoFilesLeft()

    def test_html_detected_even_with_leading_whitespace(self):
        self.assertTrue(fa.is_html(b"   \n<html><body>x</body></html>"))
        self.assertFalse(fa.is_html(b"PK\x03\x04binary"))

    def test_text_html_content_type_rejected_without_retry(self):
        op = FakeOpener(FakeResp(b"<html/>", ctype="text/html"))
        res = self.run_fetch(op)
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertIn("content type rejected", res["error"])
        self.assertEqual(op.calls, 1)

    def test_missing_content_type_rejected(self):
        res = self.run_fetch(FakeOpener(FakeResp(b"x", ctype=None)))
        self.assertIn("missing", res["error"])

    def test_corrupt_workbook_rejected(self):
        res = self.run_fetch(FakeOpener(FakeResp(b"not a zip at all")))
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertIn("invalid workbook", res["error"])
        self.assertNoFilesLeft()

    def test_zip_without_workbook_part_rejected(self):
        import zipfile
        from io import BytesIO
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "hi")
        res = self.run_fetch(FakeOpener(FakeResp(buf.getvalue())))
        self.assertIn("xl/workbook.xml", res["error"])

    def test_403_policy_denial_never_retried(self):
        op = FakeOpener(http_error(403), http_error(403), http_error(403))
        res = self.run_fetch(op)
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertEqual(res["http_status"], 403)
        self.assertEqual(op.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_other_4xx_never_retried(self):
        op = FakeOpener(http_error(404), http_error(404))
        res = self.run_fetch(op)
        self.assertEqual(op.calls, 1)
        self.assertEqual(res["http_status"], 404)

    def test_5xx_retried_with_backoff_then_fails(self):
        op = FakeOpener(http_error(503), http_error(503), http_error(503))
        res = self.run_fetch(op)
        self.assertEqual(op.calls, 3)
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(res["attempts"], 3)
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")

    def test_5xx_then_success_recovers(self):
        from _helpers import minimal_xlsx_bytes
        op = FakeOpener(http_error(500), FakeResp(minimal_xlsx_bytes()))
        res = self.run_fetch(op)
        self.assertEqual(res["status"], "DOWNLOAD_VERIFIED")
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(self.sleeps, [2])

    def test_timeout_is_transient_and_retried(self):
        op = FakeOpener(TimeoutError("slow"), TimeoutError("slow"), TimeoutError("slow"))
        res = self.run_fetch(op)
        self.assertEqual(op.calls, 3)
        self.assertIn("timeout", res["error"])

    def test_connection_error_retried(self):
        op = FakeOpener(ConnectionResetError("reset"), ConnectionResetError("reset"), ConnectionResetError("reset"))
        self.run_fetch(op)
        self.assertEqual(op.calls, 3)

    def test_redirect_refusal_not_retried(self):
        op = FakeOpener(urllib.error.URLError("redirect refused (host not allowed: evil.example)"))
        res = self.run_fetch(op)
        self.assertEqual(op.calls, 1)
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")

    def test_final_url_outside_allowlist_rejected(self):
        from _helpers import minimal_xlsx_bytes
        op = FakeOpener(FakeResp(minimal_xlsx_bytes(), final_url="https://evil.example/x.xlsx"))
        res = self.run_fetch(op)
        self.assertIn("final URL rejected", res["error"])
        self.assertEqual(op.calls, 1)

    def test_size_limit_enforced_and_partial_file_removed(self):
        from _helpers import minimal_xlsx_bytes
        body = minimal_xlsx_bytes() + b"x" * 5000
        res = self.run_fetch(FakeOpener(FakeResp(body)), max_bytes=1000)
        self.assertIn("size limit", res["error"])
        self.assertNoFilesLeft()

    def test_empty_body_fails(self):
        op = FakeOpener(FakeResp(b""), FakeResp(b""), FakeResp(b""))
        res = self.run_fetch(op)
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertIn("empty", res["error"])

    def test_disallowed_initial_url_never_opened(self):
        op = FakeOpener()
        res = fa.fetch("https://example.com/x.xlsx", self.dest, opener=op, sleep=self.sleeps.append)
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertEqual(op.calls, 0)

    def test_non_empty_dest_refused(self):
        with open(os.path.join(self.dest, "stale"), "w") as fh:
            fh.write("x")
        op = FakeOpener()
        res = self.run_fetch(op)
        self.assertIn("empty directory", res["error"])
        self.assertEqual(op.calls, 0)
        os.remove(os.path.join(self.dest, "stale"))

    def test_missing_dest_refused(self):
        res = fa.fetch(GOOD_URL, os.path.join(self.dest, "nope"), opener=FakeOpener())
        self.assertIn("empty directory", res["error"])


class TimeLimitTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="amfi_test_dest_")

    def tearDown(self):
        for n in os.listdir(self.dest):
            os.remove(os.path.join(self.dest, n))
        os.rmdir(self.dest)

    def test_slow_drip_download_stopped_by_total_time_limit(self):
        class TickClock:
            def __init__(self):
                self.t = 0.0

            def __call__(self):
                self.t += 50.0  # each check advances 50s
                return self.t

        from _helpers import minimal_xlsx_bytes
        body = minimal_xlsx_bytes() * 1000  # several read blocks
        res = fa.fetch(GOOD_URL, self.dest, opener=FakeOpener(FakeResp(body)),
                       sleep=lambda s: None, max_seconds=120, clock=TickClock())
        self.assertEqual(res["status"], "DOWNLOAD_FAILED")
        self.assertIn("time limit", res["error"])
        self.assertEqual(os.listdir(self.dest), [])

    def test_normal_download_within_limit_succeeds(self):
        from _helpers import minimal_xlsx_bytes
        res = fa.fetch(GOOD_URL, self.dest, opener=FakeOpener(FakeResp(minimal_xlsx_bytes())),
                       sleep=lambda s: None, max_seconds=120, clock=lambda: 0.0)
        self.assertEqual(res["status"], "DOWNLOAD_VERIFIED")


class CliTests(unittest.TestCase):
    def test_cli_refuses_without_permission_hold_acknowledgement(self):
        with mock.patch.object(fa, "fetch") as f, mock.patch("builtins.print"):
            rc = fa.main([GOOD_URL, tempfile.gettempdir()])
        self.assertEqual(rc, 3)
        f.assert_not_called()


if __name__ == "__main__":
    unittest.main()
