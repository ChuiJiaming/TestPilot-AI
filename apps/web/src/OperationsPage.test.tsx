import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OperationsPage } from "./OperationsPage";
import type { Organization } from "./api";

const organization: Organization = {
  id: "org-1", name: "账务组织", slug: "billing", role: "org_admin",
};
const memberOrganization: Organization = { ...organization, role: "org_member" };

const notification = {
  id: "notification-1", kind: "run.failed", payload: { run_id: "run-1" },
  read_at: null, created_at: "2026-11-02T08:00:00Z",
};

const pendingChange = {
  id: "change-1", key: "maintenance_mode", status: "PENDING",
  previous_value_hash: "a".repeat(64), proposed_value_hash: "b".repeat(64),
  requested_by: "admin-1", approved_by: null, reason: "Scheduled maintenance",
  created_at: "2026-11-02T08:00:00Z", decided_at: null,
};

type RequestRecord = {
  url: string; method: string; headers: Headers; body?: Record<string, unknown>;
};

function setupFetch(options: {
  platformAdmin?: boolean; decisionStatus?: number; decisionDetail?: string;
} = {}) {
  const requests: RequestRecord[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    requests.push({ url, method, headers, body });

    if (url.endsWith("/organizations/org-1/quotas") && method === "GET") {
      return Response.json({
        organization_id: "org-1", max_concurrent_runs: 3, monthly_token_limit: 30,
        monthly_tokens_committed: 20, monthly_tokens_reserved: 10,
        monthly_tokens_remaining: 0, quota_exhausted: true,
        period_month: "2026-11-01", updated_at: "2026-11-02T08:00:00Z",
      });
    }
    if (url.endsWith("/organizations/org-1/quotas") && method === "PUT") {
      return Response.json({
        organization_id: "org-1", max_concurrent_runs: body?.max_concurrent_runs,
        monthly_token_limit: body?.monthly_token_limit, monthly_tokens_committed: 20,
        monthly_tokens_reserved: 10, monthly_tokens_remaining: 70,
        quota_exhausted: false, period_month: "2026-11-01", updated_at: "2026-11-02T08:00:00Z",
      });
    }
    if (url.endsWith("/organizations/org-1/usage")) {
      return Response.json({ items: [{
        organization_id: "org-1", metric_name: "provider_tokens", value: 20, unit: "token",
      }] });
    }
    if (url.startsWith("/api/v1/organizations/org-1/audit-logs")) {
      return Response.json({ items: [{
        id: 1, organization_id: "org-1", actor_user_id: "user-1",
        action: "case.updated", resource_type: "test_case", resource_id: "case-1",
        metadata: { version: 3 }, previous_hash: null, entry_hash: "c".repeat(64),
        created_at: "2026-11-02T08:00:00Z",
      }], next_cursor: null });
    }
    if (url.includes("/audit-logs/verify")) {
      return Response.json({ valid: true, entries_checked: 4,
        first_invalid_entry_id: null, expected_hash: null, actual_hash: null });
    }
    if (url.endsWith("/audit-logs/export") && method === "POST") {
      return Response.json({ filename: "audit-org-1.csv", media_type: "text/csv",
        content: "id,action\n1,case.updated\n", next_cursor: null });
    }
    if (url.endsWith("/notifications?limit=100")) {
      return Response.json({ items: [notification], next_cursor: null });
    }
    if (url.endsWith("/notifications/preferences") && method === "GET") {
      return Response.json({ items: [
        { channel: "in_app", event_type: "run.failed", enabled: true },
        { channel: "email", event_type: "run.failed", enabled: true },
        { channel: "webhook", event_type: "run.failed", enabled: false },
      ] });
    }
    if (url.endsWith("/notifications/preferences") && method === "PUT") {
      return Response.json({ items: body?.items ?? [] });
    }
    if (url.endsWith("/notifications/notification-1/read") && method === "POST") {
      return Response.json({ id: "notification-1", read: true });
    }
    if (url.includes("/notification-deliveries?status=DEAD")) {
      return Response.json({ items: [{
        delivery_id: "delivery-1", notification_id: "notification-1", channel: "email",
        status: "DEAD", attempts: 8, error_code: "SMTP_REJECTED",
        created_at: "2026-11-02T08:00:00Z",
      }], next_cursor: null });
    }
    if (url === "/api/v1/system-settings" && method === "GET") {
      if (!options.platformAdmin) return Response.json({ detail: "Platform administrator access required" }, { status: 403 });
      return Response.json({ items: [{ key: "maintenance_mode", value_hash: "a".repeat(64),
        updated_at: "2026-11-02T08:00:00Z" }], pending_changes: [pendingChange] });
    }
    if (url === "/api/v1/system-settings" && method === "PUT") {
      return Response.json({ ...pendingChange, key: body?.key, status: "PENDING" });
    }
    if (url.endsWith("/admin/system-setting-changes/change-1/decision") && method === "POST") {
      if (options.decisionStatus) {
        return Response.json({ detail: options.decisionDetail ?? "Change conflict" }, { status: options.decisionStatus });
      }
      return Response.json({ ...pendingChange, status: body?.decision === "approve" ? "APPROVED" : "REJECTED", approved_by: "admin-2" });
    }
    throw new Error(`Unhandled request: ${method} ${url}`);
  }));
  return requests;
}

describe("OperationsPage", () => {
  beforeEach(() => {
    vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:audit"), revokeObjectURL: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows committed and reserved quota when provider calls are blocked", async () => {
    setupFetch();
    render(<OperationsPage organizations={[organization]} accessToken="access-token" currentUserId="admin-1" />);

    expect(await screen.findByRole("status", { name: "模型调用额度已耗尽" })).toBeInTheDocument();
    expect(screen.getByText("已结算 20 / 30 tokens" )).toBeInTheDocument();
    expect(screen.getByText("预留 10 tokens" )).toBeInTheDocument();
  });

  it("updates notification preferences and marks the owner's notification read", async () => {
    const requests = setupFetch();
    render(<OperationsPage organizations={[memberOrganization]} accessToken="access-token" currentUserId="member-1" />);
    fireEvent.click(await screen.findByRole("tab", { name: "通知与投递" }));

    expect(await screen.findByText("SMTP_REJECTED")).toBeInTheDocument();
    const emailToggle = screen.getByRole("checkbox", { name: "run.failed · email" });
    fireEvent.click(emailToggle);
    fireEvent.click(screen.getByRole("button", { name: "保存通知偏好" }));
    fireEvent.click(screen.getByRole("button", { name: "标记为已读" }));

    await waitFor(() => expect(requests.some((request) =>
      request.method === "PUT" && request.url.endsWith("/notifications/preferences"),
    )).toBe(true));
    await waitFor(() => expect(requests.some((request) =>
      request.method === "POST" && request.url.endsWith("/notifications/notification-1/read"),
    )).toBe(true));
    expect(await screen.findByText("已读")).toBeInTheDocument();
  });

  it("filters, verifies, and exports organization audit entries", async () => {
    const requests = setupFetch();
    render(<OperationsPage organizations={[organization]} accessToken="access-token" currentUserId="admin-1" />);
    fireEvent.click(await screen.findByRole("tab", { name: "审计日志" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "筛选" })).not.toBeDisabled());
    fireEvent.change(screen.getByLabelText("操作类型"), { target: { value: "case.updated" } });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    expect(await screen.findByText("当前筛选：case.updated")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "验证审计链" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "验证审计链" }));

    expect(await screen.findByText("链验证通过 · 4 条记录")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "导出审计日志" }));

    await waitFor(() => expect(requests.some((request) =>
      request.method === "POST" && request.url.endsWith("/audit-logs/export")
        && request.body?.organization_id === "org-1",
    )).toBe(true));
    expect(requests.some((request) => request.url.includes("action=case.updated"))).toBe(true);
  });

  it("lets a second platform administrator approve a pending change by digest", async () => {
    const requests = setupFetch({ platformAdmin: true });
    render(<OperationsPage organizations={[organization]} accessToken="access-token" currentUserId="admin-2" />);
    fireEvent.click(await screen.findByRole("tab", { name: "平台设置" }));

    expect(await screen.findByText("Scheduled maintenance")).toBeInTheDocument();
    expect(screen.getAllByText("a".repeat(64)).length).toBeGreaterThanOrEqual(1);
    fireEvent.change(screen.getByLabelText("审批理由"), { target: { value: "Reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "批准变更" }));

    await waitFor(() => expect(requests.some((request) =>
      request.method === "POST" && request.url.endsWith("/admin/system-setting-changes/change-1/decision")
        && request.body?.decision === "approve" && request.body?.reason === "Reviewed",
    )).toBe(true));
  });

  it("prevents self approval", async () => {
    setupFetch({ platformAdmin: true });
    render(<OperationsPage organizations={[organization]} accessToken="access-token" currentUserId="admin-1" />);
    fireEvent.click(await screen.findByRole("tab", { name: "平台设置" }));

    const approve = await screen.findByRole("button", { name: "批准变更" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("发起人不能审批自己的变更" )).toBeInTheDocument();
  });

  it("shows a stale setting proposal conflict", async () => {
    setupFetch({ platformAdmin: true, decisionStatus: 409, decisionDetail: "Current setting changed; proposal is stale" });
    render(<OperationsPage organizations={[organization]} accessToken="access-token" currentUserId="admin-2" />);
    fireEvent.click(await screen.findByRole("tab", { name: "平台设置" }));
    fireEvent.change(await screen.findByLabelText("审批理由"), { target: { value: "Reviewed" } });
    fireEvent.click(await screen.findByRole("button", { name: "批准变更" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Current setting changed; proposal is stale");
  });

  it("shows a denied platform setting decision", async () => {
    setupFetch({ platformAdmin: true, decisionStatus: 403, decisionDetail: "Platform administrator access required" });
    render(<OperationsPage organizations={[organization]} accessToken="access-token" currentUserId="admin-2" />);
    fireEvent.click(await screen.findByRole("tab", { name: "平台设置" }));
    fireEvent.change(await screen.findByLabelText("审批理由"), { target: { value: "Reviewed" } });
    fireEvent.click(await screen.findByRole("button", { name: "批准变更" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Platform administrator access required");
  });
});
