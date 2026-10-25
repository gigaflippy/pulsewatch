# Pulsewatch

Distributed uptime and latency monitor. Probes HTTP endpoints on a schedule, stores results, serves uptime/latency stats over a REST API, and exposes Prometheus metrics. It is built to run entirely on local infrastructure (Docker, local Kubernetes) at zero cost.

> Status: **v0.1 core, Grafana, Kubernetes (kind), multi-region agents, Terraform, Argo CD GitOps, alert routing (Alertmanager, Telegram-ready), SLOs with error-budget burn-rate alerts, chaos experiments with meta-monitoring.** The original [roadmap](#roadmap) is complete; extras are listed under it.

**New here?** Read [docs/GUIDE.md](docs/GUIDE.md) first. It explains the whole project in plain language: what each part does, how a probe becomes an alert, how to run it, and what went wrong along the way.

## Quick start

```bash
cp targets.example.yaml targets.yaml   # edit to taste
docker compose up --build
```

- API: http://localhost:8000/docs
- Metrics: http://localhost:8000/metrics
- Prometheus: http://localhost:9090 (try `pulsewatch_up` or `pulsewatch:latency_p95_seconds`)
- Grafana: http://localhost:3000 (opens on the Pulsewatch dashboard; anonymous read-only, `admin`/`admin` to edit)
- Alertmanager: http://localhost:9093, and every notification it sends lands in the alert sink at http://localhost:9095/notifications

Without Docker:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp targets.example.yaml targets.yaml
pulsewatch
```

## Configuration

Targets live in a YAML file:

```yaml
targets:
  - name: my-api                # unique, [a-zA-Z0-9_.-]
    url: https://api.example.com/health
    interval_seconds: 30        # >= 1
    timeout_seconds: 5
    method: GET                 # GET or HEAD
    expected_status: [200]      # anything else counts as down
    slo_objective: 0.995        # optional: availability objective over 7d (see SLOs below)
```

Runtime settings come from environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `PULSEWATCH_TARGETS_FILE` | `targets.yaml` | Path to the targets file |
| `PULSEWATCH_DB_PATH` | `pulsewatch.db` | SQLite database path |
| `PULSEWATCH_REGION` | `local` | Region label attached to every result and metric |
| `PULSEWATCH_RETENTION_HOURS` | `72` | Results older than this are pruned hourly |
| `PULSEWATCH_HOST` / `PULSEWATCH_PORT` | `0.0.0.0` / `8000` | Bind address |
| `PULSEWATCH_SCHEDULER_ENABLED` | `true` | Set `false` to serve the API without probing |

## API

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness, with region and version |
| GET | `/targets` | Configured targets |
| GET | `/status?window=3600` | Per-target uptime %, p50/p95 latency and last result over the window (60s to 7d) |
| GET | `/targets/{name}/results?limit=50` | Most recent raw results |
| POST | `/targets/{name}/check` | Run one probe now |
| GET | `/metrics` | Prometheus exposition |

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `pulsewatch_up` | gauge | `target`, `region` |
| `pulsewatch_probe_latency_seconds` | histogram | `target`, `region` |
| `pulsewatch_probes_total` | counter | `target`, `region`, `result` (`up`/`down`); both start at 0 |
| `pulsewatch_slo_objective` | gauge | `target`, `region`: `slo_objective` from the targets file |

`deploy/prometheus/rules.yml` holds recording rules for 1h uptime ratio and p95 latency, plus these alerts:

| Alert | Fires when | Severity |
|---|---|---|
| `TargetDown` | the target is down from **every** region for 2m | page |
| `TargetDownInSomeRegions` | the target is down from some regions but up from others for 5m (regional or network-path issue) | warn |
| `TargetSlow` | p95 latency is above 2s for 10m | warn |
| `ErrorBudgetBurnFast` | the target burns its 7d error budget at 14.4× on both 1h and 5m windows for 5m (flapping, partial failures) | page |
| `ErrorBudgetBurnSlow` | 6× on 6h and 30m, or 1× on 3d and 6h, for 15m | warn |
| `AlertmanagerNotificationsFailing` | Alertmanager failed to deliver to a receiver (e.g. a wrong Telegram token) for 10m | warn |
| `ProbeRegionMissing` | a region that reported in the last hour has no series for 5m (agent gone, crashlooping or unscrapable) | warn |
| `ProbeAgentsAbsent` | no agent reports `pulsewatch_up` at all for 5m: uptime alerting is blind | page |

Requiring every region to agree is what keeps a single bad probe location from paging anyone. With one region, `TargetDown` behaves like a plain "down" alert.

The consensus has a blind spot that the two `Probe*` alerts close: it only counts the regions that report. A dead agent doesn't page by itself, it quietly shrinks the consensus, and if every agent dies there is no `pulsewatch_up` left, so `TargetDown` can never fire. `ProbeRegionMissing` warns when a region drops out (after an hour it counts as decommissioned and resolves), and `ProbeAgentsAbsent` pages when none is left. The rules have unit tests in `rules.test.yml`; run them with `promtool test rules deploy/prometheus/rules.test.yml`. CI runs them too.

### SLOs and error budgets

Every target has an availability objective (`slo_objective`, default **99.5%**) over a rolling **7-day** window. Prometheus keeps 8 days of data, one day more than the window, so the oldest part of the window is never already deleted. At 99.5% the budget is about 50 minutes of downtime a week.

- **SLI: up from at least one region.** The rules use the same consensus as `TargetDown`: `pulsewatch:target_up = max by (target) (pulsewatch_up)`. A broken probe location never spends budget, so it can't page through the SLO alerts either. That case is a `TargetDownInSomeRegions` warning.
- **Recording rules:** `pulsewatch:slo_error_ratio:{5m,30m,1h,6h,3d,7d}`, `pulsewatch:slo_burn_rate:{1h,6h}` and `pulsewatch:slo_error_budget_remaining:7d` (1 = untouched, 0 = spent, below 0 = SLO missed).
- **Multiwindow, multi-burn-rate alerts**, as in the Google SRE workbook. Each alert needs a long window, so enough budget has really been spent, and a short window, so the target is still failing now and the alert resolves soon after recovery. Burn rate 1 spends the budget in exactly 7 days; 14.4 spends it in about 12 hours.
- **What the burn alerts add:** `TargetDown` needs 2 minutes of continuous downtime, so a target that flaps or fails every third probe never trips it, but it still burns budget. `ErrorBudgetBurnFast` pages for that.
- **One page per outage:** a hard outage already pages as `TargetDown`. Fast burn waits 5m, longer than `TargetDown`'s 2m, and stays quiet for 10m after `TargetDown` last fired, because its 5m window keeps burning for a few minutes after recovery. In Alertmanager, `TargetDown` holds back `ErrorBudgetBurnSlow` for the same target, and so does `ErrorBudgetBurnFast`. Slow burn can still warn after a long outage has ended, because by then that outage really has spent much of the week's budget.
- **Tested:** `rules.test.yml` covers a flapping target (fast burn pages, `TargetDown` doesn't), a hard outage including the minutes after recovery (only `TargetDown`), one bad region (no budget spent), a steady 5% failure rate (slow burn warns, no page) and per-target objectives. The CI stack job checks that the always-down fixture target ends up with a negative budget. The fast-burn suppression during an outage is proven by `rules.test.yml`; the CI check for it runs too early in the outage to fail on its own.

## Alert routing

Prometheus sends alerts to **Alertmanager** (`deploy/alertmanager/`, same files for compose and Kubernetes), which routes them by severity:

```
Prometheus rules ──► Alertmanager ──┬─ severity=page ─► receiver "page" (group_wait 10s, repeat 1h)
                                    ├─ severity=warn ─► receiver "warn" (group_wait 30s, repeat 12h)
                                    └─ anything else ─► receiver "default"
   each receiver ─► alert sink (always, no account needed)
                 └► Telegram (page + warn, only if you add a bot token locally)
```

- **Grouping:** by `alertname` and `target`. One notification per broken target, and the regions of one target are batched into it.
- **Inhibition:** while `TargetDown` fires for a target (down from every region, paged), `TargetDownInSomeRegions`, `TargetSlow` and `ErrorBudgetBurnSlow` for the same target are held back. `ErrorBudgetBurnFast` holds back `ErrorBudgetBurnSlow` too, and `ProbeAgentsAbsent` holds back every `ProbeRegionMissing`. One page, not several messages about the same outage.
- **Alert sink:** `pulsewatch-alertsink` is a tiny webhook inbox from the Pulsewatch image (standard library only). It logs one line per alert and serves `GET /notifications?receiver=page`. It makes routing visible, and testable, without any credential.
- **Delivery is watched:** Prometheus scrapes Alertmanager, and `AlertmanagerNotificationsFailing` warns when a receiver keeps failing. A revoked token doesn't go unnoticed until the next real outage. If Alertmanager itself is down, nothing can notify you: that needs an outside dead man's switch (a heartbeat alert to an external service), which this local stack does not have.

### Telegram

Telegram is switched on only by credentials that exist on your machine. Nothing secret is in the repo. `start.sh` writes the token to a 0600 file (`bot_token_file`) and removes it from the environment of the Alertmanager process, so it is never in a config file. The container's own environment still holds it (`docker inspect`, `kubectl exec ... env`), so anyone with Docker or cluster access can read it. That is acceptable for a local stack.

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `"chat":{"id":...}`.
3. Turn it on:

```bash
# docker compose: put both values in .env (git-ignored; see .env.example), then
docker compose up -d alertmanager

# Kubernetes: creates the optional Secret "alertmanager-telegram" from your shell and restarts Alertmanager
read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN   # paste the token; it is not echoed or kept in shell history
make alerts-telegram TELEGRAM_CHAT_ID=123456789
make alerts-test SEVERITY=page    # synthetic alert; arrives in Telegram and the sink within ~10s
make alerts-inbox                 # what the sink received
make alerts-telegram-off          # back to the sink only
```

`start.sh` picks the config at container start: `alertmanager.yml` with no credentials, or `alertmanager-telegram.yml` (the same file plus `telegram_configs`; a test keeps them in sync) with both set. One of the two without the other is an error, not a silent fallback. Alertmanager prints the Telegram API URL, token included, when a send fails, so treat its logs as sensitive once Telegram is on.

**CI** tests all of this without a token: `amtool check-config` on both configs, `amtool config routes test` for every severity, inhibition with synthetic alerts, and a real outage end to end in compose (an always-down target → `TargetDown` → Alertmanager → `page` in the sink). The kind job sends a test alert through the in-cluster Alertmanager and switches Telegram on and off with a fake token.

## Grafana dashboard

`docker compose up` also starts Grafana with the Prometheus datasource and the **Pulsewatch overview** dashboard provisioned from `deploy/grafana/`. Nothing to click through.

| Panel | Query basis |
|---|---|
| Targets up / down, current status per target | `pulsewatch_up` |
| Uptime over the selected time range | `avg_over_time(pulsewatch_up[$__range])` |
| Up/down history (state timeline) | `pulsewatch_up` |
| Latency p95 / p50 | `pulsewatch:latency_p95_seconds`, `histogram_quantile(0.5, …)` |
| Uptime last 1h | `pulsewatch:uptime_ratio_1h` |
| Failed probes per minute | `rate(pulsewatch_probes_total{result="down"}[5m])` |
| Firing alerts | `ALERTS{alertname=~"Target.*\|ErrorBudget.*\|Probe.*"}` |
| Error budget left (7d), availability vs objective, burn rate 1h/6h | `pulsewatch:slo_*` recording rules (all regions combined, so the `Region` filter doesn't apply) |

`Region` and `Target` dropdowns filter every panel, so the same dashboard works once several regions report into one Prometheus.

The dashboard is provisioned read-only. To change it, edit it in the UI, export the JSON, and replace `deploy/grafana/dashboards/pulsewatch.json`. `tests/test_deploy_config.py` fails if a panel queries a metric that the code or the recording rules don't produce, so renames can't silently blank a panel.

Compose publishes every port on `127.0.0.1` only, because Alertmanager, Prometheus and the alert sink have no login. If you change that, set `GRAFANA_ADMIN_PASSWORD` too.

## Kubernetes (kind / k3d)

The same stack runs on a local cluster: Pulsewatch, Prometheus, Alertmanager (with the alert sink) and Grafana in the `pulsewatch` namespace. It's built with Kustomize, so you only need `kubectl` and nothing has to be installed in the cluster.

```bash
make kind-up        # create kind cluster, build + load image, apply, wait for rollouts
make k8s-forward    # API :8000, Prometheus :9090, Grafana :3000
make kind-down
```

Needs `docker`, [`kind`](https://kind.sigs.k8s.io/) and `kubectl`. The image is built locally and loaded into the cluster with `kind load`, so no registry and no account are needed. For **k3d**, replace the load step with `k3d image import pulsewatch:dev -c <cluster>` and run `kubectl apply -k deploy/k8s/overlays/kind`.

```
deploy/k8s/
  base/                 Pulsewatch Deployment, Service, PVC, targets ConfigMap (targets.yaml)
  monitoring/           Prometheus, Alertmanager, alert sink, Grafana Deployments/Services
  overlays/kind/        single region: everything in one namespace, region=kind-local
  overlays/multi-region/ one namespace per region (eu-sim, us-sim) + shared monitoring namespace
  kind-config.yaml
deploy/argocd/
  install/              Argo CD, pinned release (kubectl apply --server-side -k)
  bootstrap/            AppProject + ApplicationSet: one Application per overlays/multi-region/* dir
deploy/prometheus/      rules.yml shared by compose and k8s; prometheus.yml (compose) / prometheus-k8s.yml (k8s SD)
deploy/grafana/         shared by compose and k8s
deploy/alertmanager/    routing config, Telegram variant, start.sh; shared by compose and k8s
```

- **One config, two runtimes.** Compose and Kubernetes share the alert rules, Grafana provisioning and the dashboard. The only difference is scraping: compose scrapes `pulsewatch:8000`, while in-cluster Prometheus uses Kubernetes service discovery (`prometheus-k8s.yml`, with a read-only ClusterRole) to find every Pulsewatch pod in any namespace.
- **Targets** for the cluster live in `deploy/k8s/base/targets.yaml`. The ConfigMap name carries a content hash, so editing the file and re-applying rolls out the pod automatically.
- **Storage:** SQLite on a 1Gi PVC with `replicas: 1` and `strategy: Recreate`, so there is only ever one writer. You scale Pulsewatch by adding regions, not replicas (roadmap item 4).
- **Hardening:** every container runs non-root with a read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp, no service-account token and memory limits.
- **CI:** `k8s-manifests` renders the overlay and validates it with `kubeconform -strict` against Kubernetes 1.30. `kind` deploys the overlay to a real kind cluster and checks the API, the Prometheus scrape, the Grafana dashboard and datasource, and that results survive a pod restart.

### Multi-region

```bash
make kind-up OVERLAY=deploy/k8s/overlays/multi-region
make k8s-forward API_NS=pulsewatch-eu
```

Each region is a Pulsewatch agent in its own namespace (`pulsewatch-eu`, `pulsewatch-us`) with its own `PULSEWATCH_REGION`, probing the same targets. One Prometheus in `pulsewatch` discovers all agents through the Kubernetes API. Adding a region is a copy of `region-us/` with a new name, and Prometheus needs no changes. The Grafana `Region` dropdown and the alerts above use the `region` label.

These "regions" all share one machine, so they show the multi-region design (discovery, labelling, consensus alerting) rather than real geographic diversity. Real regions are the same manifests applied to clusters in different places, with Prometheus federation or remote-write on top.

### Chaos experiments

Three failure modes, each with a make target and an expected alert. They run against the multi-region overlay deployed with `make kind-up` or Terraform. Under Argo CD, self-heal reverts them within a minute.

| Experiment | What it does | Expected |
|---|---|---|
| `make chaos-partition` | `pulsewatch-us` resolves `example.com` to `127.0.0.1` (a `hostAliases` entry), so only that region loses its path to the target | `TargetDownInSomeRegions` (warn) after 5m. No `TargetDown`, no page, no error budget spent |
| `make chaos-kill-region` | scales the `pulsewatch-us` agent to zero | `ProbeRegionMissing` (warn); `eu` alone keeps alerting |
| `make chaos-kill-region CHAOS_NS=pulsewatch-eu` (after the above) | the last agent goes too | `ProbeAgentsAbsent` (page); the region warnings are inhibited behind it |

`make chaos-heal` (with the same `CHAOS_NS`) undoes the partition and scales the agent back up. `make chaos-alerts` lists what Prometheus has pending and firing, and `make alerts-inbox` shows what was delivered. Change the region with `CHAOS_NS`, the host with `CHAOS_HOST`.

The partition uses `/etc/hosts` instead of a NetworkPolicy, so it works the same on any CNI (kind's default included) and touches nothing outside one Deployment.

**CI** (`chaos` job) runs all three on a fresh kind cluster and checks each one end to end with `scripts/chaos_check.py`. For every experiment it checks that the expected alert fires in Prometheus, reaches the right Alertmanager receiver in the alert sink, and that the alerts which must stay quiet do: no `TargetDown` and no budget burn during the partition, and the region warnings show as inhibited in Alertmanager's API while `ProbeAgentsAbsent` pages. Then it heals everything and waits for the meta alerts to clear.

### Terraform

The whole local platform as code: the kind cluster, the Pulsewatch image built and loaded into it, and the overlay applied (monitoring plus probe regions).

```bash
cd infra/terraform
terraform init
terraform apply                        # overlay=multi-region by default
terraform apply -var overlay=kind      # single region instead
export KUBECONFIG=$(terraform output -raw kubeconfig_path)
terraform destroy
```

- **Providers:** `tehcyx/kind` creates the cluster and `kbst/kustomization` applies the same Kustomize overlays that `kubectl apply -k` uses. There's one source of manifests, applied in priority order (namespaces and RBAC, then workloads).
- **Image:** rebuilt and reloaded only when `src/`, the `Dockerfile` or `pyproject.toml` change (content hash), so a second `terraform apply` changes nothing.
- **Node image** is pinned (`node_image`, kind v0.31's `kindest/node:v1.35.0`) so the cluster always matches the `kind` CLI that loads the image. Left to the provider default, a provider release moved it to a node `kind load` can't talk to.
- **State** stays local and is git-ignored, along with the generated `kubeconfig`. No backend or account is needed.
- **Provider lockfile.** `.terraform.lock.hcl` is committed and pins exact provider versions with checksums for Linux, macOS (Intel and Apple silicon) and Windows. Every `terraform init` gets the same providers. Without it, `init` picked up a new `tehcyx/kind` release overnight and broke `kind load` with no change in this repo. `make tf-upgrade` moves to the newest versions the constraints allow; review the diff and commit it. `make tf-lock` only adds missing platform checksums. CI's `terraform-lock` job fails if the committed file is missing a provider or a platform and prints the regenerated file. The `terraform` job runs `init -lockfile=readonly`.
- **Needs** `terraform`, `docker` and `kind` (for `kind load`).
- **CI** (`terraform` job): `fmt -check`, `validate`, a real `apply` to a fresh kind cluster, a health check that Prometheus sees both regions, a `plan -detailed-exitcode` that must show no changes, then `destroy`.
- Also runs unchanged on **OpenTofu** (`tofu init && tofu apply`). OpenTofu resolves providers from its own registry, so its first `init` adds `registry.opentofu.org` entries to the lockfile. Don't commit those unless you switch for good.

### GitOps with Argo CD

Argo CD runs in the cluster and deploys the multi-region stack **from Git**. A commit to the default branch is live within about a minute, and anything changed by hand in the cluster is put back.

```bash
make argocd-up                        # kind cluster + image, Argo CD, then Argo CD syncs from GitHub
make argocd-status                    # Applications, sync/health, deployed commit
make argocd-ui                        # http://localhost:8080, prints the admin password
make argocd-up ARGOCD_REVISION=my-branch   # deploy a branch or commit instead of HEAD
```

```
GitHub (this repo) ──poll 60s──► argocd-repo-server ──► ApplicationSet "pulsewatch"
                                                          │ git directory generator:
                                                          │ deploy/k8s/overlays/multi-region/*
                         ┌────────────────────────────────┼─────────────────────────┐
                         ▼                                ▼                         ▼
              pulsewatch-monitoring          pulsewatch-region-eu       pulsewatch-region-us
              (Prometheus, Grafana)          (agent, eu-sim)            (agent, us-sim)
              automated sync · prune · self-heal · AppProject "pulsewatch"
```

- **Same manifests, third way to apply them.** Argo CD runs the same Kustomize overlays as `kubectl apply -k` and Terraform. Nothing is duplicated for GitOps.
- **ApplicationSet, not hand-written Applications.** Each directory under `overlays/multi-region/` becomes an Application. To add a region, copy `region-us/`, rename it, list it in the overlay and push. Argo CD creates the new Application on its own.
- **Least privilege.** The `pulsewatch` AppProject allows one source repo, `pulsewatch*` namespaces and only the cluster-scoped kinds the overlays contain (Namespace, ClusterRole, ClusterRoleBinding). `tests/test_argocd.py` fails if a manifest needs more or the project allows more than it needs.
- **Image.** Argo CD syncs manifests; the image is still built locally and side-loaded with `kind load` (no registry, no account). The repo is public, so Argo CD needs no Git credentials.
- **Local tweaks** (`deploy/argocd/install`): plain HTTP for port-forwarding, 60s Git polling instead of ~3m, and no Dex since there's no SSO.
- **CI** (`argocd` job): installs Argo CD on kind, points the ApplicationSet at the **commit under test**, waits until every Application is Synced and Healthy at that exact SHA, checks Prometheus sees both regions, then deletes a Deployment by hand and checks Argo CD puts it back.

## Design

```
targets.yaml ─► Scheduler ──(one asyncio loop per target, jittered start)──► probe()
                    │                                                         │
                    │                                         ProbeResult (up, status, latency, error)
                    ▼                                                         │
               Storage (SQLite) ◄───────────────────────┬────────────────────┘
                    │                                    ▼
             FastAPI /status, /results          Metrics ─► /metrics ─► Prometheus
```

- Probes never raise: timeouts and connection errors become `up=false` results with an `error` string.
- Latency is measured only for responses that came back; failed connections record no latency, so failures don't skew the percentiles.
- SQLite keeps v0.1 dependency-free. The `Storage` class is the seam for swapping in Postgres later.

## Development

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest
```

CI (GitHub Actions) runs lint, tests on Python 3.11 to 3.13, a Docker build with a smoke test, and a full-stack job that runs `promtool check config`, brings up the compose stack, and checks that Prometheus is scraping Pulsewatch and Alertmanager, Grafana serves the provisioned dashboard and a healthy datasource, and an alert travels from a failing probe to the alert sink (see [Alert routing](#alert-routing)).

## How this was built

Pulsewatch is built by Georgi Georgiev with [Claude](https://claude.com/claude-code) as a pair programmer.

- **Georgi** chose the project, the stack and the constraints (zero cost, local infrastructure only), and made the design calls recorded in [DECISIONS.md](DECISIONS.md): what to build (D-001), Terraform over OpenTofu (D-002) and the alert-routing approach (D-003). He reviews and merges every pull request.
- **Claude** wrote most of the code, manifests and CI from that direction. Most of it was done in scheduled overnight runs, each one delivered as a pull request with a description of what changed, why, and how to test it.
- **Every change** passes the full CI suite before merge: lint, unit tests, rule tests, and real deployments to throwaway kind clusters.

The pull request history and DECISIONS.md show the work step by step.

## Roadmap

1. ~~**v0.1:** core probe service, REST API, Prometheus metrics, Docker, CI~~ ✅
2. ~~Grafana dashboard provisioned in compose~~ ✅
3. ~~Kubernetes manifests (Kustomize) running on kind/k3d~~ ✅ (a Helm chart can come later if it's needed; Kustomize is already enough for ArgoCD)
4. ~~Multi-region: probe agents in separate namespaces with distinct `region` labels, one Prometheus discovering all of them, consensus alerting~~ ✅
5. ~~Terraform for the local platform (kind cluster, image, monitoring stack, regions)~~ ✅
6. ~~ArgoCD GitOps deploys (ApplicationSet over the multi-region overlay, automated sync with self-heal, tested on kind in CI)~~ ✅
7. ~~Alert routing (Alertmanager with page/warn routing and inhibition, local alert sink, Telegram from a locally created Secret, tested end to end in CI)~~ ✅

Beyond the original roadmap:

- ~~Committed Terraform provider lockfile, enforced in CI~~ ✅
- ~~Per-target availability SLOs: error-budget recording rules, multiwindow burn-rate alerts, dashboard row~~ ✅
- ~~Chaos experiments (regional partition, lost region, all agents lost), CI-tested on kind, plus meta-monitoring alerts for lost probe agents~~ ✅
- ~~Project guide with architecture diagrams ([docs/GUIDE.md](docs/GUIDE.md))~~ ✅
- Next candidates: k6 load test of the API, a Helm chart.
