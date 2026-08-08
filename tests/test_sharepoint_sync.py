"""Tests for src/sharepoint_sync.py — the SharePoint → inputs/ mirror.

Graph is faked with httpx.MockTransport; nothing here touches the network.
What matters and is covered: recursion builds the right relative paths, a
mid-sync failure leaves the previous inputs/ fully intact (nothing half-written,
nothing deleted), and a file gone from SharePoint disappears locally.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sharepoint_sync  # noqa: E402

ENV = {
    "SHAREPOINT_TENANT_ID": "tenant",
    "SHAREPOINT_CLIENT_ID": "client",
    "SHAREPOINT_CLIENT_SECRET": "secret",
    "SHAREPOINT_SITE": "contoso.sharepoint.com:/sites/SignalAgent",
    "SHAREPOINT_INPUTS_PATH": "Signal Agent/inputs",
}

# Folder tree the fake Graph serves, keyed by drive-relative folder path.
TREE = {
    "Signal Agent/inputs": [
        {"name": "tuning.xlsx", "@microsoft.graph.downloadUrl": "https://dl/tuning"},
        {"name": "content", "folder": {}},
    ],
    "Signal Agent/inputs/content": [
        {"name": "linkedin", "folder": {}},
    ],
    "Signal Agent/inputs/content/linkedin": [
        {"name": "post.md", "@microsoft.graph.downloadUrl": "https://dl/post"},
    ],
}


def _handler(fail_download: str | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login.microsoftonline.com" in url:
            return httpx.Response(200, json={"access_token": "tok"})
        if url.endswith("/sites/contoso.sharepoint.com:/sites/SignalAgent"):
            return httpx.Response(200, json={"id": "site-1"})
        if "/children" in url:
            path = ""
            if "root:/" in url:
                path = url.split("root:/", 1)[1].split(":", 1)[0].replace("%20", " ")
            return httpx.Response(200, json={"value": TREE.get(path, [])})
        if url.startswith("https://dl/"):
            if fail_download and url.endswith(fail_download):
                return httpx.Response(500)
            return httpx.Response(200, content=f"body-of-{url}".encode())
        raise AssertionError(f"unexpected request: {url}")

    return handle


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.env = mock.patch.dict("os.environ", ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.inputs = mock.patch.object(sharepoint_sync, "INPUTS_DIR", self.tmp)
        self.inputs.start()
        self.addCleanup(self.inputs.stop)

    def _run(self, fail_download: str | None = None) -> int:
        transport = httpx.MockTransport(_handler(fail_download))
        real_client = httpx.Client

        def client(**kw):
            kw.pop("transport", None)
            return real_client(transport=transport, **kw)

        with mock.patch.object(httpx, "Client", client):
            return sharepoint_sync.sync()

    def test_mirrors_nested_tree(self) -> None:
        n = self._run()
        self.assertEqual(n, 2)
        self.assertEqual((self.tmp / "tuning.xlsx").read_bytes(), b"body-of-https://dl/tuning")
        self.assertEqual(
            (self.tmp / "content/linkedin/post.md").read_bytes(), b"body-of-https://dl/post"
        )

    def test_failed_download_preserves_previous_inputs(self) -> None:
        # A run that half-fails must not leave a torn workbook or delete the
        # last-known-good files — the 08:00 digest still has to run.
        (self.tmp / "tuning.xlsx").write_bytes(b"old-tuning")
        (self.tmp / "stale.xlsx").write_bytes(b"local-only")

        with self.assertRaises(httpx.HTTPStatusError):
            self._run(fail_download="/tuning")

        self.assertEqual((self.tmp / "tuning.xlsx").read_bytes(), b"old-tuning")
        self.assertTrue((self.tmp / "stale.xlsx").exists())

    def test_removes_files_gone_from_sharepoint(self) -> None:
        (self.tmp / "deleted-upstream.xlsx").write_bytes(b"x")
        self._run()
        self.assertFalse((self.tmp / "deleted-upstream.xlsx").exists())
        self.assertTrue((self.tmp / "tuning.xlsx").exists())

    def test_not_configured_is_a_noop(self) -> None:
        with mock.patch.dict("os.environ", {"SHAREPOINT_SITE": ""}):
            self.assertFalse(sharepoint_sync.is_configured())
            self.assertEqual(sharepoint_sync.main([]), 0)


if __name__ == "__main__":
    unittest.main()
