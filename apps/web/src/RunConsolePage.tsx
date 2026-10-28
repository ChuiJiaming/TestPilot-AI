import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Activity, AlertCircle, Check, CirclePause, Play, RefreshCw, Square, Wifi, WifiOff } from "lucide-react";
import { ApiError, api, jsonBody, type Project } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import "./run-console.css";

type RunStatus = "QUEUED" | "PREPARING" | "RUNNING" | "WAITING_APPROVAL" | "PAUSING" | "PAUSED" | "FINALIZING" | "PASSED" | "FAILED" | "ERROR" | "CANCELLING" | "CANCELLED";
type RunItem = {
  id: string; project_id: string; environment_id: string; environment_snapshot_id: string | null;
  parent_run_id: string | null; status: RunStatus; branch: string | null; commit_sha: string | null;
  trigger_source: string; browser_matrix: string[]; created_at: string; started_at: string | null;
  finished_at: string | null; latest_event_id: number; event_replay_floor: number;
};
type Plan = { id: string; name: string; policy: { browser_matrix?: string[] } };
type Environment = { id: string; name: string };
type StepItem = { id: string; run_case_id: string; action_id: string; sequence: number; action_type: string; status: string; replay_policy: string; browser: string | null; started_at: string | null; finished_at: string | null };
type RunEvent = { id: number; event_type: string; timestamp: string; payload: Record<string, unknown> };
type ApprovalItem = { id: string; project_id: string; target_kind: string; target_id: string; status: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "CANCELLED"; requested_by: string | null; kind: string | null; expires_at: string | null };

const terminalStatuses = new Set<RunStatus>(["PASSED", "FAILED", "ERROR", "CANCELLED"]);
const statusLabels: Record<RunStatus, string> = {
  QUEUED: "排队中", PREPARING: "准备中", RUNNING: "运行中",
  WAITING_APPROVAL: "等待人工处理", PAUSING: "正在暂停", PAUSED: "已暂停",
  FINALIZING: "正在收尾", PASSED: "通过", FAILED: "失败", ERROR: "错误",
  CANCELLING: "正在取消", CANCELLED: "已取消",
};
const eventTypes = [
  "run.queued", "run.started", "run.status_changed", "run.finished",
  "run.case_status_changed", "run.step_started", "run.step_finished",
  "run.lease_recovered", "run.manual_reconciliation_required", "run.waiting_approval",
  "approval.requested", "approval.decided", "run.step_reconciled", "run.resume_claimed",
];

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

export function RunConsolePage({ project, accessToken }: { project: Project | undefined; accessToken: string }) {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [selectedRun, setSelectedRun] = useState<RunItem | null>(null);
  const [steps, setSteps] = useState<StepItem[]>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [cursor, setCursor] = useState(0);
  const [connection, setConnection] = useState<"idle" | "connecting" | "live" | "reconnecting" | "closed">("idle");
  const [planId, setPlanId] = useState("");
  const [environmentId, setEnvironmentId] = useState("");
  const [requestIntent, setRequestIntent] = useState("");
  const [approvalReason, setApprovalReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const sourceRef = useRef<EventSource | null>(null);
  const cursorRef = useRef(0);

  const loadRuns = useCallback(async () => {
    if (!project) { setRuns([]); setSelectedId(""); return; }
    const result = await api<{ items: RunItem[]; next_cursor: string | null }>(
      "/api/v1/projects/" + project.id + "/test-runs?limit=50", {}, accessToken);
    setRuns(result.items);
    setSelectedId((current) => current && result.items.some((run) => run.id === current)
      ? current : (result.items[0]?.id ?? ""));
  }, [accessToken, project]);

  const loadAssets = useCallback(async () => {
    if (!project) { setPlans([]); setEnvironments([]); return; }
    const [planResult, environmentResult] = await Promise.all([
      api<{ items: Plan[] }>("/api/v1/projects/" + project.id + "/plans", {}, accessToken),
      api<{ items: Environment[] }>("/api/v1/projects/" + project.id + "/environments?limit=100", {}, accessToken),
    ]);
    setPlans(planResult.items);
    setEnvironments(environmentResult.items);
    setPlanId((current) => current && planResult.items.some((item) => item.id === current)
      ? current : (planResult.items[0]?.id ?? ""));
    setEnvironmentId((current) => current && environmentResult.items.some((item) => item.id === current)
      ? current : (environmentResult.items[0]?.id ?? ""));
  }, [accessToken, project]);

  const loadRun = useCallback(async (id: string) => {
    const value = await api<RunItem>("/api/v1/test-runs/" + id, {}, accessToken);
    setSelectedRun(value);
    return value;
  }, [accessToken]);

  const loadSteps = useCallback(async (id: string) => {
    const value = await api<{ items: StepItem[] }>("/api/v1/test-runs/" + id + "/steps", {}, accessToken);
    setSteps(value.items);
  }, [accessToken]);

  const loadApprovals = useCallback(async (id: string) => {
    const value = await api<{ items: ApprovalItem[]; next_cursor: string | null }>(
      "/api/v1/test-runs/" + id + "/approvals?limit=100", {}, accessToken);
    setApprovals(value.items);
  }, [accessToken]);

  useEffect(() => {
    let active = true;
    setError("");
    Promise.all([loadRuns(), loadAssets()]).catch((caught) => {
      if (active) setError(messageOf(caught));
    });
    return () => { active = false; };
  }, [loadAssets, loadRuns]);

  useEffect(() => {
    cursorRef.current = 0;
    setCursor(0);
    setEvents([]);
    if (!selectedId) {
      setSelectedRun(null); setSteps([]); setApprovals([]); setConnection("idle");
      return;
    }
    let active = true;
    void Promise.all([loadRun(selectedId), loadSteps(selectedId), loadApprovals(selectedId)]).catch((caught) => {
      if (active) setError(messageOf(caught));
    });
    return () => { active = false; };
  }, [loadApprovals, loadRun, loadSteps, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    let retryTimer: number | undefined;
    let currentSource: EventSource | undefined;
    setConnection("connecting");

    const connect = async () => {
      try {
        const currentRun = await loadRun(selectedId);
        if (terminalStatuses.has(currentRun.status)) { setConnection("closed"); return; }
        await api<{ run_id: string; expires_in: number }>(
          "/api/v1/sse-tickets", jsonBody({ run_id: selectedId }), accessToken);
        if (!active) return;
        const url = "/api/v1/test-runs/" + selectedId + "/events?after_id=" + cursorRef.current;
        currentSource = new EventSource(url, { withCredentials: true });
        sourceRef.current = currentSource;
        currentSource.onopen = () => { if (active) setConnection("live"); };
        const onRunEvent = (raw: Event) => {
          if (!active) return;
          const event = raw as MessageEvent<string>;
          const eventId = Number(event.lastEventId);
          if (!Number.isFinite(eventId) || eventId <= cursorRef.current) return;
          cursorRef.current = eventId;
          setCursor(eventId);
          try {
            const envelope = JSON.parse(event.data) as { event_type: string; timestamp: string; payload: Record<string, unknown> };
            setEvents((items) => [{
              id: eventId, event_type: envelope.event_type,
              timestamp: envelope.timestamp, payload: envelope.payload,
            }, ...items].slice(0, 100));
          } catch {
            setEvents((items) => [{
              id: eventId, event_type: "run.event", timestamp: new Date().toISOString(), payload: {},
            }, ...items].slice(0, 100));
          }
          void Promise.all([loadRun(selectedId), loadSteps(selectedId), loadApprovals(selectedId)]).then(([currentRun]) => {
            if (terminalStatuses.has(currentRun.status)) {
              setConnection("closed");
              currentSource?.close();
            }
          }).catch((caught) => setError(messageOf(caught)));
        };
        for (const type of eventTypes) currentSource.addEventListener(type, onRunEvent);
        currentSource.addEventListener("error", (raw: Event) => {
          const event = raw as MessageEvent<string>;
          if (event.data?.includes("SNAPSHOT_REQUIRED")) setNotice("事件游标已过期，正在刷新 Run 快照。");
        });
        currentSource.onerror = () => {
          currentSource?.close();
          if (!active) return;
          setConnection("reconnecting");
          void loadRun(selectedId).then(async (run) => {
             await Promise.all([loadSteps(selectedId), loadApprovals(selectedId)]);
            if (terminalStatuses.has(run.status)) { setConnection("closed"); return; }
            if (cursorRef.current < (run.event_replay_floor || 0)) {
              setNotice("事件游标已过期，已刷新 Run 快照。");
            }
            cursorRef.current = Math.max(cursorRef.current, run.latest_event_id || 0);
            setCursor(cursorRef.current);
            retryTimer = window.setTimeout(() => { void connect(); }, 1200);
          }).catch((caught) => setError(messageOf(caught)));
        };
      } catch (caught) {
        if (!active) return;
        setConnection("reconnecting");
        setError(messageOf(caught));
        retryTimer = window.setTimeout(() => { void connect(); }, 2500);
      }
    };

    void connect();
    return () => {
      active = false;
      if (retryTimer) window.clearTimeout(retryTimer);
      currentSource?.close();
      sourceRef.current = null;
    };
  }, [accessToken, loadApprovals, loadRun, loadSteps, selectedId]);

  const selectedPlan = useMemo(() => plans.find((item) => item.id === planId), [planId, plans]);
  const selectedEnvironment = useMemo(() => environments.find((item) => item.id === environmentId), [environmentId, environments]);

  async function createRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project || !planId || !environmentId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<RunItem>("/api/v1/test-runs", {
        ...jsonBody({
          project_id: project.id,
          plan_id: planId,
          environment_id: environmentId,
          browser_matrix: selectedPlan?.policy?.browser_matrix?.length ? selectedPlan.policy.browser_matrix : ["chromium"],
          trigger_source: "manual",
          request_intent: requestIntent || undefined,
        }),
        headers: { "Idempotency-Key": crypto.randomUUID() },
      }, accessToken);
      setNotice("Run 已写入队列，等待 Agent Worker admission。");
      setRequestIntent("");
      await loadRuns();
      setSelectedId(created.id);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function control(action: "pause" | "resume" | "cancel" | "retry") {
    if (!selectedRun) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const reason = action === "pause" ? "用户请求暂停" : action === "resume" ? "用户请求恢复"
        : action === "cancel" ? "用户请求取消" : "用户请求重试";
      const result = await api<RunItem | { run_id: string; status: string }>(
        "/api/v1/test-runs/" + selectedRun.id + "/" + action,
        jsonBody({ reason }), accessToken,
      );
      setNotice(action === "retry" ? "重试 Run 已创建。" : "Run 控制请求已保存。");
      await loadRuns();
      if (action === "retry" && "run_id" in result) setSelectedId(result.run_id);
      else await loadRun(selectedRun.id);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function decideApproval(approval: ApprovalItem, decision: "APPROVE" | "REJECT") {
    if (!selectedRun || approval.status !== "PENDING") return;
    if (!approvalReason.trim()) { setError("请填写审批决策说明。"); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      await api<ApprovalItem>("/api/v1/approvals/" + approval.id + "/decide", jsonBody({
        decision, reason: approvalReason.trim(),
      }), accessToken);
      setApprovalReason("");
      await Promise.all([loadRun(selectedRun.id), loadRuns(), loadSteps(selectedRun.id), loadApprovals(selectedRun.id)]);
      setNotice(decision === "APPROVE"
        ? "审批已通过，Run 将恢复；已核实的非幂等步骤不会重放。"
        : "审批已拒绝，Run 已失败结束。");
    } catch (caught) {
      setError(messageOf(caught));
      if (caught instanceof ApiError && caught.status === 409) {
        await Promise.all([loadRun(selectedRun.id), loadSteps(selectedRun.id), loadApprovals(selectedRun.id)]);
      }
    } finally { setBusy(false); }
  }

  const pendingApprovals = approvals.filter((approval) => approval.status === "PENDING");

  if (!project) return <Card><CardTitle>运行控制台</CardTitle><p className="run-muted">先选择项目，再查看 Run。</p></Card>;

  return <>
    <div className="run-console-grid">
      <Card className="run-create-card">
        <div className="run-card-heading"><div><div className="eyebrow muted">NEW EXECUTION</div><CardTitle>创建 Run</CardTitle></div><Activity size={20} /></div>
        {plans.length === 0 || environments.length === 0
          ? <p className="run-muted">需要至少一个执行计划和一个测试环境。</p>
          : <form className="run-create-form" onSubmit={createRun}>
            <div><Label htmlFor="run-plan">执行计划</Label><select id="run-plan" className="input" value={planId} onChange={(event) => setPlanId(event.target.value)} required>{plans.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
            <div><Label htmlFor="run-environment">测试环境</Label><select id="run-environment" className="input" value={environmentId} onChange={(event) => setEnvironmentId(event.target.value)} required>{environments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
            <div><Label htmlFor="run-intent">运行说明（可选）</Label><Input id="run-intent" value={requestIntent} onChange={(event) => setRequestIntent(event.target.value)} maxLength={4000} placeholder="例如：验证本次发布的登录流程" /></div>
            <Button type="submit" disabled={busy || !planId || !environmentId}>创建 Run</Button>
          </form>}
      </Card>

      <Card className="run-list-card">
        <div className="run-card-heading"><div><div className="eyebrow muted">PROJECT RUNS</div><CardTitle>最近运行</CardTitle></div><Button type="button" variant="ghost" className="icon-button" aria-label="刷新 Run" onClick={() => void loadRuns()}><RefreshCw size={16} /></Button></div>
        {runs.length === 0
          ? <div className="run-empty"><Activity size={20} /><span>这个项目还没有 Run</span></div>
          : <div className="run-list">{runs.map((run) => <button key={run.id} type="button" className={"run-list-item " + (selectedId === run.id ? "selected" : "")} onClick={() => setSelectedId(run.id)}>
            <span className={"run-status-dot status-" + run.status.toLowerCase()} />
            <span className="run-list-main"><strong>{statusLabels[run.status]}</strong><small>{run.browser_matrix.join(", ")} · {new Date(run.created_at).toLocaleString()}</small></span>
            <span className="run-list-id">{run.id.slice(0, 8)}</span>
          </button>)}</div>}
      </Card>
    </div>

    {error && <div className="alert alert-error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="alert alert-success" role="status"><Check size={16} />{notice}</div>}

    {selectedRun && <div className="run-detail-grid">
      <Card className="run-detail-card">
        <div className="run-detail-heading">
          <div><div className="eyebrow muted">RUN DETAIL</div><CardTitle>{statusLabels[selectedRun.status]}</CardTitle><small>{selectedRun.id}</small></div>
          <div className={"run-connection " + connection}><span className="run-connection-icon">{connection === "live" ? <Wifi size={15} /> : connection === "closed" ? <WifiOff size={15} /> : <RefreshCw size={15} />}</span>{connection === "live" ? "实时连接" : connection === "reconnecting" ? "正在重连" : "连接中"}</div>
        </div>
        <div className="run-meta-grid">
          <div><small>浏览器</small><strong>{selectedRun.browser_matrix.join(", ")}</strong></div>
          <div><small>触发来源</small><strong>{selectedRun.trigger_source}</strong></div>
          <div><small>开始时间</small><strong>{selectedRun.started_at ? new Date(selectedRun.started_at).toLocaleString() : "—"}</strong></div>
          <div><small>完成时间</small><strong>{selectedRun.finished_at ? new Date(selectedRun.finished_at).toLocaleString() : "—"}</strong></div>
        </div>
        <div className="run-controls">
          {selectedRun.status === "RUNNING" && <Button type="button" variant="secondary" disabled={busy} onClick={() => void control("pause")}><CirclePause size={15} />暂停</Button>}
          {selectedRun.status === "PAUSED" && <Button type="button" variant="secondary" disabled={busy} onClick={() => void control("resume")}><Play size={15} />恢复</Button>}
          {!terminalStatuses.has(selectedRun.status) && <Button type="button" variant="danger" disabled={busy || selectedRun.status === "CANCELLING"} onClick={() => void control("cancel")}><Square size={14} />取消</Button>}
          {terminalStatuses.has(selectedRun.status) && <Button type="button" variant="secondary" disabled={busy} onClick={() => void control("retry")}><RefreshCw size={15} />重试</Button>}
        </div>
        {selectedRun.status === "WAITING_APPROVAL" && <>
          <div className="run-manual-notice"><AlertCircle size={16} />核实非幂等步骤的结果后再决定；批准表示该副作用已人工核实，恢复时不会再次执行。</div>
          {pendingApprovals.map((approval) => <div className="run-approval-actions" key={approval.id}>
            <small>{approval.kind ?? approval.target_kind} · {approval.expires_at
              ? `审批截止 ${new Date(approval.expires_at).toLocaleString()}`
              : "无过期时间"}</small>
            <Label htmlFor="run-approval-reason">决策说明</Label>
            <Input id="run-approval-reason" value={approvalReason} required
              onChange={(event) => setApprovalReason(event.target.value)} />
            <div className="run-controls">
              <Button type="button" variant="secondary" disabled={busy || !approvalReason.trim()}
                onClick={() => void decideApproval(approval, "APPROVE")}>批准并恢复</Button>
              <Button type="button" variant="danger" disabled={busy || !approvalReason.trim()}
                onClick={() => void decideApproval(approval, "REJECT")}>拒绝并结束</Button>
            </div>
          </div>)}
          {pendingApprovals.length === 0 && <small>未找到待审批记录，请刷新 Run 状态。</small>}
        </>}
      </Card>

      <Card className="run-steps-card">
        <div className="run-card-heading"><div><div className="eyebrow muted">DETERMINISTIC EXECUTION</div><CardTitle>步骤与浏览器</CardTitle></div><span className="run-step-count">{steps.length} steps</span></div>
        {steps.length === 0 ? <div className="run-empty"><span>等待执行步骤写入</span></div> : <div className="run-step-list">{steps.map((step) => <div className="run-step-row" key={step.id}>
          <span className={"run-step-status status-" + step.status.toLowerCase()}>{step.status}</span><span className="run-step-action">{step.action_type}</span><span className="run-step-browser">{step.browser ?? "browser pending"}</span><small>{step.sequence}</small>
        </div>)}</div>}
      </Card>

      <Card className="run-events-card">
        <div className="run-card-heading"><div><div className="eyebrow muted">DURABLE EVENT LOG</div><CardTitle>事件流</CardTitle></div><small>cursor {cursor}</small></div>
        {events.length === 0 ? <div className="run-empty"><span>等待 Run 事件</span></div> : <div className="run-event-list">{events.map((event) => <div className="run-event-row" key={event.id}>
          <span>#{event.id}</span><strong>{event.event_type}</strong><time>{new Date(event.timestamp).toLocaleTimeString()}</time>
        </div>)}</div>}
      </Card>
    </div>}
  </>;
}
