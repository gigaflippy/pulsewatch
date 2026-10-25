# Local Kubernetes workflow for Pulsewatch. Needs: docker, kind, kubectl.
CLUSTER ?= pulsewatch
NS      ?= pulsewatch
API_NS  ?= $(NS)   # multi-region: API_NS=pulsewatch-eu (or -us)
IMAGE   ?= pulsewatch:dev
OVERLAY ?= deploy/k8s/overlays/kind
# GitOps: what Argo CD deploys from. HEAD = the repo's default branch.
GITOPS_REPO     ?= https://github.com/gigaflippy/pulsewatch.git
ARGOCD_REVISION ?= HEAD
# Platforms recorded in infra/terraform/.terraform.lock.hcl, so `terraform init` verifies
# provider checksums on Linux, macOS (Intel + Apple silicon) and Windows alike.
TF_LOCK_PLATFORMS ?= linux_amd64 linux_arm64 darwin_amd64 darwin_arm64 windows_amd64
# Chaos experiments (multi-region overlay): which region to break, and which target host
# that region loses its network path to.
CHAOS_NS   ?= pulsewatch-us
CHAOS_HOST ?= example.com

.PHONY: help kind-up kind-down k8s-apply k8s-wait k8s-validate k8s-forward k8s-status \
	argocd-up argocd-install argocd-bootstrap argocd-wait argocd-status argocd-ui \
	alerts-test alerts-inbox alerts-telegram alerts-telegram-off tf-lock tf-upgrade \
	chaos-partition chaos-kill-region chaos-heal chaos-alerts

help:
	@echo "make kind-up       create kind cluster, build + load image, deploy, wait for rollout"
	@echo "                   multi-region: make kind-up OVERLAY=deploy/k8s/overlays/multi-region"
	@echo "make k8s-forward   port-forward API :8000, Prometheus :9090, Grafana :3000, Alertmanager :9093, sink :9095"
	@echo "make k8s-status    show pods/services in the $(NS) namespace"
	@echo "make k8s-validate  render manifests and schema-check them (kubectl + kubeconform)"
	@echo "make kind-down     delete the kind cluster"
	@echo ""
	@echo "make argocd-up     GitOps: kind cluster + image, Argo CD, then Argo CD deploys the"
	@echo "                   multi-region stack from Git (ARGOCD_REVISION=<branch|sha>, default HEAD)"
	@echo "make argocd-status Argo CD applications with sync/health status"
	@echo "make argocd-ui     port-forward the Argo CD UI to :8080 and print the admin password"
	@echo ""
	@echo "make alerts-test   fire a synthetic warn alert through Alertmanager (SEVERITY=page to page)"
	@echo "make alerts-inbox  notifications the local alert sink has received"
	@echo "make alerts-telegram TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...  also route to Telegram"
	@echo "make alerts-telegram-off  back to the alert sink only"
	@echo ""
	@echo "make chaos-partition   cut region $(CHAOS_NS) off from $(CHAOS_HOST) (expect TargetDownInSomeRegions)"
	@echo "make chaos-kill-region scale the $(CHAOS_NS) agent to zero (expect ProbeRegionMissing)"
	@echo "make chaos-heal        undo both; make chaos-alerts lists what Prometheus has firing"
	@echo ""
	@echo "make tf-lock       add any missing provider checksums to infra/terraform/.terraform.lock.hcl"
	@echo "make tf-upgrade    move the lockfile to the newest provider versions the constraints allow"

kind-up:
	@kind get clusters | grep -qx '$(CLUSTER)' || kind create cluster --config deploy/k8s/kind-config.yaml --name $(CLUSTER)
	docker build -t $(IMAGE) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)
	$(MAKE) k8s-apply k8s-wait
	# Same tag every build: restart every Pulsewatch agent so the new image is picked up.
	kubectl get deploy -A -l app.kubernetes.io/name=pulsewatch \
	  -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' | \
	  xargs -I{} kubectl -n {} rollout restart deployment/pulsewatch
	$(MAKE) k8s-wait

k8s-apply:
	kubectl apply -k $(OVERLAY)

k8s-wait:
	@kubectl get deploy -A -l app.kubernetes.io/part-of=pulsewatch \
	  -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' | \
	while IFS=/ read -r ns name; do \
	  kubectl -n $$ns rollout status deployment/$$name --timeout=180s || exit 1; \
	done

k8s-forward:
	@echo "API http://localhost:8000/docs  Prometheus http://localhost:9090  Grafana http://localhost:3000"
	@echo "Alertmanager http://localhost:9093  alert sink http://localhost:9095/notifications  (Ctrl-C to stop)"
	@trap 'kill 0' INT TERM; \
	kubectl -n $(API_NS) port-forward svc/pulsewatch 8000:8000 & \
	kubectl -n $(NS) port-forward svc/prometheus 9090:9090 & \
	kubectl -n $(NS) port-forward svc/grafana 3000:3000 & \
	kubectl -n $(NS) port-forward svc/alertmanager 9093:9093 & \
	kubectl -n $(NS) port-forward svc/alert-sink 9095:9095 & \
	wait

k8s-status:
	kubectl get pods,svc,pvc -A -l app.kubernetes.io/part-of=pulsewatch

k8s-validate:
	kubectl kustomize $(OVERLAY) | kubeconform -strict -summary -
	promtool test rules deploy/prometheus/rules.test.yml
	amtool check-config deploy/alertmanager/alertmanager.yml

kind-down:
	kind delete cluster --name $(CLUSTER)

# --- GitOps with Argo CD -----------------------------------------------------
# Argo CD pulls the manifests from Git; only the image is side-loaded (no registry).

argocd-up:
	@kind get clusters | grep -qx '$(CLUSTER)' || kind create cluster --config deploy/k8s/kind-config.yaml --name $(CLUSTER)
	docker build -t $(IMAGE) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)
	$(MAKE) argocd-install argocd-bootstrap argocd-wait

argocd-install:
	kubectl apply --server-side --force-conflicts -k deploy/argocd/install
	kubectl wait --for=condition=Established --timeout=60s \
	  crd/applications.argoproj.io crd/applicationsets.argoproj.io crd/appprojects.argoproj.io
	kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=300s
	@for d in argocd-repo-server argocd-applicationset-controller argocd-server argocd-redis; do \
	  kubectl -n argocd rollout status deployment/$$d --timeout=300s || exit 1; \
	done

argocd-bootstrap:
	kubectl kustomize deploy/argocd/bootstrap | \
	  sed -e 's|https://github.com/gigaflippy/pulsewatch.git|$(GITOPS_REPO)|' \
	      -e 's|revision: HEAD|revision: $(ARGOCD_REVISION)|' \
	      -e 's|targetRevision: HEAD|targetRevision: $(ARGOCD_REVISION)|' | \
	  kubectl apply -f -

# One Application per overlays/multi-region/* directory, all Synced + Healthy (Argo CD
# only reports Healthy once the Deployments have rolled out).
argocd-wait:
	@want=$$(ls -d deploy/k8s/overlays/multi-region/*/ | wc -l); \
	for i in $$(seq 1 90); do \
	  s=$$(kubectl -n argocd get applications -l app.kubernetes.io/part-of=pulsewatch \
	    -o jsonpath='{range .items[*]}{.metadata.name}={.status.sync.status}/{.status.health.status}{"\n"}{end}'); \
	  if [ "$$(echo "$$s" | grep -c '=Synced/Healthy$$')" -eq "$$want" ]; then echo "$$s"; exit 0; fi; \
	  sleep 5; \
	done; echo "$$s"; exit 1

argocd-status:
	kubectl -n argocd get applications -l app.kubernetes.io/part-of=pulsewatch \
	  -o custom-columns=APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision

argocd-ui:
	@echo "Argo CD http://localhost:8080  user admin  password $$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
	kubectl -n argocd port-forward svc/argocd-server 8080:80

# --- Alert routing -----------------------------------------------------------
# Alertmanager always delivers to the in-cluster alert sink. Telegram is added only when
# the Secret below exists; it is created from your shell and never written to the repo.
SEVERITY ?= warn
# Prints one line per alert the sink received: time, receiver, firing/resolved, alertname, target.
define ALERTS_INBOX_PY
import json, urllib.request as u
for n in json.load(u.urlopen("http://127.0.0.1:9095/notifications")):
    for a in n["payload"].get("alerts", []):
        print(n["received_at"], n["receiver"], a["status"], a["labels"].get("alertname"), a["labels"].get("target", "-"))
endef
export ALERTS_INBOX_PY

alerts-test:
	kubectl -n $(NS) exec deploy/alertmanager -- amtool --alertmanager.url=http://localhost:9093 \
	  alert add PulsewatchTestAlert severity=$(SEVERITY) target=alerts-test \
	  --annotation='summary="Test alert from make alerts-test"'
	@echo "Sent. Delivered after group_wait (10s page, 30s warn): make alerts-inbox"

alerts-inbox:
	@kubectl -n $(NS) exec deploy/alert-sink -- python -c "$$ALERTS_INBOX_PY"

# Values come from the environment (or make arguments) and reach kubectl on stdin, so
# kubectl's own command line never holds them and nothing is written to this repo.
# Make arguments do show in your shell history and in `ps` while make runs. Export the
# variables first (read -rs TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN) to avoid that.
alerts-telegram:
	@test -n "$$TELEGRAM_BOT_TOKEN" -a -n "$$TELEGRAM_CHAT_ID" || \
	  { echo "usage: make alerts-telegram TELEGRAM_BOT_TOKEN=<from @BotFather> TELEGRAM_CHAT_ID=<your chat id>"; exit 1; }
	@printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: alertmanager-telegram\n  namespace: %s\ntype: Opaque\nstringData:\n  bot_token: "%s"\n  chat_id: "%s"\n' \
	  '$(NS)' "$$TELEGRAM_BOT_TOKEN" "$$TELEGRAM_CHAT_ID" | kubectl apply -f -
	kubectl -n $(NS) rollout restart deployment/alertmanager
	kubectl -n $(NS) rollout status deployment/alertmanager --timeout=120s
	@kubectl -n $(NS) logs deploy/alertmanager | grep 'start.sh:'

alerts-telegram-off:
	kubectl -n $(NS) delete secret alertmanager-telegram --ignore-not-found
	kubectl -n $(NS) rollout restart deployment/alertmanager
	kubectl -n $(NS) rollout status deployment/alertmanager --timeout=120s

# Provider lockfile. CI runs tf-lock and fails if it changes the committed file.
tf-lock:
	cd infra/terraform && terraform providers lock $(addprefix -platform=,$(TF_LOCK_PLATFORMS))

tf-upgrade:
	cd infra/terraform && terraform init -backend=false -upgrade -input=false >/dev/null
	$(MAKE) tf-lock

# Chaos experiments. Run against a kubectl/Terraform deploy of the multi-region overlay:
# under Argo CD, self-heal reverts these patches within a minute (which is its own demo).
#
# Partition: the region's agent resolves CHAOS_HOST to 127.0.0.1 (a hostAliases entry in
# /etc/hosts), so its probes of that target fail with "connection refused" while every
# other region still reaches it. Works on any CNI, no NetworkPolicy support needed.
chaos-partition:
	kubectl -n $(CHAOS_NS) patch deployment/pulsewatch --type=strategic -p \
	  '{"spec":{"template":{"spec":{"hostAliases":[{"ip":"127.0.0.1","hostnames":["$(CHAOS_HOST)"]}]}}}}'
	kubectl -n $(CHAOS_NS) rollout status deployment/pulsewatch --timeout=180s
	@echo "$(CHAOS_NS) can no longer reach $(CHAOS_HOST). Expect TargetDownInSomeRegions (warn) after 5m, no TargetDown."

chaos-kill-region:
	kubectl -n $(CHAOS_NS) scale deployment/pulsewatch --replicas=0
	@for i in $$(seq 1 60); do \
	  pods=$$(kubectl -n $(CHAOS_NS) get pods -l app.kubernetes.io/name=pulsewatch -o name) || exit 1; \
	  test -z "$$pods" && exit 0; sleep 2; \
	done; echo "pods in $(CHAOS_NS) did not terminate"; exit 1
	@echo "$(CHAOS_NS) agent is gone. Expect ProbeRegionMissing (warn) after 5m; ProbeAgentsAbsent (page) if it was the last one."

chaos-heal:
	-kubectl -n $(CHAOS_NS) patch deployment/pulsewatch --type=json \
	  -p '[{"op":"remove","path":"/spec/template/spec/hostAliases"}]' 2>/dev/null
	kubectl -n $(CHAOS_NS) scale deployment/pulsewatch --replicas=1
	kubectl -n $(CHAOS_NS) rollout status deployment/pulsewatch --timeout=180s

# Firing and pending alerts, straight from Prometheus (no port-forward needed).
chaos-alerts:
	@kubectl -n $(NS) exec deploy/prometheus -- wget -qO- 'http://localhost:9090/api/v1/alerts' | \
	  python3 -c 'import json,sys; [print(a["state"], a["labels"].get("alertname"), a["labels"].get("target", "-"), a["labels"].get("region", "-")) for a in json.load(sys.stdin)["data"]["alerts"]]'
