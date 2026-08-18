"""Unit tests for Beacon — Docker status page."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import beacon


class TestParseServicesConfig(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(beacon.parse_services_config(""), {})

    def test_simple_names(self):
        result = beacon.parse_services_config("app,db")
        self.assertEqual(result, {"app": "app", "db": "db"})

    def test_display_names(self):
        result = beacon.parse_services_config("immich_server:Photos,forgejo:Git")
        self.assertEqual(result, {"immich_server": "Photos", "forgejo": "Git"})

    def test_mixed(self):
        result = beacon.parse_services_config("app,db:Database")
        self.assertEqual(result, {"app": "app", "db": "Database"})

    def test_whitespace(self):
        result = beacon.parse_services_config(" app , db : Database ")
        self.assertEqual(result, {"app": "app", "db": "Database"})


class TestParseEndpointsConfig(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(beacon.parse_endpoints_config(""), [])

    def test_url_with_name(self):
        result = beacon.parse_endpoints_config("https://example.com:Example")
        self.assertEqual(result, [{"url": "https://example.com", "name": "Example"}])

    def test_url_without_name(self):
        result = beacon.parse_endpoints_config("https://example.com")
        self.assertEqual(result, [{"url": "https://example.com", "name": "example.com"}])

    def test_url_with_path(self):
        result = beacon.parse_endpoints_config("https://example.com/health")
        self.assertEqual(result, [{"url": "https://example.com/health", "name": "example.com"}])

    def test_multiple(self):
        result = beacon.parse_endpoints_config(
            "https://a.com:Alpha,https://b.com:Beta"
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "Alpha")
        self.assertEqual(result[1]["name"], "Beta")

    def test_http_url_with_port(self):
        result = beacon.parse_endpoints_config("http://localhost:8080:Local")
        self.assertEqual(result, [{"url": "http://localhost:8080", "name": "Local"}])

    def test_whitespace(self):
        result = beacon.parse_endpoints_config(" https://a.com : Alpha ")
        self.assertEqual(result[0]["url"], "https://a.com")
        self.assertEqual(result[0]["name"], "Alpha")


class TestCheckEndpoint(unittest.TestCase):
    @patch("beacon.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        level, label, ms = beacon.check_endpoint("https://example.com", 5)
        self.assertEqual(level, "operational")
        self.assertIsNotNone(ms)

    @patch("beacon.urllib.request.urlopen")
    def test_server_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.com", 500, "Server Error", {}, None
        )
        level, label, ms = beacon.check_endpoint("https://example.com", 5)
        self.assertEqual(level, "down")
        self.assertIn("500", label)

    @patch("beacon.urllib.request.urlopen")
    def test_client_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.com", 403, "Forbidden", {}, None
        )
        level, label, ms = beacon.check_endpoint("https://example.com", 5)
        self.assertEqual(level, "degraded")

    @patch("beacon.urllib.request.urlopen")
    def test_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        level, label, ms = beacon.check_endpoint("https://example.com", 5)
        self.assertEqual(level, "down")
        self.assertEqual(label, "Unreachable")
        self.assertIsNone(ms)


class TestCollectEndpointStatus(unittest.TestCase):
    @patch("beacon.check_endpoint")
    def test_collects(self, mock_check):
        mock_check.return_value = ("operational", "Operational", 42)
        endpoints = [{"url": "https://a.com", "name": "Alpha"}]
        result = beacon.collect_endpoint_status(endpoints, 5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Alpha")
        self.assertEqual(result[0]["response_ms"], 42)


class TestContainerName(unittest.TestCase):
    def test_extracts_name(self):
        c = {"Names": ["/myapp"], "Id": "abc123def456"}
        self.assertEqual(beacon.container_name(c), "myapp")

    def test_falls_back_to_id(self):
        c = {"Names": [], "Id": "abc123def456789"}
        self.assertEqual(beacon.container_name(c), "abc123def456")


class TestContainerStatus(unittest.TestCase):
    def test_healthy(self):
        c = {"State": "running", "Status": "Up 2 hours (healthy)"}
        level, label = beacon.container_status(c)
        self.assertEqual(level, "operational")

    def test_unhealthy(self):
        c = {"State": "running", "Status": "Up 2 hours (unhealthy)"}
        level, label = beacon.container_status(c)
        self.assertEqual(level, "degraded")

    def test_running(self):
        c = {"State": "running", "Status": "Up 2 hours"}
        level, label = beacon.container_status(c)
        self.assertEqual(level, "operational")

    def test_exited(self):
        c = {"State": "exited", "Status": "Exited (0) 1 hour ago"}
        level, label = beacon.container_status(c)
        self.assertEqual(level, "down")

    def test_restarting(self):
        c = {"State": "restarting", "Status": "Restarting"}
        level, label = beacon.container_status(c)
        self.assertEqual(level, "degraded")


class TestUptimeText(unittest.TestCase):
    def test_running(self):
        c = {"Status": "Up 2 hours"}
        self.assertEqual(beacon.uptime_text(c), "2 hours")

    def test_with_health(self):
        c = {"Status": "Up 3 weeks (healthy)"}
        self.assertEqual(beacon.uptime_text(c), "3 weeks")

    def test_exited(self):
        c = {"Status": "Exited (0) 1 hour ago"}
        self.assertEqual(beacon.uptime_text(c), "")


class TestOverallStatus(unittest.TestCase):
    def test_all_operational(self):
        services = [{"level": "operational"}, {"level": "operational"}]
        level, label = beacon.overall_status(services)
        self.assertEqual(level, "operational")

    def test_one_down(self):
        services = [{"level": "operational"}, {"level": "down"}]
        level, label = beacon.overall_status(services)
        self.assertEqual(level, "down")

    def test_degraded(self):
        services = [{"level": "operational"}, {"level": "degraded"}]
        level, label = beacon.overall_status(services)
        self.assertEqual(level, "degraded")

    def test_empty(self):
        level, label = beacon.overall_status([])
        self.assertEqual(level, "unknown")


class TestMatchService(unittest.TestCase):
    def test_exact_match(self):
        service_filter = {"app": "Application", "db": "Database"}
        self.assertEqual(beacon.match_service("app", service_filter), "Application")

    def test_glob_match(self):
        service_filter = {"immich_*": "Immich", "blog-web-*": "Blog"}
        self.assertEqual(beacon.match_service("immich_server", service_filter), "Immich")
        self.assertEqual(beacon.match_service("blog-web-abc123", service_filter), "Blog")

    def test_no_match(self):
        service_filter = {"immich_*": "Immich"}
        self.assertIsNone(beacon.match_service("creditu-mongo", service_filter))


class TestCollectStatus(unittest.TestCase):
    def test_filters_by_name(self):
        class FakeDocker:
            def containers(self, all_containers=False):
                return [
                    {"Names": ["/app"], "Id": "a" * 12, "State": "running", "Status": "Up 1 hour"},
                    {"Names": ["/db"], "Id": "b" * 12, "State": "running", "Status": "Up 1 hour"},
                    {"Names": ["/hidden"], "Id": "c" * 12, "State": "running", "Status": "Up 1 hour"},
                ]

        result = beacon.collect_status(FakeDocker(), {"app": "Application", "db": "db"})
        names = [s["name"] for s in result]
        self.assertEqual(sorted(names), ["Application", "db"])

    def test_glob_filter(self):
        class FakeDocker:
            def containers(self, all_containers=False):
                return [
                    {"Names": ["/immich_server"], "Id": "a" * 12, "State": "running", "Status": "Up 1 hour"},
                    {"Names": ["/creditu-mongo"], "Id": "c" * 12, "State": "running", "Status": "Up 1 hour"},
                ]

        result = beacon.collect_status(FakeDocker(), {"immich_*": "Immich"})
        names = [s["name"] for s in result]
        self.assertEqual(names, ["Immich"])

    def test_glob_dedup_keeps_best(self):
        class FakeDocker:
            def containers(self, all_containers=False):
                return [
                    {"Names": ["/blog-web-abc123"], "Id": "a" * 12, "State": "running", "Status": "Up 2 weeks"},
                    {"Names": ["/blog-web-abc123_replaced_old1"], "Id": "b" * 12, "State": "exited", "Status": "Exited (255) 6 months ago"},
                    {"Names": ["/blog-web-abc123_replaced_old2"], "Id": "c" * 12, "State": "exited", "Status": "Exited (255) 6 months ago"},
                ]

        result = beacon.collect_status(FakeDocker(), {"blog-web-*": "Blog"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Blog")
        self.assertEqual(result[0]["level"], "operational")

    def test_no_filter_shows_all(self):
        class FakeDocker:
            def containers(self, all_containers=False):
                return [
                    {"Names": ["/a"], "Id": "a" * 12, "State": "running", "Status": "Up"},
                    {"Names": ["/b"], "Id": "b" * 12, "State": "running", "Status": "Up"},
                ]

        result = beacon.collect_status(FakeDocker(), {})
        self.assertEqual(len(result), 2)

    def test_includes_response_ms_field(self):
        class FakeDocker:
            def containers(self, all_containers=False):
                return [
                    {"Names": ["/a"], "Id": "a" * 12, "State": "running", "Status": "Up"},
                ]

        result = beacon.collect_status(FakeDocker(), {})
        self.assertIsNone(result[0]["response_ms"])


class TestRenderPage(unittest.TestCase):
    def test_renders_html(self):
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "2 hours", "response_ms": None},
        ]
        result = beacon.render_page(services, "Test Status", "A test page")
        self.assertIn("Test Status", result)
        self.assertIn("A test page", result)
        self.assertIn("App", result)
        self.assertIn("Operational", result)

    def test_escapes_html(self):
        services = [
            {"name": "<script>alert(1)</script>", "level": "operational", "label": "OK", "uptime": "", "response_ms": None},
        ]
        result = beacon.render_page(services, "Test", "")
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_shows_response_time(self):
        services = [
            {"name": "API", "level": "operational", "label": "Operational", "uptime": "", "response_ms": 42},
        ]
        result = beacon.render_page(services, "Test", "", show_response_time=True)
        self.assertIn("42ms", result)

    def test_hides_response_time_by_default(self):
        services = [
            {"name": "API", "level": "operational", "label": "Operational", "uptime": "", "response_ms": 42},
        ]
        result = beacon.render_page(services, "Test", "")
        self.assertNotIn("42ms", result)

    def test_uptime_takes_precedence_over_response_time(self):
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "2 hours", "response_ms": 42},
        ]
        result = beacon.render_page(services, "Test", "", show_response_time=True)
        self.assertIn("2 hours", result)
        self.assertNotIn("42ms", result)


class TestUptimeDB(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmpdir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        os.rmdir(self._tmpdir)

    def test_creates_db(self):
        db = beacon.UptimeDB(self.db_path)
        self.assertTrue(os.path.exists(self.db_path))

    def test_record_and_query(self):
        db = beacon.UptimeDB(self.db_path)
        services = [
            {"name": "App", "level": "operational"},
            {"name": "DB", "level": "down"},
        ]
        db.record(services)
        pct = db.overall_uptime("App", 1)
        self.assertEqual(pct, 100.0)
        pct = db.overall_uptime("DB", 1)
        self.assertEqual(pct, 0.0)

    def test_overall_uptime_no_data(self):
        db = beacon.UptimeDB(self.db_path)
        self.assertIsNone(db.overall_uptime("NoSuch", 1))

    def test_daily_uptime(self):
        db = beacon.UptimeDB(self.db_path)
        db.record([{"name": "App", "level": "operational"}])
        db.record([{"name": "App", "level": "operational"}])
        db.record([{"name": "App", "level": "down"}])
        daily = db.daily_uptime("App", 1)
        self.assertEqual(len(daily), 1)
        today = time.strftime("%Y-%m-%d", time.localtime())
        self.assertAlmostEqual(daily[today], 66.67, places=1)

    def test_purges_old_data(self):
        import sqlite3
        db = beacon.UptimeDB(self.db_path, history_days=1)
        conn = sqlite3.connect(self.db_path)
        old_ts = int(time.time()) - 3 * 86400
        conn.execute(
            "INSERT INTO checks (service, level, ts) VALUES (?, ?, ?)",
            ("Old", "operational", old_ts),
        )
        conn.commit()
        conn.close()
        db.record([{"name": "New", "level": "operational"}])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM checks WHERE service = 'Old'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


class TestDayClass(unittest.TestCase):
    def test_none(self):
        self.assertEqual(beacon.day_class(None), "none")

    def test_good(self):
        self.assertEqual(beacon.day_class(100.0), "good")
        self.assertEqual(beacon.day_class(99.0), "good")

    def test_warn(self):
        self.assertEqual(beacon.day_class(98.9), "warn")
        self.assertEqual(beacon.day_class(95.0), "warn")

    def test_bad(self):
        self.assertEqual(beacon.day_class(94.9), "bad")
        self.assertEqual(beacon.day_class(0.0), "bad")


class TestRenderHistory(unittest.TestCase):
    def test_no_db(self):
        self.assertEqual(beacon.render_history([], None, 90), "")

    def test_no_services(self):
        db = MagicMock()
        self.assertEqual(beacon.render_history([], db, 90), "")

    def test_no_data(self):
        db = MagicMock()
        db.daily_uptime.return_value = {}
        services = [{"name": "App", "level": "operational"}]
        self.assertEqual(beacon.render_history(services, db, 90), "")

    def test_renders_history(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        db = beacon.UptimeDB(db_path)
        db.record([{"name": "App", "level": "operational"}])
        services = [{"name": "App", "level": "operational"}]
        result = beacon.render_history(services, db, 90)
        self.assertIn("90-Day Uptime", result)
        self.assertIn("App", result)
        self.assertIn("100.00%", result)
        os.unlink(db_path)
        os.rmdir(tmpdir)


class TestRenderPageWithHistory(unittest.TestCase):
    def test_renders_without_history(self):
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "", "response_ms": None},
        ]
        result = beacon.render_page(services, "Test", "")
        self.assertIn("App", result)
        self.assertNotIn("Day Uptime", result)

    def test_renders_with_history(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        db = beacon.UptimeDB(db_path)
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "", "response_ms": None},
        ]
        db.record(services)
        result = beacon.render_page(services, "Test", "", uptime_db=db, history_days=7)
        self.assertIn("7-Day Uptime", result)
        self.assertIn("App", result)
        os.unlink(db_path)
        os.rmdir(tmpdir)


class TestBuildApiResponse(unittest.TestCase):
    def test_basic_response(self):
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "2 weeks", "response_ms": None},
            {"name": "DB", "level": "down", "label": "Down", "uptime": "", "response_ms": None},
        ]
        result = beacon.build_api_response(services)
        self.assertEqual(result["status"]["level"], "down")
        self.assertEqual(result["status"]["label"], "Partial Outage")
        self.assertEqual(len(result["services"]), 2)
        self.assertEqual(result["services"][0]["name"], "App")
        self.assertEqual(result["services"][0]["uptime"], "2 weeks")
        self.assertNotIn("response_ms", result["services"][0])
        self.assertIn("updated", result)

    def test_response_ms_included(self):
        services = [
            {"name": "Blog", "level": "operational", "label": "Operational", "uptime": "", "response_ms": 42},
        ]
        result = beacon.build_api_response(services)
        self.assertEqual(result["services"][0]["response_ms"], 42)

    def test_uptime_pct_with_db(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        db = beacon.UptimeDB(db_path)
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "", "response_ms": None},
        ]
        db.record(services)
        result = beacon.build_api_response(services, uptime_db=db, history_days=90)
        self.assertIn("uptime_pct", result["services"][0])
        self.assertEqual(result["services"][0]["uptime_pct"], 100.0)
        os.unlink(db_path)
        os.rmdir(tmpdir)

    def test_empty_services(self):
        result = beacon.build_api_response([])
        self.assertEqual(result["status"]["level"], "unknown")
        self.assertEqual(result["services"], [])


class TestStatusStoreApi(unittest.TestCase):
    def test_store_and_retrieve_api(self):
        s = beacon.StatusStore()
        api_data = {"status": {"level": "operational"}, "services": []}
        s.update("<html>test</html>", api_data)
        self.assertEqual(s.get(), "<html>test</html>")
        self.assertEqual(s.get_api(), api_data)

    def test_api_empty_initially(self):
        s = beacon.StatusStore()
        self.assertEqual(s.get_api(), {})


class TestApiHandler(unittest.TestCase):
    def _make_handler(self, path):
        handler = MagicMock(spec=beacon.Handler)
        handler.path = path
        handler.wfile = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_api_status_returns_json(self):
        api_data = {"status": {"level": "operational", "label": "All Systems Operational"}, "services": [], "updated": "2026-01-01T00:00:00Z"}
        beacon.store.update("<html></html>", api_data)
        handler = self._make_handler("/api/status")
        beacon.Handler.do_GET(handler)
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "application/json")

    def test_api_status_503_when_empty(self):
        beacon.store = beacon.StatusStore()
        handler = self._make_handler("/api/status")
        beacon.Handler.do_GET(handler)
        handler.send_response.assert_called_with(503)


class TestAutoRefresh(unittest.TestCase):
    def test_refresh_meta_included(self):
        services = [
            {"name": "App", "level": "operational", "label": "OK", "uptime": "", "response_ms": None},
        ]
        result = beacon.render_page(services, "Test", "", refresh_interval=30)
        self.assertIn('<meta http-equiv="refresh" content="30">', result)

    def test_refresh_meta_excluded_when_zero(self):
        services = [
            {"name": "App", "level": "operational", "label": "OK", "uptime": "", "response_ms": None},
        ]
        result = beacon.render_page(services, "Test", "", refresh_interval=0)
        self.assertNotIn("http-equiv", result)

    def test_refresh_meta_excluded_by_default(self):
        services = [
            {"name": "App", "level": "operational", "label": "OK", "uptime": "", "response_ms": None},
        ]
        result = beacon.render_page(services, "Test", "")
        self.assertNotIn("http-equiv", result)


if __name__ == "__main__":
    unittest.main()
