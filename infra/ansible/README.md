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

# 1. Fill in inventory/hosts.yml (public IPs, ansible_user) and admin_cidrs in group_vars/all.yml.
# 2. First cluster (vertical slice):
ansible-playbook site.yml --limit c2
# 3. Later, once more clusters exist, run without --limit so every node's wg0 lists every peer:
ansible-playbook site.yml
```

## Replacing a host

On-demand hosts come back with new public IPs. Update `ansible_host` and keep the same `wg_ip`. If it's a worker, delete the old node first (`kubectl --context <c> delete node <name>`, after asking). Then re-run the playbook without `--limit` so every peer learns the new endpoint. If it's a control plane, the cluster has to be rebuilt.
