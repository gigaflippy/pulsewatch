# Local Pulsewatch platform: kind cluster + app image + monitoring + probe regions.
# Everything runs on your machine: no cloud account, no registry, no cost.

locals {
  repo_root    = abspath("${path.module}/../..")
  overlay_path = "${local.repo_root}/deploy/k8s/overlays/${var.overlay}"

  # Rebuild and reload the image only when something that goes into it changes.
  image_inputs = sha256(join("", [
    for f in sort(concat(
      tolist(fileset(local.repo_root, "src/**")),
      ["Dockerfile", "pyproject.toml"],
    )) : filesha256("${local.repo_root}/${f}")
  ]))
}

resource "kind_cluster" "this" {
  name            = var.cluster_name
  node_image      = var.node_image
  wait_for_ready  = true
  kubeconfig_path = "${path.module}/kubeconfig"

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }
  }
}

# kind has no registry by default; images are side-loaded into the node.
resource "terraform_data" "image" {
  count = var.build_image ? 1 : 0

  triggers_replace = [kind_cluster.this.id, var.image, local.image_inputs]

  provisioner "local-exec" {
    working_dir = local.repo_root
    command     = "docker build -t ${var.image} . && kind load docker-image ${var.image} --name ${kind_cluster.this.name}"
  }
}

provider "kustomization" {
  kubeconfig_raw = kind_cluster.this.kubeconfig
}

data "kustomization_build" "stack" {
  path = local.overlay_path
}

# Apply in priority order: namespaces and cluster-scoped resources first, then
# workloads, then anything that depends on those (the provider's standard pattern).
resource "kustomization_resource" "p0" {
  for_each = data.kustomization_build.stack.ids_prio[0]
  manifest = data.kustomization_build.stack.manifests[each.value]
}

resource "kustomization_resource" "p1" {
  for_each = data.kustomization_build.stack.ids_prio[1]
  manifest = data.kustomization_build.stack.manifests[each.value]

  # Pods need the image present before they start.
  depends_on = [kustomization_resource.p0, terraform_data.image]
}

resource "kustomization_resource" "p2" {
  for_each   = data.kustomization_build.stack.ids_prio[2]
  manifest   = data.kustomization_build.stack.manifests[each.value]
  depends_on = [kustomization_resource.p1]
}
