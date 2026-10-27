import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EnvironmentPage, LoginScreen, MembersPage } from "./App";

describe("LoginScreen", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.unstubAllGlobals());

  it("requires the account fields and offers registration and email verification", () => {
    render(<LoginScreen onAuthenticated={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "欢迎回来" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建新账户" }));
    expect(screen.getByLabelText("姓名")).toBeRequired();
    expect(screen.getByLabelText("工作邮箱")).toHaveAttribute("type", "email");
    expect(screen.getByLabelText("密码")).toHaveAttribute("minLength", "12");
  });
});

describe("MembersPage", () => {
  it("loads project members and sends the selected role to the scoped endpoint", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ user_id: "member-1", email: "alice@example.test", display_name: "Alice", role: "tester" }], next_cursor: null }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ user_id: "member-2", organization_id: "org-1", email: "bob@example.test", display_name: "Bob", role: "org_member", joined_at: "2026-10-26T00:00:00Z" }], next_cursor: null }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ user_id: "member-2", project_id: "project-1", email: "bob@example.test", display_name: "Bob", role: "developer" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ user_id: "member-1", email: "alice@example.test", display_name: "Alice", role: "tester" }, { user_id: "member-2", email: "bob@example.test", display_name: "Bob", role: "developer" }], next_cursor: null }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const project = { id: "project-1", organization_id: "org-1", name: "Checkout", slug: "checkout", created_by: "admin-1", role: "project_admin" };
    render(<MembersPage project={project} accessToken="access-token" />);

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("组织成员"), { target: { value: "member-2" } });
    fireEvent.change(screen.getByLabelText("项目角色"), { target: { value: "developer" } });
    fireEvent.click(screen.getByRole("button", { name: /保存角色/ }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    const [url, init] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(url).toContain("/api/v1/projects/project-1/members/member-2");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ role: "developer" });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer access-token");
  });
});

describe("EnvironmentPage", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.unstubAllGlobals());

  it("creates a project environment and reads its immutable snapshots", async () => {
    const environment = {
      id: "env-1", project_id: "project-1", name: "staging", base_url: "https://staging.example.test",
      secret_ref_id: null, network_policy: { allowed_hosts: ["staging.example.test"] }, created_at: "2026-10-26T00:00:00Z",
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(environment), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{ id: "snapshot-1", base_url: environment.base_url, snapshot_hash: "a".repeat(64), created_at: "2026-10-26T00:00:00Z", secret_ref_version_id: null }]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const project = { id: "project-1", organization_id: "org-1", name: "Checkout", slug: "checkout", created_by: "admin-1", role: "project_admin" };
    render(<EnvironmentPage project={project} accessToken="access-token" />);

    fireEvent.change(await screen.findByLabelText("环境名称"), { target: { value: "staging" } });
    fireEvent.change(screen.getByLabelText("目标 URL"), { target: { value: environment.base_url } });
    fireEvent.change(screen.getByLabelText("网络策略 JSON"), { target: { value: JSON.stringify(environment.network_policy) } });
    fireEvent.click(screen.getByRole("button", { name: /创建环境/ }));

    expect(await screen.findByText("staging", { selector: "b" })).toBeInTheDocument();
    const [createUrl, createInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(createUrl).toContain("/api/v1/projects/project-1/environments");
    expect(JSON.parse(String(createInit.body))).toEqual({ name: "staging", base_url: environment.base_url, network_policy: environment.network_policy, secret_ref_id: null });
    fireEvent.click(screen.getByRole("button", { name: "读取快照" }));
    expect(await screen.findByText(/未关联 SecretRef/)).toBeInTheDocument();
    const [snapshotUrl] = fetchMock.mock.calls[3] as [string, RequestInit];
    expect(snapshotUrl).toBe("/api/v1/environments/env-1/snapshots");
  });
});
