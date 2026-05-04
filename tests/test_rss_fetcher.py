"""Smoke tests for src/rss_fetcher.py — all offline via httpx.MockTransport."""
from __future__ import annotations

import calendar
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import rss_fetcher as rf  # noqa: E402
from models import Signal  # noqa: E402
from query_planner import Newsletter  # noqa: E402


# --- Sample feed bodies -------------------------------------------------

def _rss_body(entries: list[dict]) -> str:
    items_xml = ""
    for e in entries:
        items_xml += (
            "<item>"
            f"<title>{e['title']}</title>"
            f"<link>{e['link']}</link>"
            f"<description>{e.get('summary', '')}</description>"
            + (f"<pubDate>{e['pubDate']}</pubDate>" if e.get("pubDate") else "")
            + "</item>"
        )
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0"><channel>'
        "<title>Sample Feed</title>"
        "<link>https://sample.example/</link>"
        "<description>x</description>"
        f"{items_xml}"
        "</channel></rss>"
    )


def _atom_body(entries: list[dict]) -> str:
    items_xml = ""
    for e in entries:
        items_xml += (
            "<entry>"
            f"<title>{e['title']}</title>"
            f"<link href='{e['link']}'/>"
            f"<id>{e['link']}</id>"
            + (f"<updated>{e['updated']}</updated>" if e.get("updated") else "")
            + (f"<summary>{e.get('summary','')}</summary>" if e.get("summary") else "")
            + "</entry>"
        )
    return (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Atom Sample</title>"
        "<id>urn:uuid:test</id>"
        "<updated>2026-05-04T12:00:00Z</updated>"
        f"{items_xml}"
        "</feed>"
    )


def _rss_pubdate(dt: datetime) -> str:
    # RFC 822 / 2822: Tue, 05 May 2026 09:00:00 GMT
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _atom_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Helpers ------------------------------------------------------------

def _client_with_routes(routes: dict[str, httpx.Response]) -> httpx.Client:
    """Mock transport that returns route[url] for an exact URL match, else 404."""
    def handler(req: httpx.Request) -> httpx.Response:
        return routes.get(str(req.url), httpx.Response(404, text="not found"))
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Tests --------------------------------------------------------------

class IsParseableFeedTest(unittest.TestCase):
    def test_rss_body_parses(self) -> None:
        body = _rss_body([{"title": "x", "link": "https://e.com/1",
                           "pubDate": _rss_pubdate(datetime.now(timezone.utc))}])
        self.assertTrue(rf._is_parseable_feed(body))

    def test_atom_body_parses(self) -> None:
        body = _atom_body([{"title": "x", "link": "https://e.com/1",
                            "updated": _atom_iso(datetime.now(timezone.utc))}])
        self.assertTrue(rf._is_parseable_feed(body))

    def test_html_does_not_parse(self) -> None:
        self.assertFalse(rf._is_parseable_feed("<html><body>not a feed</body></html>"))

    def test_empty_does_not_parse(self) -> None:
        self.assertFalse(rf._is_parseable_feed(""))


class DiscoverFeedTest(unittest.TestCase):
    def test_first_heuristic_hit(self) -> None:
        body = _rss_body([{"title": "a", "link": "https://x.com/a",
                           "pubDate": _rss_pubdate(datetime.now(timezone.utc))}])
        http = _client_with_routes({
            "https://example.com/feed": httpx.Response(200, text=body),
        })
        url, method = rf.discover_feed_url("https://example.com", http=http)
        self.assertEqual(url, "https://example.com/feed")
        self.assertEqual(method, "heuristic_feed")

    def test_falls_through_to_atom_xml(self) -> None:
        body = _atom_body([{"title": "a", "link": "https://x.com/a",
                            "updated": _atom_iso(datetime.now(timezone.utc))}])
        http = _client_with_routes({
            "https://example.com/atom.xml": httpx.Response(200, text=body),
        })
        url, method = rf.discover_feed_url("https://example.com", http=http)
        self.assertEqual(url, "https://example.com/atom.xml")
        self.assertEqual(method, "heuristic_atom_xml")

    def test_html_link_discovery(self) -> None:
        feed_body = _rss_body([{"title": "a", "link": "https://x.com/a",
                                "pubDate": _rss_pubdate(datetime.now(timezone.utc))}])
        html = (
            '<html><head>'
            '<link rel="alternate" type="application/rss+xml" href="/custom-rss">'
            '</head><body></body></html>'
        )
        http = _client_with_routes({
            "https://example.com": httpx.Response(200, text=html),
            "https://example.com/custom-rss": httpx.Response(200, text=feed_body),
        })
        url, method = rf.discover_feed_url("https://example.com", http=http)
        self.assertEqual(url, "https://example.com/custom-rss")
        self.assertEqual(method, "html_link")

    def test_all_paths_fail(self) -> None:
        http = _client_with_routes({})  # all 404
        url, method = rf.discover_feed_url("https://example.com", http=http)
        self.assertIsNone(url)
        self.assertEqual(method, "failed")


class FetchFeedTest(unittest.TestCase):
    def test_time_filter_rss(self) -> None:
        now = datetime.now(timezone.utc)
        entries = [
            {"title": "fresh", "link": "https://e.com/fresh",
             "pubDate": _rss_pubdate(now - timedelta(hours=1))},
            {"title": "borderline", "link": "https://e.com/border",
             "pubDate": _rss_pubdate(now - timedelta(hours=12))},
            {"title": "stale", "link": "https://e.com/stale",
             "pubDate": _rss_pubdate(now - timedelta(hours=25))},
            {"title": "ancient", "link": "https://e.com/ancient",
             "pubDate": _rss_pubdate(now - timedelta(days=7))},
            {"title": "no_date", "link": "https://e.com/nodate"},  # missing pubDate
        ]
        body = _rss_body(entries)
        http = _client_with_routes({"https://feed.example/": httpx.Response(200, text=body)})
        signals, info = rf.fetch_feed(
            "https://feed.example/",
            source_name="Sample",
            since=now - timedelta(hours=24),
            http=http,
        )
        titles = {s.title for s in signals}
        self.assertEqual(titles, {"fresh", "borderline"})
        self.assertEqual(info.items_total, 5)
        self.assertEqual(info.items_within_window, 2)

    def test_atom_feed_fields_normalized(self) -> None:
        now = datetime.now(timezone.utc)
        entries = [{
            "title": "Atom one",
            "link": "https://atom.example/post1",
            "updated": _atom_iso(now - timedelta(hours=2)),
            "summary": "An atom summary.",
        }]
        body = _atom_body(entries)
        http = _client_with_routes({"https://atom.example/feed": httpx.Response(200, text=body)})
        signals, _info = rf.fetch_feed(
            "https://atom.example/feed",
            source_name="Atom Sample",
            since=now - timedelta(hours=24),
            http=http,
        )
        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s.title, "Atom one")
        self.assertEqual(s.url, "https://atom.example/post1")
        self.assertEqual(s.source, "Atom Sample")
        self.assertEqual(s.source_type, "rss")
        self.assertEqual(s.summary, "An atom summary.")
        self.assertLess((now - s.published_at).total_seconds(), 60 * 60 * 3)

    def test_bad_xml_returns_empty(self) -> None:
        body = "<not valid xml<"
        http = _client_with_routes({"https://feed.example/": httpx.Response(200, text=body)})
        signals, info = rf.fetch_feed(
            "https://feed.example/", source_name="Bad",
            since=datetime.now(timezone.utc) - timedelta(hours=24),
            http=http,
        )
        self.assertEqual(signals, [])
        self.assertEqual(info.items_total, 0)

    def test_404_returns_empty_with_status(self) -> None:
        http = _client_with_routes({})
        signals, info = rf.fetch_feed(
            "https://nope.example/", source_name="Nope",
            since=datetime.now(timezone.utc) - timedelta(hours=24),
            http=http,
        )
        self.assertEqual(signals, [])
        self.assertEqual(info.status, 404)
        self.assertIsNotNone(info.error)


class FetchAllNewslettersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._patch_logs = mock.patch.object(config, "LOGS_DIR", Path(self.tmp.name))
        self._patch_logs.start()

    def tearDown(self) -> None:
        self._patch_logs.stop()
        self.tmp.cleanup()

    def test_two_newsletters_one_works_one_fails(self) -> None:
        now = datetime.now(timezone.utc)
        good_body = _rss_body([
            {"title": "Good One", "link": "https://good.example/post1",
             "pubDate": _rss_pubdate(now - timedelta(hours=2))},
        ])
        http = _client_with_routes({
            "https://good.example/feed": httpx.Response(200, text=good_body),
            # bad newsletter: nothing routes; all 4 heuristics + HTML 404 → "failed"
        })
        nls = [
            Newsletter(tier=1, name="Good", geography="US", type_="Newsletter",
                       author="x", description="y", reach="z",
                       url="https://good.example"),
            Newsletter(tier=1, name="Bad", geography="US", type_="Newsletter",
                       author="x", description="y", reach="z",
                       url="https://bad.example"),
        ]
        signals = rf.fetch_all_newsletters(http=http, newsletters=nls)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "Good")

        log_files = list(Path(self.tmp.name).glob("rss_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        lines = [json.loads(l) for l in log_files[0].read_text().strip().split("\n")]
        self.assertEqual(len(lines), 2)
        by_source = {l["source"]: l for l in lines}
        self.assertEqual(by_source["Good"]["discovery"], "heuristic_feed")
        self.assertEqual(by_source["Good"]["items_within_window"], 1)
        self.assertEqual(by_source["Bad"]["discovery"], "failed")
        self.assertEqual(by_source["Bad"]["items_within_window"], 0)

    def test_signals_sorted_descending_by_published_at(self) -> None:
        now = datetime.now(timezone.utc)
        body = _rss_body([
            {"title": "older", "link": "https://e.com/o",
             "pubDate": _rss_pubdate(now - timedelta(hours=10))},
            {"title": "newer", "link": "https://e.com/n",
             "pubDate": _rss_pubdate(now - timedelta(hours=1))},
        ])
        http = _client_with_routes({
            "https://e.com/feed": httpx.Response(200, text=body),
        })
        nls = [Newsletter(tier=1, name="N", geography="US", type_="x",
                          author="x", description="x", reach="x",
                          url="https://e.com")]
        signals = rf.fetch_all_newsletters(http=http, newsletters=nls)
        self.assertEqual([s.title for s in signals], ["newer", "older"])


@unittest.skipUnless(os.getenv("RUN_LIVE") == "1",
                     "set RUN_LIVE=1 for live RSS fetch")
class LiveSmokeTest(unittest.TestCase):
    def test_kff_health_news_feed(self) -> None:
        """Hits the real KFF Health News feed; verifies discovery + parse end-to-end."""
        with rf._make_default_http() as http:
            url, method = rf.discover_feed_url("https://kffhealthnews.org/", http=http)
        self.assertIsNotNone(url)
        self.assertNotEqual(method, "failed")


if __name__ == "__main__":
    unittest.main()
