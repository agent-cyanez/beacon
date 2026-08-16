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
- HTTP endpoint monitoring (check if external URLs are reachable)
- Optional response time display
- Responsive dark/light theme (follows system preference)
- Filter which containers to display
- Custom display names for containers
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
    environment:
      - SITE_TITLE=My Services
      - SERVICES=immich_server:Photos,forgejo:Git,ntfy:Notifications
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

## License

MIT
