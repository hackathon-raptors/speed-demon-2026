#!/usr/bin/env bash
# Prepare a Hetzner EX44 (Intel i5-13500, Debian 13) as the Speed Demon scoring box.
# Run as root once after provisioning. Safe to re-run.
set -euo pipefail

ALLOWLIST="${1:-$(dirname "$0")/allowlist.json}"

echo "== packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git jq vmtouch linux-cpupower python3

if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi

echo "== docker: cgroupfs so each run's parent cgroup survives --rm; no networking of its own"
# Every run and build uses --network=none, so docker needs no bridge and must not touch the firewall.
# Container logs are capped so a submission cannot fill the disk through stdout.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "exec-opts": ["native.cgroupdriver=cgroupfs"],
  "bridge": "none",
  "iptables": false,
  "ip6tables": false,
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "1" },
  "builder": { "gc": { "enabled": true, "defaultKeepStorage": "100GB" } }
}
JSON
systemctl restart docker
docker info --format '{{.CgroupDriver}} cgroup v{{.CgroupVersion}}' | grep -q '^cgroupfs cgroup v2$' \
  || { echo "expected cgroupfs on cgroup v2"; exit 1; }

# SPEEDDEMON_TEST_HOST=1 skips the EX44-only steps on a functional test box (e.g. a cloud VM):
# no hybrid-CPU topology and no intel_pstate there, so its numbers never count.
if [ "${SPEEDDEMON_TEST_HOST:-0}" = "1" ]; then
  echo "== test host: skipping P-core and clock checks; timings from this box are not comparable"
else

echo "== P-cores must be CPUs 0-11 before --cpuset-cpus=0-11 is published"
pcores="$(cat /sys/devices/cpu_core/cpus)"
ecores="$(cat /sys/devices/cpu_atom/cpus)"
echo "P-cores: $pcores  E-cores: $ecores"
[ "$pcores" = "0-11" ] || { echo "P-cores are not 0-11; fix the published cpuset first"; exit 1; }

echo "== fixed clocks: performance governor, turbo off (persisted by a oneshot unit)"
cat > /etc/systemd/system/speeddemon-clocks.service <<'UNIT'
[Unit]
Description=Speed Demon fixed clocks
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/usr/bin/cpupower frequency-set -g performance
ExecStart=/bin/sh -c 'echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now speeddemon-clocks.service
echo "no_turbo=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo) governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
fi

echo "== directories (outputs live on a capped tmpfs so no run can fill the disk)"
mkdir -p /srv/speeddemon/{data,work,bin}
# persist the capped work tmpfs across reboot: fstab is the single source of truth, nofail so a bad
# line can never drop this headless box to the emergency console.
grep -qE '^[[:space:]]*tmpfs[[:space:]]+/srv/speeddemon/work[[:space:]]' /etc/fstab \
  || printf 'tmpfs /srv/speeddemon/work tmpfs size=8g,mode=0755,nofail 0 0\n' >> /etc/fstab
mountpoint -q /srv/speeddemon/work || mount /srv/speeddemon/work

echo "== in-container timer, built static for this box (host compiler: docker has no network here)"
apt-get install -y -qq musl-tools binutils file
musl-gcc -static -O2 -Wall -Wextra -o /srv/speeddemon/bin/sd-timer "$(dirname "$(readlink -f "$0")")/timer/sd-timer.c"
file /srv/speeddemon/bin/sd-timer | grep -q "statically linked" || { echo "sd-timer is not static"; exit 1; }

echo "== pre-pull every allowlisted base image by digest"
jq -r '.images[] | "\(.image)@\(.digest)"' "$ALLOWLIST" | while read -r ref; do
  docker pull -q "$ref"
done

echo "box ready"
