#!/usr/bin/env bash
# Read-only check of a freshly rented host against B0's assumptions. Changes nothing.
# Usage: ssh <user>@<public-ip> 'bash -s' < infra/host-probe.sh
set -uo pipefail

section() { printf '\n== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo -n"; fi

section "host"
. /etc/os-release && echo "os: $PRETTY_NAME"
echo "kernel: $(uname -r)  arch: $(uname -m)"
echo "cpus: $(nproc)  mem: $(free -g | awk '/Mem:/{print $2}')G  root disk: $(df -h / | awk 'NR==2{print $2" ("$4" free)"}')"
echo "virt: $(systemd-detect-virt 2>/dev/null)  container: $(systemd-detect-virt --container 2>/dev/null)"
echo "cgroup fs: $(stat -fc %T /sys/fs/cgroup)"
echo "user: $(id -un)  passwordless sudo: $( [ -z "$SUDO" ] || sudo -n true 2>/dev/null && echo yes || echo NO)"
echo "swap: $(swapon --noheadings --show=NAME,SIZE 2>/dev/null | tr '\n' ' ' || true)"

section "network"
SSH_SERVER_IP=$(echo "${SSH_CONNECTION:-}" | awk '{print $3}')
echo "ssh arrived on local address: ${SSH_SERVER_IP:-unknown} (compare with the public IP: same = public IP on an interface, different = NAT)"
ip -4 -o addr show scope global | awk '{print "addr: "$2" "$4}'
ip -4 route show default | sed 's/^/default route: /'
DEFAULT_DEV=$(ip -4 route show default | awk '{print $5; exit}')
[ -n "$DEFAULT_DEV" ] && echo "mtu $DEFAULT_DEV: $(cat /sys/class/net/"$DEFAULT_DEV"/mtu)"
echo "routes overlapping the addressing plan (10.1-3/16, 10.101-103/16, 172.30/16):"
ip -4 route | grep -E '^(10\.(1|2|3|101|102|103)\.|172\.30\.)' || echo "  none"
echo "wireguard module: $(modinfo -F version wireguard 2>/dev/null || (modinfo wireguard >/dev/null 2>&1 && echo built-in) || echo MISSING)"
echo "listeners on ports B0 needs (6443 10250 2379 51820):"
$SUDO ss -lntup 2>/dev/null | grep -E ':(6443|10250|2379|51820)\b' || echo "  none"
echo "ufw: $($SUDO ufw status 2>/dev/null | head -1 || echo 'not installed')"
echo "iptables INPUT policy: $($SUDO iptables -S INPUT 2>/dev/null | head -1 || echo unknown)"

section "packages"
dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\n' \
  containerd containerd.io docker.io docker-ce kubelet kubeadm unattended-upgrades \
  'nvidia-driver-*' 'nvidia-container-toolkit' 'cuda-drivers*' 'rocm*' 'amdgpu-dkms' 2>/dev/null \
  | awk '$NF ~ /^ii/ {print "  "$1" "$2}'
have docker && echo "docker: $(docker --version 2>/dev/null)"

section "nvidia"
if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
  lspci 2>/dev/null | grep -i nvidia | sed 's/^/pci: /'
  nvidia-smi --query-gpu=index,name,driver_version,memory.total,mig.mode.current,mig.mode.pending --format=csv,noheader
  nvidia-smi -q 2>/dev/null | grep -iE 'Virtualization Mode|Host VGPU Mode|MIG Mode' -A2 | sed 's/^/  /'
  echo "loaded modules: $(lsmod | awk '/^nvidia/{print $1}' | tr '\n' ' ')"
  echo "nvidia processes: $(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader | wc -l)"
else
  echo "no working nvidia-smi (no GPU, or driver not loaded)"
fi

section "amd"
if have amd-smi || have rocm-smi; then
  (amd-smi static --asic 2>/dev/null || rocm-smi --showproductname 2>/dev/null) | head -20
  echo "rocm: $(cat /opt/rocm/.info/version 2>/dev/null || echo unknown)"
else
  echo "no amd-smi/rocm-smi"
fi
echo "amdgpu module: $(lsmod | awk '$1=="amdgpu"{print "loaded"}' | head -1)"
