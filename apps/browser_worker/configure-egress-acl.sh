#!/usr/bin/env bash
set -euo pipefail

chain="TESTPILOT_BROWSER_EGRESS"
comment="testpilot-browser-worker-egress"
worker_subnet="${TESTPILOT_BROWSER_WORKER_EGRESS_SUBNET:?set the Worker bridge subnet from compose.yaml}"
mode="${1:-apply}"

validate_ipv4_network() {
  python3 - "$1" <<'PY'
import ipaddress
import sys
try:
    network = ipaddress.ip_network(sys.argv[1], strict=True)
except ValueError as error:
    raise SystemExit(str(error))
if network.version != 4:
    raise SystemExit("IPv4 network required")
print(network)
PY
}

worker_subnet="$(validate_ipv4_network "$worker_subnet")"

if [[ "$mode" == "remove" ]]; then
  while iptables -C DOCKER-USER -s "$worker_subnet" -m comment --comment "$comment" -j "$chain" 2>/dev/null; do
    iptables -D DOCKER-USER -s "$worker_subnet" -m comment --comment "$comment" -j "$chain"
  done
  if ! iptables -S | grep -F -- "-j $chain" >/dev/null; then
    iptables -F "$chain" 2>/dev/null || true
    iptables -X "$chain" 2>/dev/null || true
  fi
  exit 0
fi
if [[ "$mode" != "apply" ]]; then
  echo "usage: $0 [apply|remove]" >&2
  exit 2
fi
iptables -nL DOCKER-USER >/dev/null
dns_server="${TESTPILOT_BROWSER_WORKER_DNS_SERVER:?set the approved IPv4 DNS resolver used by Compose}"
dns_cidr="$(validate_ipv4_network "${dns_server}/32")"
if ! iptables -nL "$chain" >/dev/null 2>&1; then
  iptables -N "$chain"
fi

# Rebuild only this user-defined chain as one filter-table restore transaction.
# The existing DOCKER-USER jump stays in place while its rules are replaced.
{
  printf '*filter\n:%s - [0:0]\n' "$chain"
  printf '%s\n' \
    "-A $chain -d $dns_cidr -p udp --dport 53 -j ACCEPT" \
    "-A $chain -d $dns_cidr -p tcp --dport 53 -j ACCEPT"
  # The dedicated subnet contains the Worker and its directly configured Gateways.
  printf '%s\n' "-A $chain -d $worker_subnet -j ACCEPT"

  # Block non-public and special-use destinations before public web access.
  for blocked in \
    0.0.0.0/8 \
    10.0.0.0/8 \
    100.64.0.0/10 \
    127.0.0.0/8 \
    169.254.0.0/16 \
    172.16.0.0/12 \
    192.0.0.0/24 \
    192.0.2.0/24 \
    192.88.99.0/24 \
    192.168.0.0/16 \
    198.18.0.0/15 \
    198.51.100.0/24 \
    203.0.113.0/24 \
    224.0.0.0/4 \
    240.0.0.0/4; do
    printf '%s\n' "-A $chain -d $blocked -j DROP"
  done

  printf '%s\n' \
    "-A $chain -p tcp -m multiport --dports 80,443 -j ACCEPT" \
    "-A $chain -j DROP" \
    'COMMIT'
} | iptables-restore --noflush

if ! iptables -C DOCKER-USER -s "$worker_subnet" -m comment --comment "$comment" -j "$chain" 2>/dev/null; then
  iptables -I DOCKER-USER 1 -s "$worker_subnet" -m comment --comment "$comment" -j "$chain"
fi
