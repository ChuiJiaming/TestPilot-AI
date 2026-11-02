import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReportsPage } from "./ReportsPage";
import type { Project } from "./api";

const project: Project = {
  id: "project-1", organization_id: "org-1", name: "账务项目", slug: "billing",
  created_by: "user-1", role: "project_admin",
};
const secondProject: Project = {
  id: "project-2", organization_id: "org-1", name: "移动项目", slug: "mobile",
  created_by: "user-1", role: "project_admin",
};

const run = {
  id: "run-1", project_id: project.id, environment_id: "environment-1",
  status: "PASSED", branch: "release/1.2", commit_sha: "abc1234",
  trigger_source: "ci", browser_matrix: ["chromium"],
  created_at: "2026-11-02T08:00:00Z", finished_at: "2026-11-02T08:00:02Z",
};
const report = {
  id: "report-1", run_id: run.id,
  summary: { run: { status: "PASSED", branch: "release/1.2", commit_sha: "abc1234" },
    summary: { counts: { total: 1, passed: 1, failed: 0 } } },
  artifact_references: [], next_cursor: null,
};

type RequestRecord = { url: string; method: string; headers: Headers; body?: Record<string, unknown> };

function setupFetch(options: { reportStatus?: number; expiredArtifact?: boolean; exportStatuses?: string[] } = {}) {
  const requests: RequestRecord[] = [];
  let exportStatusIndex = 0;
  let exportCount = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    requests.push({ url, method, headers, body });

    if (url.endsWith("/test-runs?limit=100")) {
      const projectId = url.includes("project-2") ? secondProject.id : project.id;
      return Response.json({ items: projectId === project.id ? [run] : [], next_cursor: null });
    }
    if (url.endsWith("/test-runs/run-1/report")) {
      if (options.reportStatus) return Response.json({ detail: "Report access denied" }, { status: options.reportStatus });
      return Response.json({
        ...report,
        artifact_references: options.expiredArtifact
          ? [{ id: "expired-1", artifact_type: "trace", status: "EXPIRED_ARTIFACT" }]
          : [],
      });
    }
    if (method === "POST" && url.endsWith("/reports/report-1/exports")) {
      exportCount += 1;
      exportStatusIndex = 0;
      return Response.json({
        id: `export-${exportCount}`, run_id: run.id, format: body?.format,
        status: "QUEUED", artifact_id: null,
      }, { status: 202 });
    }
    if (url.includes("/report-exports/export-")) {
      const statuses = options.exportStatuses ?? ["QUEUED", "RUNNING", "SUCCEEDED"];
      const status = statuses[Math.min(exportStatusIndex++, statuses.length - 1)];
      return Response.json({
        id: "export-1", run_id: run.id, format: "pdf", status,
        artifact_id: status === "SUCCEEDED" ? "artifact-report-1" : null,
      });
    }
    if (url.endsWith("/artifacts/artifact-report-1/download")) {
      return new Response(new Blob(["private report"]), {
        status: 200, headers: { "Content-Type": "application/pdf" },
      });
    }
    throw new Error(`Unhandled request: ${method} ${url}`);
  }));
  return requests;
}

describe("ReportsPage", () => {
  beforeEach(() => {
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:private-report"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("loads a report, polls an export, and downloads bytes with the access token", async () => {
    const requests = setupFetch();
    render(<ReportsPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access-token" />);

    expect(await screen.findByText("release/1.2")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("导出格式"), { target: { value: "pdf" } });
    fireEvent.click(screen.getByRole("button", { name: "生成导出" }));

    expect(await screen.findByText("导出已完成", {}, { timeout: 5000 })).toBeInTheDocument();
    const createRequest = requests.find((request) => request.method === "POST");
    expect(createRequest?.body).toEqual({ format: "pdf" });
    expect(createRequest?.headers.get("Idempotency-Key")).toHaveLength(36);
    fireEvent.click(screen.getByRole("button", { name: "下载报告" }));

    await waitFor(() => expect(requests.some((request) =>
      request.url.endsWith("/artifacts/artifact-report-1/download")
      && request.headers.get("Authorization") === "Bearer access-token")).toBe(true));
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:private-report"));
  });

  it("shows a failed export and allows retry with a new idempotency key", async () => {
    const requests = setupFetch({ exportStatuses: ["FAILED"] });
    render(<ReportsPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access-token" />);
    expect(await screen.findByText("release/1.2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成导出" }));
    expect(await screen.findByText("导出失败，可重试")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试导出" }));
    await waitFor(() => expect(requests.filter((request) => request.method === "POST")).toHaveLength(2));
    const keys = requests.filter((request) => request.method === "POST")
      .map((request) => request.headers.get("Idempotency-Key"));
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("labels expired artifact references without hiding the report", async () => {
    setupFetch({ expiredArtifact: true });
    render(<ReportsPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access-token" />);

    expect(await screen.findByText("EXPIRED_ARTIFACT")).toBeInTheDocument();
    expect(screen.getByText("release/1.2")).toBeInTheDocument();
  });

  it("switches projects and clears the previous report", async () => {
    setupFetch();
    const onSelectProject = vi.fn();
    const view = render(<ReportsPage projects={[project, secondProject]} project={project} onSelectProject={onSelectProject} accessToken="access-token" />);
    expect(await screen.findByText("release/1.2")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("项目"), { target: { value: secondProject.id } });
    expect(onSelectProject).toHaveBeenCalledWith(secondProject.id);
    view.rerender(<ReportsPage projects={[project, secondProject]} project={secondProject} onSelectProject={onSelectProject} accessToken="access-token" />);
    expect(await screen.findByText("当前项目没有可查看的运行报告")).toBeInTheDocument();
    expect(screen.queryByText("release/1.2")).not.toBeInTheDocument();
  });

  it("shows a clear access-denied state for a 403 response", async () => {
    setupFetch({ reportStatus: 403 });
    render(<ReportsPage projects={[project]} project={project} onSelectProject={vi.fn()} accessToken="access-token" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Report access denied");
  });
});
