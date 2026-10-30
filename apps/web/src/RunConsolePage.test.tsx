import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunConsolePage } from "./RunConsolePage";

const project = {
  id: "project-1", organization_id: "org-1", name: "Checkout",
  slug: "checkout", created_by: "owner-1", role: "project_admin",
};

const run = {
  id: "run-1", project_id: project.id, environment_id: "env-1",
  environment_snapshot_id: "snapshot-1", parent_run_id: null,
  status: "WAITING_APPROVAL", branch: null, commit_sha: null,
  trigger_source: "manual", browser_matrix: ["chromium"],
  created_at: "2026-10-30T09:00:00Z", started_at: "2026-10-30T09:01:00Z",
  finished_at: null, latest_event_id: 0, event_replay_floor: 0,
};

const healing = {
  id: "healing-1", run_id: run.id, run_case_id: "run-case-1", step_id: "step-1",
  action_id: "action-1", sequence: 2, action_type: "click", browser: "chromium",
  old_locator: { by: "text", value: "Continue" },
  new_locator: { by: "role", value: "button", name: "Submit order" },
  source: "a11y_tree", confidence: 0.91,
  evidence: {
    verification: {
      match_count: 1, visible: true,
      descriptor: { role: "button", name: "Submit order" },
    },
    semantic_descriptor: { role: "button", name: "Submit order" },
  },
  status: "PENDING_APPROVAL",
  approval: {
    id: "approval-1", status: "PENDING", requested_by: "requester-1",
    decided_by: null, reason: null, expires_at: "2026-10-30T10:00:00Z",
  },
  expires_at: "2026-10-30T10:00:00Z", new_version_id: null,
  applied_at: null, created_at: "2026-10-30T09:02:00Z",
};

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listeners = new Map<string, Set<(event: Event) => void>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
    queueMicrotask(() => this.onopen?.(new Event("open")));
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const normalized = typeof listener === "function" ? listener : (event: Event) => listener.handleEvent(event);
    const listeners = this.listeners.get(type) ?? new Set<(event: Event) => void>();
    listeners.add(normalized);
    this.listeners.set(type, listeners);
  }

  dispatch(type: string, payload: Record<string, unknown>, id = 1) {
    const event = new MessageEvent(type, {
      data: JSON.stringify({ event_type: type, timestamp: "2026-10-30T09:03:00Z", payload }),
      lastEventId: String(id),
    });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  close() {}
}

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup({
  decisionStatus = 200,
  decisionBody = { ...healing, status: "APPROVED", approval: { ...healing.approval, status: "APPROVED", decided_by: "reviewer-1", reason: "Confirmed the unique target" } },
  refreshAfterConflict = false,
}: {
  decisionStatus?: number;
  decisionBody?: unknown;
  refreshAfterConflict?: boolean;
} = {}) {
  let healingReads = 0;
  const requests: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    requests.push({ url, init });
    if (url.endsWith("/test-runs?limit=50")) return response({ items: [run], next_cursor: null });
    if (url.endsWith("/projects/project-1/plans")) return response({ items: [{ id: "plan-1", name: "Checkout", policy: {} }] });
    if (url.endsWith("/projects/project-1/environments?limit=100")) return response({ items: [{ id: "env-1", name: "Staging" }] });
    if (url === "/api/v1/test-runs/run-1") return response(run);
    if (url === "/api/v1/test-runs/run-1/steps") return response({ items: [] });
    if (url === "/api/v1/test-runs/run-1/approvals?limit=100") return response({ items: [], next_cursor: null });
    if (url === "/api/v1/sse-tickets") return response({ run_id: run.id, expires_in: 60 });
    if (url === "/api/v1/healing-records/healing-1/decision") return response(decisionBody, decisionStatus);
    if (url === "/api/v1/healing-records/healing-1") {
      healingReads += 1;
      if (refreshAfterConflict && healingReads > 1) {
        return response({ ...healing, status: "EXPIRED", approval: { ...healing.approval, status: "EXPIRED" } });
      }
      return response(healing);
    }
    throw new Error(`Unexpected API request: ${url}`);
  });
  FakeEventSource.instances = [];
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("EventSource", FakeEventSource);
  render(<RunConsolePage project={project} accessToken="access-token" />);
  return { fetchMock, requests };
}

async function requestHealingReview() {
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  await act(async () => {
    FakeEventSource.instances[0].dispatch("run.healing_requested", { healing_id: healing.id });
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("RunConsolePage Healing review", () => {
  it("loads the scoped review and presents locator evidence after the Run event", async () => {
    const { requests } = setup();
    await requestHealingReview();

    expect(await screen.findByRole("heading", { name: "定位器恢复审阅" })).toBeInTheDocument();
    expect(screen.getByText("第 2 步 · click · chromium")).toBeInTheDocument();
    expect(screen.getByText("by: text · value: Continue", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText("by: role · value: button · name: Submit order", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText(/a11y_tree/)).toBeInTheDocument();
    expect(screen.getByText(/91%/)).toBeInTheDocument();
    expect(screen.getByText(/match_count/)).toBeInTheDocument();
    expect(requests.some(({ url }) => url === "/api/v1/healing-records/healing-1")).toBe(true);
  });

  it("requires a reason, sends one decision and disables terminal actions", async () => {
    const { requests } = setup();
    await requestHealingReview();

    const approve = await screen.findByRole("button", { name: "批准并继续" });
    expect(approve).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/决策说明/), {
      target: { value: "  Confirmed the unique target  " },
    });
    expect(approve).toBeEnabled();
    fireEvent.click(approve);

    await waitFor(() => expect(requests.some(({ url }) => url.endsWith("/decision"))).toBe(true));
    expect(await screen.findByText("定位器恢复已批准。Run 会重放失败动作并重新执行原断言。"))
      .toBeInTheDocument();
    const decisionRequest = requests.find(({ url }) => url.endsWith("/decision"));
    expect(decisionRequest?.url).toBe("/api/v1/healing-records/healing-1/decision");
    expect(decisionRequest?.init.method).toBe("POST");
    expect(JSON.parse(String(decisionRequest?.init.body))).toEqual({
      decision: "APPROVE", reason: "Confirmed the unique target",
    });
    expect(screen.getByRole("button", { name: "批准并继续" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "拒绝恢复" })).toBeDisabled();
    expect(requests.filter(({ url }) => url.endsWith("/decision"))).toHaveLength(1);
  });

  it("shows a 409 conflict and refreshes the terminal review state", async () => {
    setup({
      decisionStatus: 409,
      decisionBody: { detail: "Healing state conflict" },
      refreshAfterConflict: true,
    });
    await requestHealingReview();

    fireEvent.change(await screen.findByLabelText(/决策说明/), {
      target: { value: "Review before approval" },
    });
    fireEvent.click(screen.getByRole("button", { name: "批准并继续" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Healing state conflict");
    await waitFor(() => expect(screen.getByText("已过期")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "批准并继续" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "拒绝恢复" })).toBeDisabled();
  });
});
