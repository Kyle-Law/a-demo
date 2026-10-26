# infra/ansible — B0 provisioning

Turns hand-provisioned hosts into three kubeadm clusters with Cilium. No Terraform for compute: Prime Intellect and HotAisle capacity is grabbed on demand through their consoles, so the input is a static inventory of public IPs.

## Host requirements

Check these when renting a machine. The playbook asserts the first three.

- A **VM or bare metal**, not a container "pod". kubeadm, WireGuard and GPU drivers need the host kernel.
- Ubuntu 22.04 or 24.04, x86_64, cgroup v2.
- No Ubuntu `containerd`/`docker.io` packages (Docker CE's `containerd.io` is fine).
- Root or passwordless sudo over SSH.

## Run

```bash
cd infra/ansible
ansible-galaxy collection install -r requirements.yml

# 1. Fill in inventory/hosts.yml (public IPs, ansible_user) and admin_cidrs in group_vars/all.yml.
# 2. First cluster (vertical slice):
ansible-playbook site.yml --limit c2
```
