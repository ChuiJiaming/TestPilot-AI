import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, jsonBody, type Project } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";

type Proposal = { proposal_id: string; title: string; priority: string; steps: Record<string, unknown>[]; assertions: Record<string, unknown>[]; requires_human_review: true; risk_tags: string[] };
type Job = { id: string; status: string; model_name: string; result: { proposals?: Proposal[] } | null; error_code: string | null; cost: number; attempts: number; automatic_retries: number };
type Suite = { id: string; name: string };
type Plan = { id: string; name: string; suite_id: string | null; policy: { concurrency_mode: "serial" | "bounded"; max_concurrent_runs: number } };
type Schedule = { id: string; cron_expression: string; timezone: string; enabled: boolean; next_fire_at: string | null };
type CaseItem = { id: string; title: string; status: string };
type SuiteCase = { case_id: string; title: string; sequence: number };
type Environment = { id: string; name: string };
type Tab = "generation" | "assets" | "schedules";

function messageOf(error: unknown) { return error instanceof Error ? error.message : "发生未知错误，请稍后重试。"; }

export function PlanningPage({ projects, project, onSelectProject, accessToken }: {
  projects: Project[]; project: Project | undefined; onSelectProject: (id: string) => void; accessToken: string;
}) {
  const [tab, setTab] = useState<Tab>("generation");
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]); const [requirement, setRequirement] = useState(""); const [modelName, setModelName] = useState("");
  const [jobCursor, setJobCursor] = useState<string | null>(null);
  const [suites, setSuites] = useState<Suite[]>([]); const [plans, setPlans] = useState<Plan[]>([]);
  const [cases, setCases] = useState<CaseItem[]>([]); const [environments, setEnvironments] = useState<Environment[]>([]);
  const [suiteCases, setSuiteCases] = useState<SuiteCase[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [scheduleEdits, setScheduleEdits] = useState<Record<string, { cron_expression: string; timezone: string }>>({});
  const [suiteId, setSuiteId] = useState(""); const [planId, setPlanId] = useState(""); const [caseId, setCaseId] = useState("");
  const [environmentId, setEnvironmentId] = useState(""); const [sequence, setSequence] = useState("0");
  const [suiteName, setSuiteName] = useState(""); const [planName, setPlanName] = useState("");
  const [description, setDescription] = useState(""); const [policy, setPolicy] = useState('{"concurrency_mode":"serial","max_concurrent_runs":1}');
  const [cron, setCron] = useState(""); const [timezone, setTimezone] = useState("UTC");
  const [scheduleReason, setScheduleReason] = useState(""); const [requestIntent, setRequestIntent] = useState("");

  const loadAssets = useCallback(async () => {
    if (!project) { setSuites([]); setPlans([]); setCases([]); setEnvironments([]); return; }
    try {
      const [s, p, c, e] = await Promise.all([
        api<{ items: Suite[] }>("/api/v1/projects/" + project.id + "/suites", {}, accessToken),
        api<{ items: Plan[] }>("/api/v1/projects/" + project.id + "/plans", {}, accessToken),
        api<{ items: CaseItem[] }>("/api/v1/projects/" + project.id + "/test-cases?limit=100", {}, accessToken),
        api<{ items: Environment[] }>("/api/v1/projects/" + project.id + "/environments?limit=100", {}, accessToken),
      ]);
      setSuites(s.items); setPlans(p.items); setCases(c.items); setEnvironments(e.items);
      setSuiteId((v) => s.items.some((x) => x.id === v) ? v : (s.items[0]?.id ?? ""));
      setPlanId((v) => p.items.some((x) => x.id === v) ? v : (p.items[0]?.id ?? ""));
      setCaseId((v) => c.items.some((x) => x.id === v) ? v : (c.items[0]?.id ?? ""));
      setEnvironmentId((v) => e.items.some((x) => x.id === v) ? v : (e.items[0]?.id ?? ""));
    } catch (caught) { setError(messageOf(caught)); }
  }, [accessToken, project]);
  const loadSchedules = useCallback(async () => {
    if (!planId) { setSchedules([]); return; }
    try { const result = await api<{ items: Schedule[] }>("/api/v1/plans/" + planId + "/schedules", {}, accessToken); setSchedules(result.items); }
    catch (caught) { setError(messageOf(caught)); }
  }, [accessToken, planId]);
  const loadJobs = useCallback(async (cursor?: string | null) => {
    if (!project) { setJobs([]); setJobCursor(null); return; }
    try {
      const query = new URLSearchParams({ limit: "50" }); if (cursor) query.set("cursor", cursor);
      const result = await api<{ items: Job[]; next_cursor: string | null }>(`/api/v1/projects/${project.id}/generations?${query}`, {}, accessToken);
      setJobs((current) => cursor ? [...current, ...result.items] : result.items);
      setJobCursor(result.next_cursor);
    } catch (caught) { setError(messageOf(caught)); }
  }, [accessToken, project]);
  const loadSuiteCases = useCallback(async () => {
    if (!suiteId) { setSuiteCases([]); return; }
    try {
      const result = await api<{ cases: SuiteCase[] }>(`/api/v1/suites/${suiteId}/cases`, {}, accessToken);
      setSuiteCases(result.cases);
    } catch (caught) { setError(messageOf(caught)); }
  }, [accessToken, suiteId]);
  useEffect(() => { void loadAssets(); setJobs([]); setJobCursor(null); void loadJobs(); }, [loadAssets, loadJobs]);
  useEffect(() => { void loadSchedules(); }, [loadSchedules]);
  useEffect(() => { void loadSuiteCases(); }, [loadSuiteCases]);

  async function createJob(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!project) return; setBusy(true); setError(""); setNotice("");
    try { const job = await api<Job>("/api/v1/projects/" + project.id + "/generations", jsonBody({ requirement, model_name: modelName }), accessToken); setJobs((v) => [job, ...v]); setRequirement(""); setNotice("任务已持久化，等待独立 Celery Agent Worker 消费。"); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function refreshJob(id: string) {
    try { const job = await api<Job>("/api/v1/generations/" + id, {}, accessToken); setJobs((v) => [job, ...v.filter((x) => x.id !== id)]); }
    catch (caught) { setError(messageOf(caught)); }
  }
  async function jobAction(id: string, action: "cancel" | "retry" | "accept", proposalIds?: string[]) {
    setBusy(true); setError(""); setNotice("");
    try {
      const body = action === "accept" ? { proposal_ids: proposalIds ?? [] } : {};
      const result = await api<Job | unknown>("/api/v1/generations/" + id + "/" + action, jsonBody(body), accessToken);
      if (action === "accept") setNotice("建议已采纳为 DSL 草稿；仍需独立审核和发布。");
      else if (result && typeof result === "object" && "status" in result) setJobs((v) => [result as Job, ...v.filter((x) => x.id !== id)]);
      if (action === "accept") await refreshJob(id);
    } catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function createSuite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!project) return; setBusy(true); setError("");
    try { const row = await api<Suite>("/api/v1/projects/" + project.id + "/suites", jsonBody({ name: suiteName, description }), accessToken); setSuiteName(""); setDescription(""); await loadAssets(); setSuiteId(row.id); setNotice("套件已创建。"); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function addCase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try { const result = await api<{ cases: SuiteCase[] }>("/api/v1/suites/" + suiteId + "/cases", jsonBody({ case_id: caseId, sequence: Number(sequence) }), accessToken); setSuiteCases(result.cases); setNotice("用例已按顺序关联到套件。"); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function createPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!project) return; setBusy(true); setError("");
    try { const parsed: unknown = JSON.parse(policy); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("policy 必须是 JSON 对象"); const row = await api<Plan>("/api/v1/projects/" + project.id + "/plans", jsonBody({ name: planName, suite_id: suiteId || null, policy: parsed }), accessToken); setPlanName(""); await loadAssets(); setPlanId(row.id); setNotice("执行计划已创建。"); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function createSchedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try { await api("/api/v1/plans/" + planId + "/schedules", jsonBody({ cron_expression: cron, timezone, environment_id: environmentId }), accessToken); setCron(""); await loadSchedules(); setNotice("调度已保存；Beat 按 5 字段 Cron 与 DST 规则扫描，到期后由 Worker 创建 Run。"); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function scheduleAction(row: Schedule, action: "enable" | "disable" | "trigger" | "delete") {
    setBusy(true); setError(""); setNotice("");
    try {
      if (action === "delete") await api("/api/v1/schedules/" + row.id, { method: "DELETE" }, accessToken);
      else if (action === "trigger") { await api("/api/v1/schedules/" + row.id + "/trigger", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ request_intent: requestIntent, reason: scheduleReason || undefined }) }, accessToken); setNotice("手动触发已写入 Outbox；Agent Worker 将创建 Run，本页面暂不展示 Run 执行状态。"); }
      else await api("/api/v1/schedules/" + row.id + "/" + action, jsonBody({ reason: scheduleReason }), accessToken);
      await loadSchedules();
    } catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function updateSchedule(event: FormEvent<HTMLFormElement>, row: Schedule) {
    event.preventDefault();
    const edit = scheduleEdits[row.id]; if (!edit) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/api/v1/schedules/" + row.id, { method: "PATCH", body: JSON.stringify(edit) }, accessToken);
      await loadSchedules(); setNotice("调度规则已更新；下次触发时间已重新计算。");
    } catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }

  return <div className="planning-workspace">
    <Card className="form-card"><div className="form-card-title"><div><CardTitle>第二阶段工作区</CardTitle><p>生成、测试资产与调度均限定在所选项目。</p></div></div><div className="environment-picker"><Label htmlFor="planning-project">项目</Label><select id="planning-project" className="input" value={project?.id ?? ""} onChange={(event) => onSelectProject(event.target.value)}><option value="">选择项目</option>{projects.map((row) => <option value={row.id} key={row.id}>{row.name}</option>)}</select></div><div className="planning-tabs" role="tablist"><button type="button" aria-selected={tab === "generation"} onClick={() => setTab("generation")}>AI 生成</button><button type="button" aria-selected={tab === "assets"} onClick={() => setTab("assets")}>套件与计划</button><button type="button" aria-selected={tab === "schedules"} onClick={() => setTab("schedules")}>调度</button></div></Card>
    {!project && <Card className="form-card">请先选择项目。</Card>}
    {project && tab === "generation" && <Card className="form-card"><div className="form-card-title"><div><CardTitle>自然语言场景生成</CardTitle><p>Agent 输出仅作为建议；所有提案风险标签暂为未分类，采纳后仍需人工审核和发布。单个任务最多自动重试 3 次。</p></div></div><form className="form-stack" onSubmit={createJob}><div><Label htmlFor="generation-requirement">测试需求</Label><textarea id="generation-requirement" className="input policy-editor" value={requirement} onChange={(e) => setRequirement(e.target.value)} required minLength={5} maxLength={12000} /></div><div><Label htmlFor="generation-model">模型名称</Label><Input id="generation-model" value={modelName} onChange={(e) => setModelName(e.target.value)} required /></div><Button disabled={busy}>创建生成任务</Button></form><div className="case-version-list">{jobs.map((job) => <article className="case-version-row" key={job.id}><div><b>{job.model_name}</b><span className="status-pill">{job.status}</span></div><small>{job.id} · 成本 {job.cost} · 总尝试 {job.attempts} · 自动重试 {job.automatic_retries}/3</small>{job.error_code && <small role="alert">{job.error_code}</small>}<div className="button-row"><Button variant="secondary" type="button" onClick={() => void refreshJob(job.id)}>刷新</Button>{(job.status === "QUEUED" || job.status === "RUNNING") && <Button variant="danger" type="button" disabled={busy} onClick={() => void jobAction(job.id, "cancel")}>取消</Button>}{job.status === "FAILED" && <Button type="button" disabled={busy} onClick={() => void jobAction(job.id, "retry")}>重试</Button>}{job.status === "SUCCEEDED" && <Button type="button" disabled={busy || !job.result?.proposals?.length} onClick={() => void jobAction(job.id, "accept", job.result?.proposals?.map((x) => x.proposal_id))}>采纳全部建议</Button>}</div>{job.result?.proposals?.map((proposal) => <article className="case-version-row" key={proposal.proposal_id}><b>{proposal.title}</b><small>风险：{proposal.risk_tags.join("、")} · 需要人工审核</small><pre>{JSON.stringify(proposal, null, 2)}</pre></article>)}</article>)}</div>{jobCursor && <Button variant="secondary" type="button" onClick={() => void loadJobs(jobCursor)}>加载更多历史任务</Button>}</Card>}
    {project && tab === "assets" && <div className="planning-grid"><Card className="form-card"><div className="form-card-title"><div><CardTitle>测试套件</CardTitle><p>按顺序关联项目中的用例。</p></div></div><form className="form-stack" onSubmit={createSuite}><div><Label htmlFor="suite-name">名称</Label><Input id="suite-name" value={suiteName} onChange={(e) => setSuiteName(e.target.value)} required /></div><div><Label htmlFor="suite-desc">说明</Label><Input id="suite-desc" value={description} onChange={(e) => setDescription(e.target.value)} /></div><Button disabled={busy}>创建套件</Button></form><div className="environment-picker"><Label htmlFor="suite-select">套件</Label><select id="suite-select" className="input" value={suiteId} onChange={(e) => setSuiteId(e.target.value)}>{suites.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select></div><form className="form-stack" onSubmit={addCase}><div><Label htmlFor="suite-case">用例</Label><select id="suite-case" className="input" value={caseId} onChange={(e) => setCaseId(e.target.value)}>{cases.map((row) => <option key={row.id} value={row.id}>{row.title} · {row.status}</option>)}</select></div><div><Label htmlFor="suite-seq">顺序</Label><Input id="suite-seq" type="number" min="0" value={sequence} onChange={(e) => setSequence(e.target.value)} /></div><Button disabled={busy || !suiteId || !caseId}>添加用例</Button></form><div className="suite-case-list" aria-live="polite">{suiteCases.length ? suiteCases.map((row) => <div className="suite-case-item" key={row.case_id}><span>{row.sequence + 1}. {row.title}</span><small>{row.case_id}</small></div>) : <p className="empty-inline">该套件暂无用例。</p>}</div></Card><Card className="form-card"><div className="form-card-title"><div><CardTitle>执行计划</CardTitle><p>默认串行执行，每个计划最多 1 个活动 Run。当前策略会随计划保存；并发执行限制将在 M09 Run 执行器接入时强制执行。</p></div></div><form className="form-stack" onSubmit={createPlan}><div><Label htmlFor="plan-name">名称</Label><Input id="plan-name" value={planName} onChange={(e) => setPlanName(e.target.value)} required /></div><div><Label htmlFor="plan-suite">关联套件</Label><select id="plan-suite" className="input" value={suiteId} onChange={(e) => setSuiteId(e.target.value)}><option value="">不关联</option>{suites.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select></div><div><Label htmlFor="plan-policy">Policy JSON</Label><textarea id="plan-policy" className="input policy-editor" value={policy} onChange={(e) => setPolicy(e.target.value)} /></div><Button disabled={busy}>创建计划</Button></form><div className="access-token-list">{plans.map((row) => <button type="button" className="case-list-item" key={row.id} onClick={() => setPlanId(row.id)}><b>{row.name}</b><small>{row.suite_id ?? "未关联套件"} · {row.policy?.concurrency_mode ?? "serial"} · 活动 Run 上限 {row.policy?.max_concurrent_runs ?? 1}</small></button>)}</div></Card></div>}
    {project && tab === "schedules" && <div className="planning-grid"><Card className="form-card"><div className="form-card-title"><div><CardTitle>调度配置</CardTitle><p>仅接受 5 字段 Cron；DST 缺口跳过，重复时间只执行一次。</p></div></div><div className="environment-picker"><Label htmlFor="schedule-plan">计划</Label><select id="schedule-plan" className="input" value={planId} onChange={(e) => setPlanId(e.target.value)}>{plans.map((row) => <option value={row.id} key={row.id}>{row.name}</option>)}</select></div><form className="form-stack" onSubmit={createSchedule}><div><Label htmlFor="schedule-cron">Cron 表达式（分 时 日 月 周）</Label><Input id="schedule-cron" value={cron} onChange={(e) => setCron(e.target.value)} required /></div><div><Label htmlFor="schedule-timezone">IANA 时区</Label><Input id="schedule-timezone" value={timezone} onChange={(e) => setTimezone(e.target.value)} required /></div><div><Label htmlFor="schedule-env">执行环境</Label><select id="schedule-env" className="input" value={environmentId} onChange={(e) => setEnvironmentId(e.target.value)}>{environments.map((row) => <option value={row.id} key={row.id}>{row.name}</option>)}</select></div><Button disabled={busy || !planId || !environmentId}>保存调度</Button></form></Card><Card className="form-card"><div className="form-card-title"><div><CardTitle>启停与手动触发</CardTitle><p>触发写入 Outbox，由 Worker 创建 Run。</p></div><Button variant="secondary" type="button" onClick={() => void loadSchedules()}>刷新</Button></div><div className="case-review-tools"><Label htmlFor="schedule-reason">操作原因（启停必填）</Label><Input id="schedule-reason" value={scheduleReason} onChange={(e) => setScheduleReason(e.target.value)} /></div><div className="case-review-tools"><Label htmlFor="schedule-intent">手动触发说明</Label><Input id="schedule-intent" value={requestIntent} onChange={(e) => setRequestIntent(e.target.value)} /></div><div className="access-token-list">{schedules.map((row) => <article className="case-version-row" key={row.id}><div><b>{row.cron_expression} · {row.timezone}</b><span className="status-pill">{row.enabled ? "启用" : "暂停"}</span></div><small>下次执行：{row.next_fire_at ?? "调度已暂停"}</small><form className="form-stack" onSubmit={(event) => void updateSchedule(event, row)}><div><Label htmlFor={`schedule-cron-${row.id}`}>Cron</Label><Input id={`schedule-cron-${row.id}`} value={(scheduleEdits[row.id] ?? { cron_expression: row.cron_expression, timezone: row.timezone }).cron_expression} onChange={(e) => setScheduleEdits((current) => ({ ...current, [row.id]: { ...(current[row.id] ?? { cron_expression: row.cron_expression, timezone: row.timezone }), cron_expression: e.target.value } }))} required /></div><div><Label htmlFor={`schedule-timezone-${row.id}`}>IANA 时区</Label><Input id={`schedule-timezone-${row.id}`} value={(scheduleEdits[row.id] ?? { cron_expression: row.cron_expression, timezone: row.timezone }).timezone} onChange={(e) => setScheduleEdits((current) => ({ ...current, [row.id]: { ...(current[row.id] ?? { cron_expression: row.cron_expression, timezone: row.timezone }), timezone: e.target.value } }))} required /></div><Button variant="secondary" disabled={busy}>更新调度规则</Button></form><div className="button-row"><Button variant="secondary" type="button" disabled={busy || !scheduleReason} onClick={() => void scheduleAction(row, row.enabled ? "disable" : "enable")}>{row.enabled ? "暂停" : "恢复"}</Button><Button type="button" disabled={busy || !requestIntent} onClick={() => void scheduleAction(row, "trigger")}>手动触发</Button><Button variant="danger" type="button" disabled={busy} onClick={() => void scheduleAction(row, "delete")}>删除</Button></div></article>)}</div></Card></div>}
    {error && <div className="alert alert-error page-alert" role="alert">{error}</div>}{notice && <div className="alert alert-success page-alert" role="status">{notice}</div>}
  </div>;
}
