import https from "node:https";
import { readFile } from "node:fs/promises";

const port = Number(process.env.TESTPILOT_GATEWAY_PORT ?? "9443");
const maxBodyBytes = 32 * 1024;
const workerUrl = new URL(process.env.TESTPILOT_WORKER_URL ?? "https://127.0.0.1:9444/internal/v1/health-checks");
if (workerUrl.protocol !== "https:" || workerUrl.username || workerUrl.password) throw new Error("Worker URL must use HTTPS without userinfo");

function json(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, { "content-type": "application/json", "content-length": data.length, "cache-control": "no-store" });
  res.end(data);
}

async function readJson(req) {
  const chunks = []; let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > maxBodyBytes) throw Object.assign(new Error("BODY_TOO_LARGE"), { status: 413 });
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw Object.assign(new Error("INVALID_JSON"), { status: 400 }); }
}

function forward(payload) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(payload));
    const request = https.request(workerUrl, {
      method: "POST",
      cert: gatewayWorkerCert,
      key: gatewayWorkerKey,
      ca: workerCa,
      rejectUnauthorized: true,
      timeout: 8000,
      headers: { "content-type": "application/json", "content-length": data.length, "connection": "close" },
    }, (response) => {
      const chunks = []; let bytes = 0;
      response.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes > 4096) { response.destroy(new Error("WORKER_RESPONSE_TOO_LARGE")); return; }
        chunks.push(chunk);
      });
      response.on("end", () => {
        if (response.statusCode !== 200) return reject(new Error("WORKER_REJECTED_HEALTH_CHECK"));
        try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
        catch { reject(new Error("WORKER_INVALID_RESPONSE")); }
      });
    });
    request.on("timeout", () => request.destroy(new Error("WORKER_TIMEOUT")));
    request.on("error", reject);
    request.end(data);
  });
}

const gatewayCert = await readFile(process.env.TESTPILOT_GATEWAY_TLS_CERT);
const gatewayKey = await readFile(process.env.TESTPILOT_GATEWAY_TLS_KEY);
const apiClientCa = await readFile(process.env.TESTPILOT_GATEWAY_API_CLIENT_CA_CERT);
const gatewayWorkerCert = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_CERT);
const gatewayWorkerKey = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_KEY);
const workerCa = await readFile(process.env.TESTPILOT_GATEWAY_WORKER_CA_CERT);

const server = https.createServer({
  cert: gatewayCert, key: gatewayKey, ca: apiClientCa,
  requestCert: true, rejectUnauthorized: true,
}, async (req, res) => {
  if (req.method === "GET" && req.url === "/internal/health/ready") return json(res, 200, { ready: true, gateway_version: "health-probe-v1" });
  if (req.method !== "POST" || req.url !== "/internal/v1/health-checks") return json(res, 404, { error: "NOT_FOUND" });
  try {
    const payload = await readJson(req);
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.allowed_domains)) return json(res, 422, { error: "INVALID_HEALTH_CHECK_REQUEST" });
    return json(res, 200, await forward(payload));
  } catch (error) {
    return json(res, 503, { error: error.status ? error.message : "BROWSER_WORKER_UNAVAILABLE" });
  }
});
server.requestTimeout = 10000;
server.listen(port, "0.0.0.0");
