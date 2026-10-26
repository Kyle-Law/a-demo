# infra/ansible — B0 provisioning

Turns hand-provisioned hosts into three kubeadm clusters with Cilium. No Terraform for compute: Prime Intellect and HotAisle capacity is grabbed on demand through their consoles, so the input is a static inventory of public IPs.

## Network design

Every node of every cluster joins one host-level **WireGuard mesh** (`wg0`, `172.30.0.0/16`):

- C1 spans two providers (A100 on Prime Intellect, MI300X on HotAisle), so nodes have no shared private network, and some providers NAT their public IPs. The mesh gives each node a stable private IP that works in both cases.
- The `wg0` IP is the Kubernetes node IP (`kubelet --node-ip`) and the API server address.
- Cilium runs VXLAN over `wg0`. Cilium's own encryption is off because `wg0` already encrypts.
- Cluster Mesh (B5) needs node-to-node reachability across clusters, and the shared mesh provides it.

Public interfaces accept only SSH, WireGuard (UDP 51820) and, on control planes, the API server (6443) from `admin_cidrs`. ufw is set to default-deny inbound.

| Cluster | Cilium ID | Pod CIDR | Service CIDR | Control plane wg IP | Nodes (wg IP) |
|---|---|---|---|---|---|
| c1 | 1 | 10.1.0.0/16 | 10.101.0.0/16 | 172.30.1.10 | c1-a100 .1.11, c1-mi300x .1.12 |
| c2 | 2 | 10.2.0.0/16 | 10.102.0.0/16 | 172.30.2.10 | c2-a6000 .2.11 |
| c3 | 3 | 10.3.0.0/16 | 10.103.0.0/16 | 172.30.3.10 | c3-w1 .3.11, c3-w2 .3.12 |

Sources of truth: pod CIDRs and cluster IDs live in `platform/cilium/values-<cluster>.yaml`, service CIDRs in `inventory/group_vars/<cluster>.yml`, and wg IPs in `inventory/hosts.yml`.

## Host requirements

Check these when renting a machine. The playbook asserts the first three.

- A **VM or bare metal**, not a container "pod". kubeadm, WireGuard and GPU drivers need the host kernel.
- Ubuntu 22.04 or 24.04, x86_64, cgroup v2.
- No Ubuntu `containerd`/`docker.io` packages (Docker CE's `containerd.io` is fine).
- **UDP 51820 reachable** on the public IP. If the provider maps ports, set `wg_endpoint` (and the port) for that host.
- Root or passwordless sudo over SSH.
- Provider private networks must not overlap `10.1-3.0.0/16`, `10.101-103.0.0/16` or `172.30.0.0/16`. Check with `ip route` on a fresh host.

## Run

```bash
cd infra/ansible
ansible-galaxy collection install -r requirements.yml

# 1. Probe each new host (read-only): ssh <user>@<ip> 'bash -s' < ../host-probe.sh
#    Then write inventory/local.yml (git-ignored; real IPs stay out of this public repo):
#
#      all:
#        hosts:
#          c2-cp:    { ansible_host: 203.0.113.21, ansible_user: root }
#          c2-a6000: { ansible_host: 203.0.113.22, ansible_user: ubuntu }
#        vars:
#          admin_cidrs: ["198.51.100.7/32"]   # where you run kubectl from
#
# 2. First cluster (vertical slice):
ansible-playbook site.yml --limit c2
# 3. Later, once more clusters exist, run without --limit so every node's wg0 lists every peer:
ansible-playbook site.yml
```

The playbook is safe to re-run. `kubeadm init/join` are skipped on nodes that already joined, and the containerd config is written only once, so later GPU operator edits survive a re-run. The Cilium Helm release is installed only if it's absent: from B1 on, Argo CD owns it, and changes go through `platform/cilium/` in Git.

## Verify (B0 "done when")

Admin kubeconfigs are written to `~/.kube/a-demo/<cluster>.yaml`, outside the repo, with context names `c1`, `c2`, `c3`.

```bash
export KUBECONFIG=~/.kube/a-demo/c1.yaml:~/.kube/a-demo/c2.yaml:~/.kube/a-demo/c3.yaml
brew install cilium-cli          # once
for c in c1 c2 c3; do cilium --context $c status --wait; done
kubectl --context c2 get nodes -o wide   # INTERNAL-IP should be the 172.30.x.x wg IP
```

## Replacing a host

On-demand hosts come back with new public IPs. Update `ansible_host` and keep the same `wg_ip`. If it's a worker, delete the old node first (`kubectl --context <c> delete node <name>`, after asking). Then re-run the playbook without `--limit` so every peer learns the new endpoint. If it's a control plane, the cluster has to be rebuilt.

## B1: Argo CD hub (`gitops.yml`)

Run after `site.yml`, once C3 and at least one GPU cluster exist:

```bash
ansible-playbook gitops.yml          # or --limit c2,c3 while C1 doesn't exist yet
```

1. On C1/C2: creates `kube-system/argocd-manager` (cluster-admin) and a token for it.
2. On C3: installs Argo CD (install-only; afterwards it manages itself from `clusters/c3/argocd.yaml`).
3. Writes one Argo CD cluster Secret per spoke (`c1`, `c2`, server = the spoke's wg IP) straight into C3. Tokens never touch Git or disk.
4. Creates the `roots` ApplicationSet (`clusters/c3/roots.yaml`). From here on, everything comes from Git.

UI: `kubectl --context c3 -n argocd port-forward svc/argocd-server 8080:443`, user `admin`, password from
`kubectl --context c3 -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`.
