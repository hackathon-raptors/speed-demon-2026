#!/usr/bin/env bash
# Lock down the Speed Demon scoring box before any participant code reaches it.
# Run as root once, before setup-box.sh. Safe to re-run.
set -euo pipefail

echo "== sshd: keys only, no forwarding"
cat > /etc/ssh/sshd_config.d/10-speeddemon.conf <<'CONF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
CONF
sshd -t
systemctl reload ssh

echo "== firewall: inbound SSH and ICMP only, outbound open (apt, image pulls, git, S3)"
apt-get install -y -qq nftables
cat > /etc/nftables.conf <<'NFT'
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ct state invalid drop
    ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } limit rate 10/second accept
    ip6 nexthdr icmpv6 icmpv6 type { echo-request, destination-unreachable, packet-too-big, time-exceeded, parameter-problem, nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, nd-router-solicit } accept
    tcp dport 22 ct state new limit rate 60/minute accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output { type filter hook output priority 0; policy accept; }
}
NFT
# The ruleset is flushed again after 180 s unless an operator cancels the timer from a NEW
# ssh session, so a mistake cannot lock us out (this session survives as "established").
systemctl stop sd-fw-rollback.timer 2>/dev/null || true
systemd-run --quiet --unit=sd-fw-rollback --on-active=180 /usr/sbin/nft flush ruleset
nft -f /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1
echo "firewall loaded; from a NEW ssh session run: systemctl stop sd-fw-rollback.timer"

echo "== no heavy background jobs during the event window (re-enable after Nov 14)"
systemctl disable --now mdcheck_start.timer mdcheck_continue.timer fstrim.timer 2>/dev/null || true

echo "== security updates now, nothing automatic later"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get -y -qq full-upgrade
echo "hardened"
