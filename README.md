# Beacon

[![CI](https://github.com/agent-cyanez/beacon/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-cyanez/beacon/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/agent-cyanez/beacon)](https://github.com/agent-cyanez/beacon/releases)
[![Container](https://img.shields.io/badge/ghcr.io-beacon-blue)](https://ghcr.io/agent-cyanez/beacon)

A lightweight, zero-dependency status page for Docker environments. Shows the health of your containers on a clean, responsive web page.

**Beacon** is the public-facing companion to [Lookout](https://github.com/agent-cyanez/lookout) (private alerting).

## Features

- Single Python file, zero pip dependencies
- Queries Docker socket directly
- Auto-detects container health status
- **90-day uptime history** with per-day visualization (SQLite-backed, zero new deps)
- HTTP endpoint monitoring (check if external URLs are reachable)
- Optional response time display
- Responsive dark/light theme (follows system preference)
- Filter which containers to display
- Custom display names for containers
- **JSON API** at `/api/status` for programmatic access
- Health endpoint at `/health`

## Quick Start

```bash
docker run -d \
  --name beacon \
  -p 8585:8585 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  ghcr.io/agent-cyanez/beacon:latest
```

Or with Docker Compose:

```yaml
services:
  beacon:
    image: ghcr.io/agent-cyanez/beacon:latest
    container_name: beacon
    restart: unless-stopped
    ports:
      - "8585:8585"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - beacon-data:/data
    environment:
      - SITE_TITLE=My Services
      - SERVICES=immich_server:Photos,forgejo:Git,ntfy:Notifications

volumes:
  beacon-data:
```

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `SITE_TITLE` | `Service Status` | Page heading |
| `SITE_DESCRIPTION` | *(empty)* | Subtitle shown below the title |
| `SERVICES` | *(empty — all)* | Comma-separated container filter (see below) |
| `ENDPOINTS` | *(empty)* | Comma-separated HTTP endpoints to monitor (see below) |
| `ENDPOINT_TIMEOUT` | `10` | Seconds before an endpoint check times out |
| `SHOW_RESPONSE_TIME` | `false` | Show response time for HTTP endpoints |
| `POLL_INTERVAL` | `15` | Seconds between Docker queries |
| `PORT` | `8585` | HTTP server port |
| `DOCKER_HOST` | `/var/run/docker.sock` | Docker socket path |
| `HISTORY_DB` | `/data/beacon.db` | SQLite database path for uptime history |
| `HISTORY_DAYS` | `90` | Number of days to retain in uptime history |

### Uptime History

Beacon records each service's status at every poll interval in a SQLite database (Python stdlib — no new dependencies). The status page displays a colored bar for each service showing daily uptime over the configured period:

- **Green**: 99%+ uptime that day
- **Yellow**: 95–99% uptime
- **Red**: Below 95% uptime
- **Grey**: No data

Mount a volume at `/data` to persist history across container restarts.

### Service Filter

The `SERVICES` variable controls which containers appear and how they're named:

```
SERVICES=immich_server:Photos,forgejo:Git,ntfy:Notifications
```

- `container_name:Display Name` — show container with a custom label
- `container_name` — show container with its Docker name
- Leave empty to show all running containers

### HTTP Endpoints

The `ENDPOINTS` variable adds external URL monitoring alongside containers:

```
ENDPOINTS=https://example.com:Website,https://api.example.com/health:API
```

- `url:Display Name` — monitor URL and show with a custom label
- `url` — monitor URL, display the hostname
- Supports HTTP and HTTPS
- Status: Operational (2xx), Degraded (4xx), Down (5xx/timeout/unreachable)

### JSON API

Beacon exposes a REST endpoint for programmatic access:

```
GET /api/status
```

Returns the current status of all services as JSON:

```json
{
  "status": { "level": "operational", "label": "All Systems Operational" },
  "services": [
    {
      "name": "Photos",
      "level": "operational",
      "label": "Operational",
      "uptime": "2 weeks",
      "uptime_pct": 99.98
    }
  ],
  "updated": "2026-08-16T20:00:00Z"
}
```

Fields: `uptime` (container uptime text), `response_ms` (HTTP endpoint latency), and `uptime_pct` (overall percentage from history DB) are included only when available.

## Part of the Harbor Monitoring Suite

Beacon is part of [Harbor](https://github.com/agent-cyanez/harbor) — a Docker monitoring suite for self-hosters:

| Tool | Purpose |
|------|---------|
| [Lookout](https://github.com/agent-cyanez/lookout) | Container lifecycle alerts |
| **Beacon** | Status page with uptime history |
| [Bosun](https://github.com/agent-cyanez/bosun) | Log pattern alerting |
| [Sextant](https://github.com/agent-cyanez/sextant) | TLS certificate monitoring |
| [Drift](https://github.com/agent-cyanez/drift) | Image update notifications |

## License

MIT
