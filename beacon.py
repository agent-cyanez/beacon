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
import sqlite3
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
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "30"))
HISTORY_DB = os.environ.get("HISTORY_DB", "/data/beacon.db")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "90"))


class UptimeDB:
    def __init__(self, db_path, history_days=90):
        self._db_path = db_path
        self._history_days = history_days
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS checks ("
            "service TEXT NOT NULL, level TEXT NOT NULL, ts INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checks_service_ts ON checks(service, ts)"
        )
        conn.commit()
        conn.close()

    def record(self, services):
        ts = int(time.time())
        conn = sqlite3.connect(self._db_path)
        conn.executemany(
            "INSERT INTO checks (service, level, ts) VALUES (?, ?, ?)",
            [(s["name"], s["level"], ts) for s in services],
        )
        cutoff = ts - self._history_days * 86400
        conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()

    def daily_uptime(self, service, days=None):
        days = days or self._history_days
        cutoff = int(time.time()) - days * 86400
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN level = 'operational' THEN 1 ELSE 0 END) AS up "
            "FROM checks WHERE service = ? AND ts >= ? "
            "GROUP BY day ORDER BY day",
            (service, cutoff),
        ).fetchall()
        conn.close()
        return {row[0]: (row[2] / row[1] * 100) for row in rows}

    def overall_uptime(self, service, days=None):
        days = days or self._history_days
        cutoff = int(time.time()) - days * 86400
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN level = 'operational' THEN 1 ELSE 0 END) AS up "
            "FROM checks WHERE service = ? AND ts >= ?",
            (service, cutoff),
        ).fetchone()
        conn.close()
        if not row or row[0] == 0:
            return None
        return row[1] / row[0] * 100


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
{refresh_meta}
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
.history-section {{ margin-top: 1.5rem; }}
.history-title {{
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--muted);
  margin-bottom: 0.75rem;
}}
.history-row {{
  padding: 0.75rem 1.25rem;
  border-bottom: 1px solid var(--border);
}}
.history-row:last-child {{ border-bottom: none; }}
.history-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.4rem;
}}
.history-name {{ font-size: 0.85rem; font-weight: 500; }}
.history-pct {{ font-size: 0.8rem; color: var(--muted); }}
.uptime-bar {{
  display: flex;
  gap: 1px;
  height: 24px;
  align-items: stretch;
}}
.uptime-bar .day {{
  flex: 1;
  border-radius: 2px;
  min-width: 1px;
  position: relative;
}}
.uptime-bar .day.good {{ background: var(--green); opacity: 0.8; }}
.uptime-bar .day.warn {{ background: var(--yellow); opacity: 0.8; }}
.uptime-bar .day.bad {{ background: var(--red); opacity: 0.8; }}
.uptime-bar .day.none {{ background: var(--border); opacity: 0.5; }}
.uptime-bar .day:hover {{ opacity: 1; }}
.uptime-bar .day .tip {{
  display: none;
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--card);
  border: 1px solid var(--border);
  padding: 0.25rem 0.5rem;
  border-radius: 4px;
  font-size: 0.7rem;
  white-space: nowrap;
  z-index: 10;
  color: var(--text);
  box-shadow: 0 2px 4px rgba(0,0,0,0.2);
}}
.uptime-bar .day:hover .tip {{ display: block; }}
.history-legend {{
  display: flex;
  justify-content: space-between;
  margin-top: 0.25rem;
  font-size: 0.7rem;
  color: var(--muted);
}}
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
  {history_html}
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

HISTORY_ROW = """<div class="history-row">
  <div class="history-header">
    <span class="history-name">{name}</span>
    <span class="history-pct">{pct}</span>
  </div>
  <div class="uptime-bar">{days_html}</div>
  <div class="history-legend"><span>{days_ago}d ago</span><span>Today</span></div>
</div>"""

HISTORY_DAY = '<div class="day {cls}" title="{date}: {pct}"><span class="tip">{date}: {pct}</span></div>'


def day_class(pct):
    if pct is None:
        return "none"
    if pct >= 99.0:
        return "good"
    if pct >= 95.0:
        return "warn"
    return "bad"


def render_history(services, uptime_db, history_days):
    if uptime_db is None:
        return ""
    service_names = sorted(set(s["name"] for s in services))
    if not service_names:
        return ""
    today = time.localtime()
    dates = []
    for i in range(history_days - 1, -1, -1):
        t = time.localtime(time.time() - i * 86400)
        dates.append(time.strftime("%Y-%m-%d", t))
    rows = []
    for name in service_names:
        daily = uptime_db.daily_uptime(name, history_days)
        if not daily:
            continue
        overall = uptime_db.overall_uptime(name, history_days)
        pct_str = f"{overall:.2f}%" if overall is not None else "—"
        days_html_parts = []
        for d in dates:
            pct = daily.get(d)
            cls = day_class(pct)
            pct_label = f"{pct:.1f}%" if pct is not None else "No data"
            days_html_parts.append(HISTORY_DAY.format(
                cls=cls, date=d, pct=pct_label,
            ))
        rows.append(HISTORY_ROW.format(
            name=html.escape(name),
            pct=pct_str,
            days_html="".join(days_html_parts),
            days_ago=history_days,
        ))
    if not rows:
        return ""
    return (
        '<div class="history-section">'
        '<div class="history-title">{days}-Day Uptime</div>'
        '<div class="services">{rows}</div>'
        '</div>'
    ).format(days=history_days, rows="\n".join(rows))


def render_page(services, title, description, show_response_time=False, uptime_db=None,
                history_days=90, refresh_interval=0):
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
    refresh_meta = ""
    if refresh_interval > 0:
        refresh_meta = f'<meta http-equiv="refresh" content="{refresh_interval}">'
    updated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    history = render_history(services, uptime_db, history_days)
    return PAGE_TEMPLATE.format(
        title=html.escape(title),
        description_html=desc_html,
        overall_level=level,
        overall_label=html.escape(label),
        refresh_meta=refresh_meta,
        service_rows="\n    ".join(rows),
        history_html=history,
        updated=updated,
    )


def build_api_response(services, uptime_db=None, history_days=90):
    level, label = overall_status(services)
    api_services = []
    for s in services:
        entry = {
            "name": s["name"],
            "level": s["level"],
            "label": s["label"],
        }
        if s.get("uptime"):
            entry["uptime"] = s["uptime"]
        if s.get("response_ms") is not None:
            entry["response_ms"] = s["response_ms"]
        if uptime_db:
            pct = uptime_db.overall_uptime(s["name"], history_days)
            if pct is not None:
                entry["uptime_pct"] = round(pct, 4)
        api_services.append(entry)
    return {
        "status": {"level": level, "label": label},
        "services": api_services,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


class StatusStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._html = ""
        self._api = {}

    def update(self, html_content, api_data=None):
        with self._lock:
            self._html = html_content
            if api_data is not None:
                self._api = api_data

    def get(self):
        with self._lock:
            return self._html

    def get_api(self):
        with self._lock:
            return self._api


store = StatusStore()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/api/status":
            api_data = store.get_api()
            if not api_data:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"initializing"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(json.dumps(api_data).encode())
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
           show_response_time, uptime_db, history_days, refresh_interval=0):
    while True:
        try:
            services = collect_status(docker, service_filter)
            if endpoints:
                services.extend(collect_endpoint_status(endpoints, endpoint_timeout))
            if uptime_db:
                uptime_db.record(services)
            page = render_page(services, title, description, show_response_time,
                               uptime_db, history_days, refresh_interval)
            api = build_api_response(services, uptime_db, history_days)
            store.update(page, api)
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

    uptime_db = None
    if HISTORY_DB:
        uptime_db = UptimeDB(HISTORY_DB, HISTORY_DAYS)
        print(f"  History: {HISTORY_DB} ({HISTORY_DAYS} days)")

    docker = DockerClient(DOCKER_SOCKET)

    services = collect_status(docker, service_filter)
    if endpoints:
        services.extend(collect_endpoint_status(endpoints, ENDPOINT_TIMEOUT))
    if uptime_db:
        uptime_db.record(services)
    if REFRESH_INTERVAL > 0:
        print(f"  Auto-refresh: {REFRESH_INTERVAL}s")

    page = render_page(services, SITE_TITLE, SITE_DESCRIPTION, SHOW_RESPONSE_TIME,
                       uptime_db, HISTORY_DAYS, REFRESH_INTERVAL)
    api = build_api_response(services, uptime_db, HISTORY_DAYS)
    store.update(page, api)
    print(f"  Found {len(services)} services")

    t = threading.Thread(
        target=poller,
        args=(docker, service_filter, endpoints, ENDPOINT_TIMEOUT,
              SITE_TITLE, SITE_DESCRIPTION, SHOW_RESPONSE_TIME,
              uptime_db, HISTORY_DAYS, REFRESH_INTERVAL),
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
