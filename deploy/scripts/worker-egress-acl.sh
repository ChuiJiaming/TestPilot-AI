#!/usr/bin/env bash
# TestPilot AI — Browser Worker host egress ACL (DOCKER-USER chain)
#
# Purpose: enforce the egress policy for the dedicated Browser Worker subnet.
# The Worker must reach:
#   - the Gateway over mTLS (same-subnet / specific origin)
#   - the public web ONLY through the approved, explicitly-configured IPv4
#     DNS resolver (no DNS-over-alternate, no IPv6)
#
# Everything else egressing from the Worker subnet is dropped. This is the
# host-level backstop that complements the Worker's in-container DNS pinning
# and redirect/subresource interception.
#
# STATUS: CANDIDATE — NOT applied. Review subnet, Gateway CIDR, and resolver
# IP for the real environment, then apply once and persist with iptables-persistent.

set -euo pipefail

WORKER_SUBNET="${TESTPILOT_BROWSER_WORKER_EGRESS_SUBNET:-172.30.60.0/24}"
GATEWAY_CIDR="${TESTPILOT_GATEWAY_CIDR:-172.30.60.2/32}"
DNS_RESOLVER_IP="${TESTPILOT_BROWSER_WORKER_DNS_SERVER:?set approved IPv4 DNS resolver}"
GATEWAY_PORT="${TESTPILOT_GATEWAY_PORT:-9443}"
DNS_PORT="53"

DOCKER_USER="DOCKER-USER"

# idempotent: flush any prior TestPilot rules from DOCKER-USER
iptables -D "$DOCKER_USER" -s "$WORKER_SUBNET" -j TESTPILOT_WORKER_EGRESS 2>/dev/null || true
iptables -F TESTPILOT_WORKER_EGRESS 2>/dev/null || true
iptables -X TESTPILOT_WORKER_EGRESS 2>/dev/null || true

# dedicated chain for clarity and easy flushing
iptables -N TESTPILOT_WORKER_EGRESS

# 1) Allow the Gateway hop (mTLS) from the Worker subnet
iptables -A TESTPILOT_WORKER_EGRESS -p tcp -d "$GATEWAY_CIDR" --dport "$GATEWAY_PORT" -j ACCEPT

# 2) Allow DNS ONLY to the approved resolver, over UDP and TCP
iptables -A TESTPILOT_WORKER_EGRESS -p udp -d "$DNS_RESOLVER_IP" --dport "$DNS_PORT" -j ACCEPT
iptables -A TESTPILOT_WORKER_EGRESS -p tcp -d "$DNS_RESOLVER_IP" --dport "$DNS_PORT" -j ACCEPT

# 3) Allow public web (80/443) — Worker's in-container DNS pinning still
#    constrains resolution to the approved resolver; this rule only opens the
#    transport. Tighten per policy if only a domain allowlist is required.
iptables -A TESTPILOT_WORKER_EGRESS -p tcp --dport 80  -j ACCEPT
iptables -A TESTPILOT_WORKER_EGRESS -p tcp --dport 443 -j ACCEPT

# 4) Default drop everything else from the Worker subnet
iptables -A TESTPILOT_WORKER_EGRESS -j REJECT --reject-with icmp-admin-prohibited

# 5) Route Worker-subnet traffic through the chain (before DOCKER's ACCEPT)
iptables -I "$DOCKER_USER" 1 -s "$WORKER_SUBNET" -j TESTPILOT_WORKER_EGRESS

echo "Applied Worker egress ACL:"
echo "  subnet     $WORKER_SUBNET"
echo "  gateway    $GATEWAY_CIDR:$GATEWAY_PORT"
echo "  dns        $DNS_RESOLVER_IP"
echo
echo "Persist with: sudo netfilter-persistent save   (or iptables-persistent)"
