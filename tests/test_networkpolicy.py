"""NetworkPolicy: default-deny everywhere, and exactly the intended connections allowed.

A small evaluator applies the NetworkPolicy rules (matchLabels selectors, ports, ipBlock)
to the pods each overlay deploys and checks the full connection matrix. The kind jobs
in CI prove the same on a real cluster (scripts/netpol_check.py); these tests catch a
policy that is too loose or too tight before anything is deployed, with no tooling.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "deploy" / "k8s"
KIND = K8S / "overlays" / "kind"
MULTI = K8S / "overlays" / "multi-region"
LEAF_OVERLAYS = [KIND, *sorted(p for p in MULTI.iterdir() if p.is_dir())]

POD_IP = "10.244.0.10"  # kind's pod network
PUBLIC_IP = "93.184.215.14"  # any public address (a probe target, api.telegram.org, ...)


def _render(directory: Path) -> list[dict]:
    """Enough of `kustomize build` for these overlays: resources, components, namespace."""
    k = yaml.safe_load((directory / "kustomization.yaml").read_text())
    docs: list[dict] = []
    for res in [*k.get("resources", []), *k.get("components", [])]:
        p = (directory / res).resolve()
        if p.is_dir():
            docs += _render(p)
        else:
            docs += [d for d in yaml.safe_load_all(p.read_text()) if d]
    if ns := k.get("namespace"):
        for d in docs:
            if d["kind"] not in {"Namespace", "ClusterRole", "ClusterRoleBinding"}:
                d.setdefault("metadata", {})["namespace"] = ns
    return docs


def _render_overlay(path: Path) -> list[dict]:
    k = yaml.safe_load((path / "kustomization.yaml").read_text())
    if "namespace" in k:
        return _render(path)
    return [d for sub in k["resources"] for d in _render((path / sub).resolve())]


@dataclass(frozen=True)
class Pod:
    namespace: str
    labels: tuple[tuple[str, str], ...]
    ports: tuple[int, ...] = ()
    ip: str = POD_IP

    @property
    def name(self) -> str:
        return dict(self.labels).get("app.kubernetes.io/name", "?")


@dataclass
class Cluster:
    pods: list[Pod]
    policies: list[dict]
    namespaces: dict[str, dict[str, str]] = field(default_factory=dict)

    def ns_labels(self, ns: str) -> dict[str, str]:
        return {**self.namespaces.get(ns, {}), "kubernetes.io/metadata.name": ns}


def _cluster(overlay: Path) -> Cluster:
    docs = _render_overlay(overlay)
    pods = []
    for d in docs:
        if d["kind"] == "Deployment":
            tpl = d["spec"]["template"]
            ports = tuple(p["containerPort"] for c in tpl["spec"]["containers"] for p in c["ports"])
            labels = tuple(sorted(tpl["metadata"]["labels"].items()))
            pods.append(Pod(d["metadata"]["namespace"], labels, ports))
    pods.append(Pod("kube-system", (("k8s-app", "kube-dns"),), (53,)))
    namespaces = {
        d["metadata"]["name"]: d["metadata"].get("labels", {})
        for d in docs
        if d["kind"] == "Namespace"
    }
    policies = [d for d in docs if d["kind"] == "NetworkPolicy"]
    return Cluster(pods, policies, namespaces)


def _match(selector: dict, labels: dict[str, str]) -> bool:
    assert set(selector) <= {"matchLabels"}, "the evaluator only understands matchLabels"
    return all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())


def _selects(policy: dict, pod: Pod) -> bool:
    return policy["metadata"]["namespace"] == pod.namespace and _match(
        policy["spec"]["podSelector"], dict(pod.labels)
    )


def _peer_matches(c: Cluster, peer: dict, policy_ns: str, other: Pod | str) -> bool:
    if "ipBlock" in peer:
        ip = ipaddress.ip_address(other.ip if isinstance(other, Pod) else other)
        block = peer["ipBlock"]
        net = ipaddress.ip_network(block["cidr"])
        if ip.version != net.version or ip not in net:
            return False
        return not any(ip in ipaddress.ip_network(e) for e in block.get("except", []))
    if not isinstance(other, Pod):
        return False  # selectors only ever match pods
    if "namespaceSelector" in peer:
        if not _match(peer["namespaceSelector"], c.ns_labels(other.namespace)):
            return False
    elif other.namespace != policy_ns:
        return False
    return _match(peer.get("podSelector", {}), dict(other.labels))


def _port_matches(rule: dict, port: int, protocol: str) -> bool:
    ports = rule.get("ports")
    if not ports:
        return True
    return any(p["port"] == port and p.get("protocol", "TCP") == protocol for p in ports)


def _side_allows(c: Cluster, pod: Pod, direction: str, other: Pod | str, port: int, proto: str):
    """Policies selecting `pod` for this direction; none = isolated not, so allowed."""
    key = "ingress" if direction == "Ingress" else "egress"
    peer_key = "from" if direction == "Ingress" else "to"
    relevant = [p for p in c.policies if _selects(p, pod) and direction in p["spec"]["policyTypes"]]
    if not relevant:
        return True
    for p in relevant:
        for rule in p["spec"].get(key) or []:
            peers = rule.get(peer_key)
            peer_ok = not peers or any(
                _peer_matches(c, peer, p["metadata"]["namespace"], other) for peer in peers
            )
            if peer_ok and _port_matches(rule, port, proto):
                return True
    return False


def allowed(c: Cluster, src: Pod, dst: Pod | str, port: int, proto: str = "TCP") -> bool:
    egress = _side_allows(c, src, "Egress", dst, port, proto)
    if not isinstance(dst, Pod):
        return egress
    return egress and _side_allows(c, dst, "Ingress", src, port, proto)


def _pod(c: Cluster, name: str, namespace: str = "pulsewatch") -> Pod:
    [pod] = [p for p in c.pods if p.name == name and p.namespace == namespace]
    return pod


# Every connection that may exist between Pulsewatch pods: (client, server, port).
ALLOWED_FLOWS = {
    ("grafana", "prometheus", 9090),
    ("prometheus", "pulsewatch", 8000),
    ("prometheus", "alertmanager", 9093),
    ("alertmanager", "alert-sink", 9095),
}
DNS_CLIENTS = {"pulsewatch", "prometheus", "alertmanager", "grafana"}
INTERNET = {"pulsewatch": [80, 443, 8443], "alertmanager": [443]}


@pytest.mark.parametrize("overlay", LEAF_OVERLAYS, ids=lambda p: p.name)
def test_every_namespace_is_default_deny(overlay):
    docs = _render(overlay)
    namespaces = {d["metadata"]["name"] for d in docs if d["kind"] == "Namespace"}
    assert namespaces, f"{overlay} creates no namespace"
    for ns in namespaces:
        deny = [
            d
            for d in docs
            if d["kind"] == "NetworkPolicy"
            and d["metadata"]["namespace"] == ns
            and d["spec"]["podSelector"] == {}
        ]
        assert deny, f"no default-deny NetworkPolicy in namespace {ns}"
        [policy] = deny
        assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        assert not policy["spec"].get("ingress") and not policy["spec"].get("egress")


@pytest.mark.parametrize("overlay", [KIND, MULTI], ids=["kind", "multi-region"])
def test_connection_matrix(overlay):
    c = _cluster(overlay)
    app_pods = [p for p in c.pods if p.namespace != "kube-system"]
    assert {p.name for p in app_pods} == {
        "pulsewatch",
        "prometheus",
        "alertmanager",
        "alert-sink",
        "grafana",
    }
    for src in app_pods:
        for dst in app_pods:
            for port in dst.ports:
                want = (src.name, dst.name, port) in ALLOWED_FLOWS
                got = allowed(c, src, dst, port)
                where = f"{src.namespace}/{src.name} -> {dst.namespace}/{dst.name}:{port}"
                assert got == want, f"{where}: allowed={got}, want {want}"


@pytest.mark.parametrize("overlay", [KIND, MULTI], ids=["kind", "multi-region"])
def test_dns_and_internet_egress(overlay):
    c = _cluster(overlay)
    [dns] = [p for p in c.pods if p.namespace == "kube-system"]
    for src in (p for p in c.pods if p.namespace != "kube-system"):
        for proto in ("UDP", "TCP"):
            assert allowed(c, src, dns, 53, proto) == (src.name in DNS_CLIENTS), (src, proto)
        for port in (80, 443, 8443):
            want = port in INTERNET.get(src.name, [])
            assert allowed(c, src, PUBLIC_IP, port) == want, (src.name, port)
        # Private networks (LAN, the kind node, other pods by IP) stay closed to agents.
        if src.name == "pulsewatch":
            for ip in ("192.168.1.10", "172.18.0.2", "10.96.0.1", "169.254.169.254"):
                assert not allowed(c, src, ip, 443), ip


def test_multi_region_agents_cannot_reach_each_other():
    c = _cluster(MULTI)
    eu, us = _pod(c, "pulsewatch", "pulsewatch-eu"), _pod(c, "pulsewatch", "pulsewatch-us")
    assert not allowed(c, eu, us, 8000)
    assert not allowed(c, us, eu, 8000)
    prom = _pod(c, "prometheus")
    assert allowed(c, prom, eu, 8000) and allowed(c, prom, us, 8000)


def test_prometheus_namespace_matches_agent_ingress():
    """The agent's ingress rule names the monitoring namespace; both overlays must use it."""
    [policy] = [d for d in _render(K8S / "base") if d["kind"] == "NetworkPolicy"]
    [rule] = policy["spec"]["ingress"]
    [peer] = rule["from"]
    ns = peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
    for overlay in (KIND, MULTI / "monitoring"):
        assert yaml.safe_load((overlay / "kustomization.yaml").read_text())["namespace"] == ns


# --- scripts/netpol_check.py ----------------------------------------------------------

_spec = importlib.util.spec_from_file_location("netpol_check", ROOT / "scripts/netpol_check.py")
netpol = importlib.util.module_from_spec(_spec)
sys.modules["netpol_check"] = netpol  # dataclasses look the module up while loading
_spec.loader.exec_module(netpol)


@pytest.mark.parametrize("overlay", ["kind", "multi-region"])
def test_live_checks_agree_with_the_policies(overlay):
    """Each live check expects what the policies say for that path."""
    c = _cluster(KIND if overlay == "kind" else MULTI)
    for check in netpol.checks_for(overlay):
        src = _pod(c, check.client, check.namespace)
        if check.dest.startswith("svc/"):
            _, ns, name = check.dest.split("/")
            dst: Pod | str = _pod(c, name, ns)
        else:
            dst = PUBLIC_IP  # example.com, 1.1.1.1
        assert allowed(c, src, dst, check.port) == check.allowed, check.label()


def test_outcomes():
    allow = netpol.Check("ns", "c", "example.com", 443, True, "")
    deny = netpol.Check("ns", "c", "svc/ns/x", 1, False, "")
    assert netpol.holds(allow, "open")
    assert not netpol.holds(allow, "timeout")
    assert all(netpol.holds(deny, o) for o in ("timeout", "refused", "error"))
    assert not netpol.holds(deny, "open")
    # Unresolvable destinations prove nothing either way.
    assert not netpol.holds(allow, "dns") and not netpol.holds(deny, "dns")


class FakeKubectl:
    """Answers like kubectl: ClusterIPs, and dial results that depend on applied policies."""

    def __init__(self, enforced: bool = True):
        self.enforced = enforced
        self.control = False
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: str | None) -> str:
        self.calls.append(args)
        if args[:1] == ["apply"]:
            assert netpol.CONTROL_POLICY in stdin
            self.control = True
            return "created"
        if "delete" in args:
            self.control = False
            return "deleted"
        if "get" in args:
            return "10.96.0." + str(len(args[1]))
        host, port, client = args[-2], int(args[-1]), args[3]
        if not self.enforced:
            return "open"
        if not host.startswith("10.96."):  # the internet: only the agent gets out
            return "open" if client == "deploy/pulsewatch" else "timeout"
        if self.control and port == 9090 and client == "deploy/pulsewatch":
            return "open"
        return "timeout"


@pytest.mark.parametrize("overlay", ["kind", "multi-region"])
def test_run_passes_when_enforced(overlay):
    fake = FakeKubectl()
    assert netpol.run(overlay, netpol.Cluster(fake, sleep=lambda s: None), timeout=0) == []
    if overlay == "kind":
        assert not fake.control, "control policy must be deleted again"


def test_run_fails_when_cni_does_not_enforce():
    fake = FakeKubectl(enforced=False)
    failures = netpol.run("kind", netpol.Cluster(fake, sleep=lambda s: None), timeout=0)
    assert failures and all("blocked" in f for f in failures)


def test_control_policy_is_valid_networkpolicy():
    docs = list(yaml.safe_load_all(netpol.control_policy("pulsewatch")))
    assert [d["spec"]["policyTypes"] for d in docs] == [["Egress"], ["Ingress"]]
    assert all(d["metadata"]["name"].startswith(netpol.CONTROL_POLICY) for d in docs)


def test_ci_runs_the_live_check():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "make netpol-check\n" in ci
    assert "make netpol-check OVERLAY=deploy/k8s/overlays/multi-region" in ci
