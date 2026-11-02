import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QualityPage } from "./QualityPage";
import type { Project } from "./api";

const project: Project = {
  id: "project-1", organization_id: "org-1", name: "结算项目", slug: "billing",
  created_by: "user-1", role: "project_admin",
};
const secondProject: Project = {
  id: "project-2", organization_id: "org-1", name: "移动项目", slug: "mobile",
  created_by: "user-1", role: "project_admin",
};

const defect = {
  id: "defect-1", project_id: project.id, fingerprint: "fingerprint",
  title: "Run failed; review the available evidence", status: "OPEN",
  severity: "MEDIUM", classification: "ASSERTION",
  root_cause_status: "SUGGESTED", analysis: {
    hypothesis: "The assertion failed and needs human review.",
    evidence_status: "AVAILABLE", evidence_ids: ["artifact-1"], confidence: 0.4,
  }, occurrence_count: 1, created_at: "2026-10-30T00:00:00Z",
};

const baseline = {
  id: "baseline-1", project_id: project.id, case_version_id: "version-1",
  action_id: "action-1", environment_id: "env-1", browser: "chromium",
  viewport: { width: 1280, height: 720 }, artifact_id: "artifact-1",
  device_scale_factor: 1, color_scheme: "light", status: "PENDING",
  created_by: "user-1", mask_regions: [], max_pixel_ratio: 0.01,
  pixel_channel_tolerance: 0, comparison_profile: "pixel-v1", mask_hash: "hash",
  created_at: "2026-10-30T00:00:00Z",
};

function setupFetch() {
  const requests: Array<{ url: string; method: string; body?: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    requests.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.includes("/artifacts/") && url.endsWith("/download")) {
      return new Response(new Blob(["private image"]), { status: 200, headers: { "Content-Type": "image/png" } });
    }
    if (method === "PATCH" && url.endsWith("/defects/defect-1")) {
      return Response.json({ ...defect, status: "IN_PROGRESS", classification: "ASSERTION" });
    }
    if (method === "POST" && url.endsWith("/visual-baselines/baseline-1/decision")) {
      return Response.json({ ...baseline, status: "APPROVED" });
    }
    if (method === "POST" && url.endsWith("/visual-baselines/proposals")) {
      return Response.json({ ...baseline, id: "baseline-2" }, { status: 201 });
    }
    if (url.includes("/defects/defect-1/occurrences")) {
      return Response.json({ items: [{
        id: "occurrence-1", run_id: "run-1", run_case_id: "run-case-1",
        browser: "chromium", error_code: "ASSERTION_FAILED", evidence_status: "AVAILABLE",
        evidence: { artifact_ids: ["artifact-1"] }, observed_at: "2026-10-30T00:00:00Z",
      }], next_cursor: null });
    }
    if (url.endsWith("/defects/defect-1")) return Response.json(defect);
    if (url.includes("/projects/project-2/defects")) return Response.json({ items: [], next_cursor: null });
    if (url.includes("/projects/project-2/visual-baselines")) return Response.json({ items: [], next_cursor: null });
    if (url.includes("/projects/project-2/visual-diffs")) return Response.json({ items: [], next_cursor: null });
    if (url.includes("/projects/project-2/artifacts")) return Response.json({ items: [], next_cursor: null });
    if (url.includes("/projects/project-1/defects")) return Response.json({ items: [defect], next_cursor: null });
    if (url.includes("/visual-baselines")) return Response.json({ items: [baseline], next_cursor: null });
    if (url.includes("/visual-diffs")) return Response.json({ items: [{
      id: "diff-1", baseline_id: "baseline-1", run_case_id: "run-case-1",
      actual_artifact_id: "actual-1", diff_artifact_id: "diff-artifact-1",
      pixel_ratio: 0.25, changed_pixels: 1, compared_pixels: 4,
      status: "FAIL", error_code: null, created_at: "2026-10-30T00:00:00Z",
    }], next_cursor: null });
    if (url.includes("/artifacts")) return Response.json({ items: [{
      id: "artifact-1", project_id: project.id, run_id: "run-1", run_case_id: "run-case-1",
      action_id: "action-1", environment_id: "env-1", case_version_id: "version-1",
      browser: "chromium", artifact_type: "screenshot", content_type: "image/png",
      size_bytes: 512, redacted: true, viewport: { width: 1280, height: 720 },
      device_scale_factor: 1, color_scheme: "light", created_at: "2026-10-30T00:00:00Z",
    }], next_cursor: null });
    throw new Error(`Unhandled request: ${method} ${url}`);
  }));
  return requests;
}

describe("QualityPage", () => {
  beforeEach(() => {
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:private-image"), revokeObjectURL: vi.fn() });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("loads project defects and evidence, then saves human triage", async () => {
    const requests = setupFetch();
    render(<QualityPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access" currentUserId="user-1" />);

    expect(await screen.findByText("The assertion failed and needs human review.")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Run 截图证据" })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "实际运行截图" })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "像素差异图" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("缺陷状态"), { target: { value: "IN_PROGRESS" } });
    fireEvent.change(screen.getByLabelText("分诊原因"), { target: { value: "已分配负责人" } });
    fireEvent.click(screen.getByRole("button", { name: "保存分诊" }));

    await waitFor(() => expect(requests.some((request) => request.method === "PATCH"
      && request.url.endsWith("/defects/defect-1")
      && (request.body as { status?: string }).status === "IN_PROGRESS")).toBe(true));
  });

  it("requires a separate reviewer decision for a pending baseline", async () => {
    const requests = setupFetch();
    const view = render(<QualityPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access" currentUserId="user-1" />);
    expect(await screen.findByText("等待审批")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("候选创建者不能审批或拒绝");
    view.rerender(<QualityPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access" currentUserId="reviewer-2" />);
    fireEvent.change(screen.getByLabelText("基线审批原因"), { target: { value: "已核对页面状态" } });
    fireEvent.click(screen.getByRole("button", { name: "批准基线" }));

    await waitFor(() => expect(requests.some((request) => request.method === "POST"
      && request.url.endsWith("/visual-baselines/baseline-1/decision")
      && (request.body as { decision?: string }).decision === "APPROVE")).toBe(true));
  });

  it("creates a baseline proposal from a selected redacted screenshot", async () => {
    const requests = setupFetch();
    render(<QualityPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access" currentUserId="user-1" />);
    expect(await screen.findByLabelText("候选截图")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("候选截图"), { target: { value: "artifact-1" } });
    fireEvent.click(screen.getByRole("button", { name: "提交基线候选" }));

    await waitFor(() => expect(requests.some((request) => request.method === "POST"
      && request.url.endsWith("/visual-baselines/proposals")
      && (request.body as { artifact_id?: string }).artifact_id === "artifact-1")).toBe(true));
  });

  it("switches the quality view to the selected project and shows its empty state", async () => {
    setupFetch();
    const onSelectProject = vi.fn();
    const view = render(<QualityPage projects={[project, secondProject]} project={project} onSelectProject={onSelectProject} accessToken="access" currentUserId="user-1" />);
    expect(await screen.findByText("缺陷列表")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("项目"), { target: { value: secondProject.id } });
    expect(onSelectProject).toHaveBeenCalledWith(secondProject.id);
    view.rerender(<QualityPage projects={[project, secondProject]} project={secondProject} onSelectProject={onSelectProject} accessToken="access" currentUserId="user-1" />);
    expect(await screen.findByText("当前项目没有缺陷")).toBeInTheDocument();
  });

  it("shows a project-scoped loading error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network unavailable"); }));
    render(<QualityPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access" currentUserId="user-1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("network unavailable");
  });
});
