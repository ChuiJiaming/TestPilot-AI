import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { IntegrationsPage } from "./IntegrationsPage";
import type { Organization, Project, SecretRef } from "./api";

const organization: Organization = {
  id: "org-1", name: "Northstar QA", slug: "northstar-qa", role: "org_admin",
};
const project: Project = {
  id: "project-1", organization_id: organization.id, name: "Storefront", slug: "storefront",
  created_by: "user-1", role: "owner",
};
const secretRef: SecretRef = {
  id: "secret-ref-1", organization_id: organization.id, provider: "vault",
  external_key: "prod/slack-webhook", rotated_at: null, created_at: "2026-11-01T00:00:00Z",
  active_version: 4,
};
const integration = {
  id: "integration-1", organization_id: organization.id, kind: "slack", enabled: true,
  config: { channel: "quality" }, secret_ref_id: secretRef.id,
  created_at: "2026-11-01T00:00:00Z",
};
const plan = { id: "plan-1", project_id: project.id, name: "Release suite", suite_id: "suite-1" };
const environment = { id: "environment-1", project_id: project.id, name: "Staging", base_url: "https://staging.example.test" };
const deadDelivery = {
  id: "delivery-1", integration_id: integration.id, event_id: "outbound:run.completed",
  status: "DEAD", attempts: 8, response_code: 503, next_attempt_at: null,
  created_at: "2026-11-01T00:00:00Z",
  attempt_history: [{ attempt: 8, status: "DEAD", response_status: 503, error_code: "HTTP_503", created_at: "2026-11-01T00:00:00Z" }],
};

type SetupOptions = { deliveries?: unknown[]; gateStatus?: "PENDING" | "PASS" | "FAIL" };

function setupFetch(options: SetupOptions = {}) {
  const requests: Array<{ url: string; method: string; body?: Record<string, unknown>; headers: Headers }> = [];
  let enabled = integration.enabled;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    requests.push({ url, method, body, headers });

    if (url.includes("/api/v1/integrations?") || url.includes("/api/v1/organizations/org-1/integrations")) {
      return Response.json({ items: [{ ...integration, enabled }], next_cursor: null });
    }
    if (method === "POST" && url.endsWith("/api/v1/integrations")) {
      return Response.json({ ...integration, id: "integration-2", ...body }, { status: 201 });
    }
    if (method === "PATCH" && url.endsWith("/api/v1/integrations/integration-1")) {
      enabled = Boolean(body?.enabled);
      return Response.json({ ...integration, enabled });
    }
    if (url.endsWith("/api/v1/secret-refs")) return Response.json([secretRef]);
    if (url.endsWith(`/api/v1/projects/${project.id}/plans`)) return Response.json({ items: [plan], next_cursor: null });
    if (url.includes(`/api/v1/projects/${project.id}/environments`)) return Response.json({ items: [environment], next_cursor: null });
    if (method === "POST" && url.endsWith(`/api/v1/webhooks/${integration.id}/deliveries/delivery-1/retry`)) {
      return Response.json({ ...deadDelivery, status: "PENDING", attempts: 0, queued: true }, { status: 202 });
    }
    if (url.includes(`/api/v1/webhooks/${integration.id}/deliveries`)) {
      return Response.json({ items: options.deliveries ?? [deadDelivery], next_cursor: null });
    }
    if (method === "POST" && url.endsWith("/api/v1/ci/trigger")) {
      return Response.json({ id: "run-1", project_id: project.id, status: "QUEUED", trigger_source: "ci" }, { status: 202 });
    }
    if (url.includes("/api/v1/test-runs/run-1/gate")) {
      return Response.json({ run_id: "run-1", status: options.gateStatus ?? "PENDING", blocking_severity: "HIGH", counts: {
        run_cases: 1, assertions_total: 2, assertions_evaluated: 1, assertions_passed: 1,
        assertions_failed: 0, assertions_pending: 1, blocking_failures: 0,
        cases_without_assertions: 0, cases_with_unknown_priority: 0,
      } });
    }
    throw new Error(`Unhandled request: ${method} ${url}`);
  }));
  return requests;
}

function renderPage() {
  return render(<IntegrationsPage
    organizations={[organization]} projects={[project]} project={project}
    onSelectProject={vi.fn()} accessToken="access-token"
  />);
}

describe("IntegrationsPage", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows SecretRef metadata without revealing secret values and toggles integration state", async () => {
    const requests = setupFetch();
    renderPage();
    expect(await screen.findByRole("button", { name: /Slack.*prod\/slack-webhook/ })).toBeInTheDocument();
    expect(screen.queryByText("plain-text-secret-value")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "停用集成" }));
    await waitFor(() => expect(requests.some((request) => request.method === "PATCH"
      && request.url.endsWith("/api/v1/integrations/integration-1")
      && request.body?.enabled === false)).toBe(true));
  });

  it("creates an integration with a SecretRef ID and never sends secret material", async () => {
    const requests = setupFetch();
    renderPage();
    await screen.findByRole("button", { name: /Slack.*prod\/slack-webhook/ });
    fireEvent.click(screen.getAllByRole("button", { name: "添加集成" })[0]);
    fireEvent.change(screen.getByLabelText("集成类型"), { target: { value: "slack" } });
    fireEvent.change(screen.getByLabelText("SecretRef"), { target: { value: secretRef.id } });
    fireEvent.click(screen.getAllByRole("button", { name: "添加集成" })[1]);

    await waitFor(() => expect(requests.some((request) => request.method === "POST"
      && request.url.endsWith("/api/v1/integrations")
      && request.body?.secret_ref_id === secretRef.id)).toBe(true));
    const created = requests.find((request) => request.method === "POST" && request.url.endsWith("/api/v1/integrations"));
    expect(JSON.stringify(created?.body)).not.toContain("plain-text-secret-value");
  });

  it("triggers CI with the selected environment and browser matrix, then shows the deterministic gate", async () => {
    const requests = setupFetch({ gateStatus: "PENDING" });
    renderPage();
    await screen.findByRole("button", { name: /Slack.*prod\/slack-webhook/ });
    fireEvent.change(screen.getByLabelText("仓库"), { target: { value: "acme/storefront" } });
    fireEvent.change(screen.getByLabelText("Commit SHA"), { target: { value: "abcdef1234567" } });
    fireEvent.change(screen.getByLabelText("触发意图"), { target: { value: "Validate release candidate" } });
    fireEvent.click(screen.getByLabelText("Firefox"));
    fireEvent.click(screen.getByRole("button", { name: "触发 CI Run" }));

    expect(await screen.findByText("PENDING")).toBeInTheDocument();
    const trigger = requests.find((request) => request.method === "POST" && request.url.endsWith("/api/v1/ci/trigger"));
    expect(trigger?.body).toMatchObject({
      project_id: project.id, plan_id: plan.id, environment_id: environment.id,
      repository: "acme/storefront", commit_sha: "abcdef1234567",
      browser_matrix: ["chromium", "firefox"], request_intent: "Validate release candidate",
    });
    expect(requests.some((request) => request.url.includes("/api/v1/test-runs/run-1/gate"))).toBe(true);
  });

  it("shows dead delivery attempts and queues a scoped retry", async () => {
    const requests = setupFetch();
    renderPage();
    expect(await screen.findByText("DEAD")).toBeInTheDocument();
    expect(screen.getByText("HTTP_503")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试投递" }));

    await waitFor(() => expect(requests.some((request) => request.method === "POST"
      && request.url.endsWith(`/api/v1/webhooks/${integration.id}/deliveries/delivery-1/retry`))).toBe(true));
  });
});
