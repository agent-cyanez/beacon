"""Unit tests for Beacon — Docker status page."""

import unittest
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


class TestRenderPage(unittest.TestCase):
    def test_renders_html(self):
        services = [
            {"name": "App", "level": "operational", "label": "Operational", "uptime": "2 hours"},
        ]
        html = beacon.render_page(services, "Test Status", "A test page")
        self.assertIn("Test Status", html)
        self.assertIn("A test page", html)
        self.assertIn("App", html)
        self.assertIn("Operational", html)

    def test_escapes_html(self):
        services = [
            {"name": "<script>alert(1)</script>", "level": "operational", "label": "OK", "uptime": ""},
        ]
        html = beacon.render_page(services, "Test", "")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
