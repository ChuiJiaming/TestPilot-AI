import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { Readable } from "node:stream";
import test from "node:test";

import { handleBrowserGatewayRequest } from "./browser_gateway_sessions.mjs";

const secret = "phase3-test-secret";
const runId = "b5a3a598-0e9f-4f9a-a6aa-b29c0119f26e";
const runCaseId = "89a8eafe-2dba-4d3f-aacf-7206c28b4db8";
const sessionId = "30964104-49e9-4f6a-8a91-58011f93a963";
const attemptToken = "408fae39-61ce-40db-96f3-c2a609b829ee";

function jobToken(overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const claims = Buffer.from(JSON.stringify({
    iss: "testpilot-agent-worker",
    aud: "testpilot-browser-gateway",
    run_id: runId,
    run_case_id: runCaseId,
    browser: "chromium",
    attempt_token: attemptToken,
    base_url: "https://example.com",
    allowed_domains: ["example.com"],
    iat: now,
    exp: now + 60,
    ...overrides,
  })).toString("base64url");
  const signingInput = `${header}.${claims}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

async function invokeGateway(path, payload) {
  process.env.TESTPILOT_BROWSER_JOB_TOKEN_SECRET = secret;
  const req = Readable.from([Buffer.from(JSON.stringify(payload))]);
  req.method = "POST";
  req.url = path;
  let statusCode;
  let body;
  const res = {
    writeHead(status) { statusCode = status; },
    end(value) { body = JSON.parse(value.toString("utf8")); },
  };
  const forwarded = [];
  const handled = await handleBrowserGatewayRequest(req, res, async (...args) => {
    forwarded.push(args);
    return { status: "SUCCEEDED" };
  });
  return { handled, statusCode, body, forwarded };
}

function actionPayload(overrides = {}) {
  return {
    run_id: runId,
    run_case_id: runCaseId,
    browser: "chromium",
    attempt_token: attemptToken,
    worker_instance: "worker-a",
    action: { action_id: "2d599dcb-6ead-4b04-af85-2fb2d0046215", action: "click" },
    job_token: jobToken({ worker_instance: "worker-a" }),
    ...overrides,
  };
}

test("forwards an in-scope action to the signed worker instance", async () => {
  const result = await invokeGateway(
    `/internal/v1/browser-sessions/${sessionId}/actions`, actionPayload(),
  );

  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 200);
  assert.equal(result.body.status, "SUCCEEDED");
  assert.equal(result.forwarded.length, 1);
  assert.equal(result.forwarded[0][2].workerInstance, "worker-a");
});

test("rejects a cross-RunCase action before forwarding", async () => {
  const result = await invokeGateway(
    `/internal/v1/browser-sessions/${sessionId}/actions`,
    actionPayload({ run_case_id: "e85740a4-070d-4a95-861f-a39fcb90e4a2" }),
  );

  assert.equal(result.statusCode, 403);
  assert.equal(result.body.error, "JOB_TOKEN_SCOPE_MISMATCH");
  assert.equal(result.forwarded.length, 0);
});

test("rejects a cross-attempt action before forwarding", async () => {
  const result = await invokeGateway(
    `/internal/v1/browser-sessions/${sessionId}/actions`,
    actionPayload({ attempt_token: "7992b23d-9501-4e38-8fc6-ff865ae101dc" }),
  );

  assert.equal(result.statusCode, 403);
  assert.equal(result.body.error, "JOB_TOKEN_SCOPE_MISMATCH");
  assert.equal(result.forwarded.length, 0);
});
