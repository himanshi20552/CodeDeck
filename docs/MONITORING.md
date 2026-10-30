# Monitoring

## Prometheus

FastAPI exposes `/metrics` using `prometheus-client`. HTTP requests are counted after completion, excluding metric scrapes. Labels use method, route template and status code only; repository URLs, IDs, prompts, source paths and exception text are not labels. Histograms cover HTTP duration, bounded operation duration, and LLM duration. Counters cover HTTP requests, operations by a fixed operation/outcome set, and LLM requests by a bounded provider/outcome set. Process metrics come from the Python client's default collector where the runtime supports them.

`/health/live` checks the process. `/health/ready` checks the critical database dependency and returns 503 when it cannot execute `SELECT 1`. Optional observability status is available through `/api/system/status` and `/api/system/integrations`.

## Grafana local setup

Copy `.env.example` to `.env` and configure a local Grafana password, then run:

```powershell
docker compose --profile observability up --build -d
```

Grafana is at `http://localhost:3001` (or `GRAFANA_PORT`) and Prometheus at `http://localhost:9090` (or `PROMETHEUS_PORT`), bound to loopback. The backend's host port is also loopback-only (`BACKEND_PORT`, default 8000); Prometheus reaches `/metrics` on the private Compose network, while the frontend Nginx proxy does not forward the metrics path. Sign in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from `.env`. Prometheus scrape configuration, Grafana datasource, dashboard provider and CodeDeck dashboard are version controlled under `monitoring/`. Data persists in named Docker volumes. `docker compose --profile observability down` preserves volumes; remove them manually only when you intentionally want to erase local monitoring history.

The dashboard uses actual application metric names: HTTP rate/errors/p95, process memory, operation outcomes and p95, LLM request/latency, active operations and Prometheus scrape availability. Docker build, Kubernetes rollout and smoke-test panels populate only after those call paths are instrumented and executed. They are not infrastructure-level CPU/memory metrics.

## Infrastructure limits

This setup scrapes CodeDeck application metrics only. It does not install node-exporter, cAdvisor or kube-state-metrics, so it cannot report host CPU, Docker container resource use, or Kubernetes node/pod state. Those should be added only for a reachable cluster with a reviewed permissions model. Prometheus and Grafana are local development services, not secured for public exposure.
