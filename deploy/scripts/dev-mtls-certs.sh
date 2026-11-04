#!/usr/bin/env bash
# DEV-ONLY mTLS certificate matrix for the local browser execution chain.
#
# !!! 这些证书只用于本机验证。私钥无口令、有效期 30 天、CA 与叶子证书同机存放。
# !!! 严禁用于任何生产或共享环境。生产证书的签发与轮换见 deploy/README.md 第 6 节。
#
# 产物（默认 .certs/，已被 .gitignore 忽略）：
#   ca-api.crt / ca-api.key                    API 侧信任根 + 签发 gateway 服务端证书
#   ca-worker.crt / ca-worker.key              Worker 侧信任根 + 签发双向客户端证书
#   api-client.crt/.key                        API 访问 Gateway 的客户端证书
#   gateway-server.crt/.key                    Gateway 的 TLS 服务端证书（SAN 含 127.0.0.1）
#   gateway-worker-client.crt/.key             Gateway 访问 Worker 的客户端证书
#   worker-server.crt/.key                     Worker 的 TLS 服务端证书，同时兼作注册客户端证书
#                                              （CN 必须等于 TESTPILOT_WORKER_INSTANCE 与 pool key）
#   env.sh                                     三个进程共用的一整套 TESTPILOT_* 导出
#
# 用法：
#   bash deploy/scripts/dev-mtls-certs.sh          # 生成到 .certs/
#   bash deploy/scripts/dev-mtls-certs.sh --force  # 覆盖重建（会改变 CA，需重启三进程）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Git Bash 给出 /d/... 形式，Node 与 Python 原生进程读不了，统一切成 D:/... 形式。
ROOT_WIN="$(cygpath -m "$ROOT" 2>/dev/null || printf '%s' "$ROOT")"
CERT_DIR="${CERT_DIR:-$ROOT_WIN/.certs}"
DAYS="${DAYS:-30}"
WORKER_INSTANCE="${WORKER_INSTANCE:-browser-worker-1}"
GATEWAY_PORT="${GATEWAY_PORT:-9443}"
WORKER_PORT="${WORKER_PORT:-9444}"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
if [ -d "$CERT_DIR" ] && [ "$FORCE" -ne 1 ]; then
  echo "拒绝覆盖已存在的 $CERT_DIR（如需重建请加 --force）" >&2
  exit 1
fi
rm -rf "$CERT_DIR"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

ca() {
  local name="$1" cn="$2"
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days "$DAYS" \
    -keyout "$name.key" -out "$name.crt" -subj "/CN=$cn/O=TestPilot Dev" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
}

leaf() {
  local name="$1" cn="$2" ca_name="$3"
  openssl req -new -newkey rsa:2048 -sha256 -nodes \
    -keyout "$name.key" -out "$name.csr" -subj "/CN=$cn/O=TestPilot Dev" 2>/dev/null
  cat > "$name.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:localhost,IP:127.0.0.1
authorityKeyIdentifier=keyid,issuer
EOF
  openssl x509 -req -in "$name.csr" -CA "$ca_name.crt" -CAkey "$ca_name.key" \
    -set_serial "0x$(openssl rand -hex 8)" -days "$DAYS" -sha256 \
    -extfile "$name.ext" -out "$name.crt" 2>/dev/null
  rm -f "$name.csr" "$name.ext"
}

ca ca-api "TestPilot Dev API CA"
ca ca-worker "TestPilot Dev Worker CA"

leaf api-client           testpilot-api-client ca-api
leaf gateway-server       localhost            ca-api
leaf gateway-worker-client testpilot-gateway   ca-worker
leaf worker-server        "$WORKER_INSTANCE"   ca-worker

chmod 600 ./*.key
chmod 644 ./*.crt

JOB_TOKEN_SECRET="$(openssl rand -hex 32)"

cat > env.sh <<EOF
# 由 deploy/scripts/dev-mtls-certs.sh 生成 —— DEV ONLY，请勿提交或用于生产。
export CERT_DIR="$CERT_DIR"

# --- Agent Worker / API：调用 Gateway 的 mTLS 客户端 ---
export TESTPILOT_BROWSER_GATEWAY_URL="https://127.0.0.1:$GATEWAY_PORT"
export TESTPILOT_BROWSER_GATEWAY_CLIENT_CERT="\$CERT_DIR/api-client.crt"
export TESTPILOT_BROWSER_GATEWAY_CLIENT_KEY="\$CERT_DIR/api-client.key"
export TESTPILOT_BROWSER_GATEWAY_CA_CERT="\$CERT_DIR/ca-api.crt"
export TESTPILOT_BROWSER_JOB_TOKEN_SECRET="$JOB_TOKEN_SECRET"

# --- Browser Gateway ---
export TESTPILOT_GATEWAY_PORT="$GATEWAY_PORT"
export TESTPILOT_GATEWAY_TLS_CERT="\$CERT_DIR/gateway-server.crt"
export TESTPILOT_GATEWAY_TLS_KEY="\$CERT_DIR/gateway-server.key"
export TESTPILOT_GATEWAY_API_CLIENT_CA_CERT="\$CERT_DIR/ca-api.crt"
export TESTPILOT_GATEWAY_WORKER_CLIENT_CA_CERT="\$CERT_DIR/ca-worker.crt"
export TESTPILOT_GATEWAY_WORKER_CERT="\$CERT_DIR/gateway-worker-client.crt"
export TESTPILOT_GATEWAY_WORKER_KEY="\$CERT_DIR/gateway-worker-client.key"
export TESTPILOT_GATEWAY_WORKER_CA_CERT="\$CERT_DIR/ca-worker.crt"
export TESTPILOT_GATEWAY_WORKER_POOL_JSON='{"$WORKER_INSTANCE":"https://127.0.0.1:$WORKER_PORT"}'
export TESTPILOT_WORKER_URL="https://127.0.0.1:$WORKER_PORT/internal/v1/health-checks"

# --- Browser Worker ---
export TESTPILOT_WORKER_PORT="$WORKER_PORT"
export TESTPILOT_WORKER_INSTANCE="$WORKER_INSTANCE"
export TESTPILOT_WORKER_TLS_CERT="\$CERT_DIR/worker-server.crt"
export TESTPILOT_WORKER_TLS_KEY="\$CERT_DIR/worker-server.key"
export TESTPILOT_WORKER_CLIENT_CA_CERT="\$CERT_DIR/ca-worker.crt"
export TESTPILOT_WORKER_GATEWAY_URLS_JSON='["https://127.0.0.1:$GATEWAY_PORT"]'
export TESTPILOT_WORKER_GATEWAY_CA_CERT="\$CERT_DIR/ca-api.crt"
export TESTPILOT_WORKER_GATEWAY_CLIENT_CERT="\$CERT_DIR/worker-server.crt"
export TESTPILOT_WORKER_GATEWAY_CLIENT_KEY="\$CERT_DIR/worker-server.key"

# --- Worker 浏览器运行时（本机验证用收紧值，可按需覆盖）---
export TESTPILOT_BROWSER_MAX_SESSIONS="\${TESTPILOT_BROWSER_MAX_SESSIONS:-2}"
EOF

echo "证书矩阵已生成：$CERT_DIR"
echo "  CA:     ca-api.crt / ca-worker.crt"
echo "  叶子:   api-client / gateway-server / gateway-worker-client / worker-server"
echo "  环境:   source \"$CERT_DIR/env.sh\""
echo
echo "校验："
openssl verify -CAfile ca-api.crt api-client.crt gateway-server.crt | sed 's/^/  /'
openssl verify -CAfile ca-worker.crt gateway-worker-client.crt worker-server.crt | sed 's/^/  /'
openssl x509 -in worker-server.crt -noout -subject -ext subjectAltName | sed 's/^/  /'
