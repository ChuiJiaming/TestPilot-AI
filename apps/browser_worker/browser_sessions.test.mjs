import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { Readable } from "node:stream";
import test from "node:test";

import {
  assertTarget,
  captureScreenshot,
  handleBrowserWorkerRequest,
  publicAddress,
  resolvePinned,
} from "./browser_sessions.mjs";

const secret = "phase3-worker-test-secret";
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

function actionPayload(overrides = {}) {
  return {
    run_id: runId,
    run_case_id: runCaseId,
    browser: "chromium",
    attempt_token: attemptToken,
    action: { action_id: "2d599dcb-6ead-4b04-af85-2fb2d0046215", action: "click" },
    job_token: jobToken(),
    ...overrides,
  };
}

test("Worker screenshot capture masks form controls and returns bounded viewport metadata", async () => {
  let screenshotOptions;
  const mainFrameMask = { locator: "main-frame-form-controls" };
  const childFrameMask = { locator: "child-frame-form-controls" };
  const frames = [mainFrameMask, childFrameMask].map((mask) => ({
    locator(selector) {
      assert.equal(selector, "input,textarea,select,[contenteditable='true']");
      return mask;
    },
  }));
  const page = {
    frames: () => frames,
    viewportSize: () => ({ width: 1280, height: 720 }),
    evaluate: async () => ({ device_scale_factor: 1, color_scheme: "light" }),
    screenshot: async (options) => {
      screenshotOptions = options;
      return Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg==",
        "base64",
      );
    },
  };

  const result = await captureScreenshot({ page });

  assert.deepEqual(screenshotOptions.mask, [mainFrameMask, childFrameMask]);
  assert.equal(screenshotOptions.type, "png");
  assert.equal(result.content_type, "image/png");
  assert.deepEqual(result.viewport, { width: 1280, height: 720 });
  assert.equal(result.device_scale_factor, 1);
  assert.equal(result.color_scheme, "light");
  assert.equal(Buffer.from(result.image_base64, "base64").length, 70);
});

test("Worker rejects an action scoped to another RunCase", async () => {
  const result = await invokeWorker(
    `/internal/v1/browser-sessions/${sessionId}/actions`,
    actionPayload({ run_case_id: "e85740a4-070d-4a95-861f-a39fcb90e4a2" }),
  );

  assert.equal(result.handled, true);
  assert.equal(result.statusCode, 403);
  assert.equal(result.body.error, "JOB_TOKEN_SCOPE_MISMATCH");
});

test("Worker rejects an action scoped to another attempt", async () => {
  const result = await invokeWorker(
    `/internal/v1/browser-sessions/${sessionId}/actions`,
    actionPayload({ attempt_token: "7992b23d-9501-4e38-8fc6-ff865ae101dc" }),
  );

  assert.equal(result.statusCode, 403);
  assert.equal(result.body.error, "JOB_TOKEN_SCOPE_MISMATCH");
});

test("Worker reports lost session state instead of silently creating a replacement", async () => {
  const result = await invokeWorker("/internal/v1/browser-sessions/create", {
    session_id: sessionId,
    run_id: runId,
    run_case_id: runCaseId,
    browser: "chromium",
    attempt_token: attemptToken,
    resume_only: true,
    job_token: jobToken(),
  });

  assert.equal(result.statusCode, 200);
  assert.equal(result.body.status, "ERROR");
  assert.equal(result.body.error_code, "SESSION_NOT_RESTORABLE");
});

test("egress policy rejects IP literals and hosts outside the exact allowlist", async () => {
  const claims = { allowed_domains: ["example.com"] };

  await assert.rejects(
    assertTarget("http://127.0.0.1", claims),
    (error) => error.code === "IP_LITERAL_TARGET_BLOCKED",
  );
  await assert.rejects(
    assertTarget("https://private.example.net/path", claims),
    (error) => error.code === "DOMAIN_NOT_ALLOWLISTED",
  );
  await assert.rejects(
    assertTarget("https://sub.example.com/path", claims),
    (error) => error.code === "DOMAIN_NOT_ALLOWLISTED",
  );
});

test("egress policy rejects private, loopback, link-local, and IPv6 local addresses", () => {
  for (const address of [
    "127.0.0.1", "10.20.30.40", "192.168.1.1", "169.254.169.254",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
  ]) {
    assert.equal(publicAddress(address), false, `${address} must not be public egress`);
  }
  assert.equal(publicAddress("8.8.8.8"), true);
  assert.equal(publicAddress("2606:4700:4700::1111"), true);
});

test("DNS pinning rejects any private answer and rechecks each resolution", async () => {
  await assert.rejects(
    resolvePinned("example.com", async () => [
      { address: "8.8.8.8", family: 4 },
      { address: "10.0.0.12", family: 4 },
    ]),
    (error) => error.code === "DNS_ADDRESS_BLOCKED",
  );

  let lookupCount = 0;
  const lookup = async () => {
    lookupCount += 1;
    return [{
      address: lookupCount === 1 ? "8.8.8.8" : "169.254.169.254",
      family: 4,
    }];
  };
  assert.equal((await resolvePinned("example.com", lookup)).address, "8.8.8.8");
  await assert.rejects(
    resolvePinned("example.com", lookup),
    (error) => error.code === "DNS_ADDRESS_BLOCKED",
  );
  assert.equal(lookupCount, 2);
});
