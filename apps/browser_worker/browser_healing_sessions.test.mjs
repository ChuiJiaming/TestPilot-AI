import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { Readable } from "node:stream";
import test from "node:test";

import { handleBrowserWorkerRequest } from "./browser_sessions.mjs";
import * as workerModule from "./browser_sessions.mjs";

const secret = "m10-worker-test-secret";
const runId = "b5a3a598-0e9f-4f9a-a6aa-b29c0119f26e";
const runCaseId = "89a8eafe-2dba-4d3f-aacf-7206c28b4db8";
const sessionId = "30964104-49e9-4f6a-8a91-58011f93a963";
const attemptToken = "408fae39-61ce-40db-96f3-c2a609b829ee";

function jobToken(overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const claims = Buffer.from(JSON.stringify({
    iss: "testpilot-agent-worker", aud: "testpilot-browser-gateway",
    run_id: runId, run_case_id: runCaseId, browser: "chromium",
    attempt_token: attemptToken, base_url: "https://example.com",
    allowed_domains: ["example.com"], worker_instance: "browser-worker",
    iat: now, exp: now + 60, ...overrides,
  })).toString("base64url");
  const signingInput = `${header}.${claims}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

async function invokeWorker(path, payload) {
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
  const handled = await handleBrowserWorkerRequest(req, res);
  return { handled, statusCode, body };
}

function handoff() {
  const nextAttemptToken = "7c0fcb91-e0fc-4a62-8508-405d49d6d45d";
  const grant = {
    healing_id: "7673e52a-8140-429a-849e-1417da57a434",
    session_id: sessionId, run_id: runId, run_case_id: runCaseId,
    browser: "chromium", previous_attempt_token: attemptToken,
    attempt_token: nextAttemptToken, worker_instance: "browser-worker",
  };
  const claims = {
    run_id: runId, run_case_id: runCaseId, browser: "chromium",
    attempt_token: nextAttemptToken, worker_instance: "browser-worker",
    base_url: "https://example.com", allowed_domains: ["example.com"],
    recovery_handoff: grant,
  };
  return { nextAttemptToken, grant, claims };
}

function target({ count = 1, visible = true, descriptor = {} } = {}) {
  let clicks = 0;
  return {
    async count() { return count; },
    async isVisible() { return visible; },
    async evaluate() { return descriptor; },
    async click() { clicks += 1; },
    get clicks() { return clicks; },
  };
}

test("locator verification reports a missing session without guessing page state", async () => {
  const result = await invokeWorker(
    `/internal/v1/browser-sessions/${sessionId}/locators/verify`,
    { run_id: runId, run_case_id: runCaseId, browser: "chromium", attempt_token: attemptToken,
      locator: { by: "role", value: "button", name: "Save" }, job_token: jobToken() },
  );
  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 410);
  assert.equal(result.body.error, "BROWSER_SESSION_EXPIRED");
});

test("pre-dispatch locator failures expose zero or multiple matches", async () => {
  assert.equal(typeof workerModule.performAction, "function");
  for (const count of [0, 2]) {
    const button = target({ count });
    const result = await workerModule.performAction({
      actions: new Map(), claims: { attempt_token: attemptToken },
      page: { getByRole: () => button }, expiresAt: 0,
    }, { action_id: "2d599dcb-6ead-4b04-af85-2fb2d0046215", action: "click",
      locator: { by: "role", value: "button", name: "Save" } });
    assert.equal(result.error_code, "LOCATOR_NOT_UNIQUE");
    assert.equal(result.failure_phase, "locator_resolution");
    assert.equal(result.action_dispatched, false);
    assert.equal(result.match_count, count);
    assert.equal(button.clicks, 0);
  }
});

test("locator verification returns the descriptor for one visible control", async () => {
  const descriptor = { role: "button", name: "Save changes", label: "", placeholder: "" };
  const result = await workerModule.verifyLocator({
    page: { getByRole: () => target({ descriptor }) },
  }, { by: "role", value: "button", name: "Save changes" });
  assert.deepEqual(result, { status: "VERIFIED", match_count: 1, visible: true, descriptor });
});

test("locator verification refuses multiple or hidden targets", async () => {
  for (const input of [{ count: 2 }, { count: 1, visible: false }]) {
    const result = await workerModule.verifyLocator({
      page: { getByRole: () => target({ ...input, descriptor: { role: "button", name: "Save" } }) },
    }, { by: "role", value: "button", name: "Save" });
    assert.equal(result.status, "UNVERIFIED");
    assert.equal(result.visible && result.match_count === 1, false);
  }
});

test("recovery handoff rotates session claims once and rejects stale or replayed grants", () => {
  const oldClaims = {
    run_id: runId, run_case_id: runCaseId, browser: "chromium", attempt_token: attemptToken,
    worker_instance: "browser-worker", base_url: "https://example.com", allowed_domains: ["example.com"],
  };
  const session = { claims: oldClaims, actions: new Map(), expiresAt: 1000 };
  const { claims } = handoff();
  const first = workerModule.applyRecoveryHandoff(session, sessionId, claims, "browser-worker", 500);
  assert.equal(first.status, "READY");
  assert.equal(session.claims.attempt_token, claims.attempt_token);

  const replay = workerModule.applyRecoveryHandoff(session, sessionId, claims, "browser-worker", 501);
  assert.equal(replay.error_code, "SESSION_HANDOFF_INVALID");
  const staleSession = {
    claims: { ...oldClaims, attempt_token: "d5d4b0e5-8388-45a9-9b02-84df9a35193b" },
    actions: new Map(), expiresAt: 1000,
  };
  const stale = workerModule.applyRecoveryHandoff(staleSession, sessionId, claims, "browser-worker", 500);
  assert.equal(stale.error_code, "SESSION_HANDOFF_INVALID");
  assert.equal(staleSession.claims.attempt_token, "d5d4b0e5-8388-45a9-9b02-84df9a35193b");
});

test("action de-duplication is scoped to attempt and action ID", async () => {
  const session = { actions: new Map() };
  let executions = 0;
  const actionId = "2d599dcb-6ead-4b04-af85-2fb2d0046215";
  const invoke = () => workerModule.withActionDeduplication(
    session, attemptToken, actionId, async () => ({ call: ++executions }),
  );
  const duplicates = await Promise.all([invoke(), invoke()]);
  const nextAttempt = await workerModule.withActionDeduplication(
    session, "7c0fcb91-e0fc-4a62-8508-405d49d6d45d", actionId,
    async () => ({ call: ++executions }),
  );
  assert.deepEqual(duplicates, [{ call: 1 }, { call: 1 }]);
  assert.deepEqual(nextAttempt, { call: 2 });
  assert.equal(executions, 2);
});

test("rebind reports a missing session and does not create a new page", async () => {
  const { nextAttemptToken, claims } = handoff();
  const result = await invokeWorker(
    `/internal/v1/browser-sessions/${sessionId}/rebind`,
    { run_id: runId, run_case_id: runCaseId, browser: "chromium",
      attempt_token: nextAttemptToken, worker_instance: "browser-worker",
      job_token: jobToken(claims) },
  );
  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 200);
  assert.equal(result.body.status, "ERROR");
  assert.equal(result.body.error_code, "SESSION_NOT_RESTORABLE");
});

test("rebind refuses a missing session through the domain transition", () => {
  const { claims } = handoff();
  assert.deepEqual(
    workerModule.applyRecoveryHandoff(null, sessionId, claims, "browser-worker", 500),
    { status: "ERROR", error_code: "SESSION_NOT_RESTORABLE" },
  );
});
