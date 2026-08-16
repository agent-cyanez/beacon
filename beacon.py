#!/usr/bin/env python3
"""Beacon — lightweight Docker status page. Zero dependencies."""

import fnmatch
import html
import http.client
import http.server
import json
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.request


DOCKER_SOCKET = os.environ.get("DOCKER_HOST", "/var/run/docker.sock")
LISTEN_PORT = int(os.environ.get("PORT", "8585"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
SITE_TITLE = os.environ.get("SITE_TITLE", "Service Status")
SITE_DESCRIPTION = os.environ.get("SITE_DESCRIPTION", "")
SERVICES = os.environ.get("SERVICES", "")
ENDPOINTS = os.environ.get("ENDPOINTS", "")
ENDPOINT_TIMEOUT = int(os.environ.get("ENDPOINT_TIMEOUT", "10"))
SHOW_RESPONSE_TIME = os.environ.get("SHOW_RESPONSE_TIME", "false").lower() == "true"


class DockerClient:
    def __init__(self, socket_path):
        self._socket_path = socket_path

    def _request(self, method, path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._socket_path)
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        conn.request(method, path)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status != 200:
            raise RuntimeError(f"Docker API {resp.status}: {data.decode()[:200]}")
        return json.loads(data)

    def containers(self, all_containers=False):
        path = "/containers/json"
        if all_containers:
            path += "?all=true"
        return self._request("GET", path)


def parse_services_config(raw):
    if not raw.strip():
        return {}
    services = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            container_name, display_name = entry.split(":", 1)
            services[container_name.strip()] = display_name.strip()
        else:
            services[entry] = entry
    return services


def container_name(c):
    return c["Names"][0].lstrip("/") if c.get("Names") else c["Id"][:12]


def container_status(c):
    state = c.get("State", "unknown")
    status_text = c.get("Status", "")
    if "(healthy)" in status_text:
        return "operational", "Operational"
    if "(unhealthy)" in status_text:
        return "degraded", "Degraded"
    if state == "running":
        return "operational", "Running"
    if state == "exited":
        return "down", "Down"
    if state == "restarting":
        return "degraded", "Restarting"
    return "unknown", state.capitalize()


def uptime_text(c):
    status = c.get("Status", "")
    if status.startswith("Up "):
        return status[3:].split("(")[0].strip()
    return ""


def match_service(name, service_filter):
    for pattern, display in service_filter.items():
        if fnmatch.fnmatch(name, pattern):
            return display
    return None


def parse_endpoints_config(raw):
    if not raw.strip():
        return []
    endpoints = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        last_colon = entry.rfind(":")
        scheme_end = entry.find("://")
        if last_colon > scheme_end + 2:
            url = entry[:last_colon].strip()
            name = entry[last_colon + 1:].strip()
        else:
            url = entry
            name = url.replace("https://", "").replace("http://", "").split("/")[0]
        endpoints.append({"url": url, "name": name})
    return endpoints


def check_endpoint(url, timeout):
    try:
        start = time.monotonic()
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "Beacon/1.0")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            code = resp.getcode()
            if 200 <= code < 400:
                return "operational", "Operational", elapsed_ms
            return "degraded", f"HTTP {code}", elapsed_ms
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if e.code >= 500:
            return "down", f"HTTP {e.code}", elapsed_ms
        return "degraded", f"HTTP {e.code}", elapsed_ms
    except Exception:
        return "down", "Unreachable", None


def collect_endpoint_status(endpoints, timeout):
    results = []
    for ep in endpoints:
        level, label, response_ms = check_endpoint(ep["url"], timeout)
        results.append({
            "name": ep["name"],
            "level": level,
            "label": label,
            "uptime": "",
            "response_ms": response_ms,
        })
    return results


_LEVEL_RANK = {"operational": 0, "degraded": 1, "down": 2, "unknown": 3}


def collect_status(docker, service_filter):
    containers = docker.containers(all_containers=True)
    results = []
    for c in containers:
        name = container_name(c)
        if service_filter:
            display = match_service(name, service_filter)
            if display is None:
                continue
        else:
            display = name
        level, label = container_status(c)
        uptime = uptime_text(c)
        results.append({
            "name": display,
            "level": level,
            "label": label,
            "uptime": uptime,
            "response_ms": None,
        })
    # When glob patterns match multiple containers to the same display name,
    # keep only the best-status one (e.g. running blog, not old _replaced_ copies).
    if service_filter:
        best = {}
        for s in results:
            existing = best.get(s["name"])
            if existing is None or _LEVEL_RANK.get(s["level"], 9) < _LEVEL_RANK.get(existing["level"], 9):
                best[s["name"]] = s
        results = list(best.values())
    results.sort(key=lambda s: s["name"].lower())
    return results


def overall_status(services):
    levels = {s["level"] for s in services}
    if "down" in levels:
        return "down", "Partial Outage"
    if "degraded" in levels:
        return "degraded", "Degraded Performance"
    if not services:
        return "unknown", "No Services Found"
    return "operational", "All Systems Operational"


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg: #0f1117;
  --card: #1a1d27;
  --border: #2a2d37;
  --text: #e4e4e7;
  --muted: #71717a;
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --blue: #3b82f6;
}}
@media (prefers-color-scheme: light) {{
  :root {{
    --bg: #f8f9fa;
    --card: #ffffff;
    --border: #e4e4e7;
    --text: #18181b;
    --muted: #71717a;
  }}
}}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  min-height: 100vh;
}}
.container {{ max-width: 640px; margin: 0 auto; padding: 2rem 1rem; }}
header {{ margin-bottom: 2rem; }}
h1 {{ font-size: 1.5rem; font-weight: 600; }}
.description {{ color: var(--muted); margin-top: 0.25rem; font-size: 0.9rem; }}
.overall {{
  padding: 1rem 1.25rem;
  border-radius: 0.5rem;
  background: var(--card);
  border: 1px solid var(--border);
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  gap: 0.75rem;
}}
.dot {{
  width: 10px; height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}}
.dot.operational {{ background: var(--green); box-shadow: 0 0 6px var(--green); }}
.dot.degraded {{ background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }}
.dot.down {{ background: var(--red); box-shadow: 0 0 6px var(--red); }}
.dot.unknown {{ background: var(--muted); }}
.overall-text {{ font-weight: 500; }}
.services {{
  border-radius: 0.5rem;
  background: var(--card);
  border: 1px solid var(--border);
  overflow: hidden;
}}
.service {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.875rem 1.25rem;
  border-bottom: 1px solid var(--border);
}}
.service:last-child {{ border-bottom: none; }}
.service-name {{ font-weight: 500; font-size: 0.95rem; }}
.service-right {{ display: flex; align-items: center; gap: 0.75rem; }}
.service-uptime {{ color: var(--muted); font-size: 0.8rem; }}
.badge {{
  font-size: 0.75rem;
  padding: 0.2rem 0.6rem;
  border-radius: 9999px;
  font-weight: 500;
}}
.badge.operational {{ background: rgba(34,197,94,0.12); color: var(--green); }}
.badge.degraded {{ background: rgba(234,179,8,0.12); color: var(--yellow); }}
.badge.down {{ background: rgba(239,68,68,0.12); color: var(--red); }}
.badge.unknown {{ background: rgba(113,113,122,0.12); color: var(--muted); }}
footer {{
  margin-top: 2rem;
  text-align: center;
  color: var(--muted);
  font-size: 0.8rem;
}}
footer a {{ color: var(--muted); text-decoration: none; }}
footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>{title}</h1>
    {description_html}
  </header>
  <div class="overall">
    <span class="dot {overall_level}"></span>
    <span class="overall-text">{overall_label}</span>
  </div>
  <div class="services">
    {service_rows}
  </div>
  <footer>
    Updated {updated} &middot; Powered by <a href="https://github.com/agent-cyanez/beacon">Beacon</a>
  </footer>
</div>
</body>
</html>"""

SERVICE_ROW = """<div class="service">
  <span class="service-name">{name}</span>
  <div class="service-right">
    {meta_html}
    <span class="badge {level}">{label}</span>
  </div>
</div>"""


def render_page(services, title, description, show_response_time=False):
    level, label = overall_status(services)
    rows = []
    for s in services:
        meta_html = ""
        if s.get("uptime"):
            meta_html = f'<span class="service-uptime">{html.escape(s["uptime"])}</span>'
        elif show_response_time and s.get("response_ms") is not None:
            meta_html = f'<span class="service-uptime">{s["response_ms"]}ms</span>'
        rows.append(SERVICE_ROW.format(
            name=html.escape(s["name"]),
            level=s["level"],
            label=html.escape(s["label"]),
            meta_html=meta_html,
        ))
    desc_html = ""
    if description:
        desc_html = f'<p class="description">{html.escape(description)}</p>'
    updated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    return PAGE_TEMPLATE.format(
        title=html.escape(title),
        description_html=desc_html,
        overall_level=level,
        overall_label=html.escape(label),
        service_rows="\n    ".join(rows),
        updated=updated,
    )


class StatusStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._html = ""

    def update(self, html_content):
        with self._lock:
            self._html = html_content

    def get(self):
        with self._lock:
            return self._html


store = StatusStore()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        content = store.get()
        if not content:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"initializing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(content.encode())

    def log_message(self, fmt, *args):
        pass


def poller(docker, service_filter, endpoints, endpoint_timeout, title, description,
           show_response_time):
    while True:
        try:
            services = collect_status(docker, service_filter)
            if endpoints:
                services.extend(collect_endpoint_status(endpoints, endpoint_timeout))
            page = render_page(services, title, description, show_response_time)
            store.update(page)
        except Exception as e:
            print(f"[poller error] {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


def main():
    print(f"Beacon starting — port {LISTEN_PORT}, polling every {POLL_INTERVAL}s")
    print(f"  Docker: {DOCKER_SOCKET}")
    print(f"  Title:  {SITE_TITLE}")

    service_filter = parse_services_config(SERVICES)
    if service_filter:
        print(f"  Services: {', '.join(service_filter.values())}")
    else:
        print("  Services: all containers")

    endpoints = parse_endpoints_config(ENDPOINTS)
    if endpoints:
        print(f"  Endpoints: {', '.join(e['name'] for e in endpoints)}")

    docker = DockerClient(DOCKER_SOCKET)

    services = collect_status(docker, service_filter)
    if endpoints:
        services.extend(collect_endpoint_status(endpoints, ENDPOINT_TIMEOUT))
    page = render_page(services, SITE_TITLE, SITE_DESCRIPTION, SHOW_RESPONSE_TIME)
    store.update(page)
    print(f"  Found {len(services)} services")

    t = threading.Thread(
        target=poller,
        args=(docker, service_filter, endpoints, ENDPOINT_TIMEOUT,
              SITE_TITLE, SITE_DESCRIPTION, SHOW_RESPONSE_TIME),
        daemon=True,
    )
    t.start()

    server = http.server.HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)

    def handle_signal(signum, frame):
        print(f"\nReceived signal {signum}, shutting down")
        server.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(f"  Serving at http://0.0.0.0:{LISTEN_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
