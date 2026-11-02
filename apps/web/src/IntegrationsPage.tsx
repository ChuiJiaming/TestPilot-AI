import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, jsonBody, type Organization, type Project, type SecretRef } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";

type Page<T> = { items: T[]; next_cursor: string | null };
type Integration = {
  id: string; organization_id: string; kind: "slack" | "discord"; enabled: boolean;
  config: Record<string, unknown>; secret_ref_id: string | null; created_at: string;
};
type Environment = { id: string; project_id: string; name: string; base_url: string };
type Plan = { id: string; project_id: string; name: string; suite_id: string | null };
type DeliveryAttempt = {
  attempt: number; status: "SUCCESS" | "RETRY" | "DEAD"; response_status: number | null;
  error_code: string | null; created_at: string;
};
type Delivery = {
  id: string; integration_id: string; event_id: string;
  status: "PENDING" | "SUCCEEDED" | "FAILED" | "DEAD"; attempts: number;
  response_code: number | null; next_attempt_at: string | null; created_at: string;
  attempt_history: DeliveryAttempt[]; queued?: boolean;
};
type Run = { id: string; project_id: string; status: string; trigger_source: string };
type Gate = {
  run_id: string; status: "PENDING" | "PASS" | "FAIL"; blocking_severity: string;
  counts: { assertions_total: number; assertions_evaluated: number; assertions_pending: number; blocking_failures: number };
};
type Browser = "chromium" | "firefox" | "webkit";

const browsers: Array<{ id: Browser; label: string }> = [
  { id: "chromium", label: "Chromium" },
  { id: "firefox", label: "Firefox" },
  { id: "webkit", label: "WebKit" },
];

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

export function IntegrationsPage({ organizations, projects, project, onSelectProject, accessToken }: {
  organizations: Organization[]; projects: Project[]; project: Project | undefined;
  onSelectProject: (id: string) => void; accessToken: string;
}) {
  const [organizationId, setOrganizationId] = useState(organizations[0]?.id ?? "");
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [secretRefs, setSecretRefs] = useState<SecretRef[]>([]);
  const [selectedIntegrationId, setSelectedIntegrationId] = useState("");
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [planId, setPlanId] = useState("");
  const [environmentId, setEnvironmentId] = useState("");
  const [kind, setKind] = useState<Integration["kind"]>("slack");
  const [secretRefId, setSecretRefId] = useState("");
  const [repository, setRepository] = useState("");
  const [commitSha, setCommitSha] = useState("");
  const [branch, setBranch] = useState("");
  const [requestIntent, setRequestIntent] = useState("");
  const [selectedBrowsers, setSelectedBrowsers] = useState<Browser[]>(["chromium"]);
  const [run, setRun] = useState<Run | null>(null);
  const [gate, setGate] = useState<Gate | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingDeliveries, setLoadingDeliveries] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const organizationRequest = useRef(0);
  const deliveryRequest = useRef(0);
  const visibleProjects = useMemo(
    () => projects.filter((item) => item.organization_id === organizationId),
    [organizationId, projects],
  );
  const visibleSecretRefs = useMemo(
    () => secretRefs.filter((item) => item.organization_id === organizationId),
    [organizationId, secretRefs],
  );
  const selectedIntegration = integrations.find((item) => item.id === selectedIntegrationId) ?? null;
  const selectedSecretRef = visibleSecretRefs.find((item) => item.id === secretRefId);

  useEffect(() => {
    if (!organizations.some((item) => item.id === organizationId)) {
      setOrganizationId(organizations[0]?.id ?? "");
    }
  }, [organizationId, organizations]);

  const loadOrganizationData = useCallback(async () => {
    const requestId = ++organizationRequest.current;
    setIntegrations([]); setSecretRefs([]); setSelectedIntegrationId(""); setDeliveries([]);
    setError(""); setNotice("");
    if (!organizationId) { setLoading(false); return; }
    setLoading(true);
    try {
      const query = encodeURIComponent(organizationId);
      const [integrationPage, refs] = await Promise.all([
        api<Page<Integration>>(`/api/v1/integrations?organization_id=${query}&limit=100`, {}, accessToken),
        api<SecretRef[]>("/api/v1/secret-refs", {}, accessToken),
      ]);
      if (requestId !== organizationRequest.current) return;
      const scopedRefs = refs.filter((item) => item.organization_id === organizationId);
      setIntegrations(integrationPage.items);
      setSecretRefs(scopedRefs);
      setSecretRefId((current) => scopedRefs.some((item) => item.id === current)
        ? current : (scopedRefs[0]?.id ?? ""));
      setSelectedIntegrationId((current) => integrationPage.items.some((item) => item.id === current)
        ? current : (integrationPage.items[0]?.id ?? ""));
    } catch (caught) {
      if (requestId === organizationRequest.current) setError(messageOf(caught));
    } finally {
      if (requestId === organizationRequest.current) setLoading(false);
    }
  }, [accessToken, organizationId]);

  useEffect(() => { void loadOrganizationData(); }, [loadOrganizationData]);

  const loadDeliveries = useCallback(async () => {
    const requestId = ++deliveryRequest.current;
    setDeliveries([]);
    if (!selectedIntegration) { setLoadingDeliveries(false); return; }
    setLoadingDeliveries(true);
    try {
      const result = await api<Page<Delivery>>(
        `/api/v1/webhooks/${selectedIntegration.id}/deliveries?limit=100`, {}, accessToken,
      );
      if (requestId === deliveryRequest.current) setDeliveries(result.items);
    } catch (caught) {
      if (requestId === deliveryRequest.current) setError(messageOf(caught));
    } finally {
      if (requestId === deliveryRequest.current) setLoadingDeliveries(false);
    }
  }, [accessToken, selectedIntegration]);

  useEffect(() => { void loadDeliveries(); }, [loadDeliveries]);

  useEffect(() => {
    let active = true;
    setPlans([]); setEnvironments([]); setPlanId(""); setEnvironmentId(""); setRun(null); setGate(null);
    if (!project) return;
    Promise.all([
      api<Page<Plan>>(`/api/v1/projects/${project.id}/plans`, {}, accessToken),
      api<Page<Environment>>(`/api/v1/projects/${project.id}/environments?limit=100`, {}, accessToken),
    ]).then(([planPage, environmentPage]) => {
      if (!active) return;
      setPlans(planPage.items); setEnvironments(environmentPage.items);
      setPlanId(planPage.items[0]?.id ?? ""); setEnvironmentId(environmentPage.items[0]?.id ?? "");
    }).catch((caught: unknown) => {
      if (active) setError(messageOf(caught));
    });
    return () => { active = false; };
  }, [accessToken, project]);

  async function createIntegration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!organizationId || !secretRefId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<Integration>("/api/v1/integrations", jsonBody({
        organization_id: organizationId, kind, config: {}, secret_ref_id: secretRefId, enabled: true,
      }), accessToken);
      setIntegrations((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelectedIntegrationId(created.id);
      setNotice("集成已创建。浏览器只保存 SecretRef ID，不接收或保存密钥值。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function toggleIntegration(item: Integration) {
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await api<Integration>(`/api/v1/integrations/${item.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !item.enabled }),
      }, accessToken);
      setIntegrations((current) => current.map((row) => row.id === updated.id ? updated : row));
      setNotice(updated.enabled ? "集成已启用。" : "集成已停用。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function loadGate(runId: string) {
    setError("");
    try {
      const result = await api<Gate>(`/api/v1/test-runs/${runId}/gate?blocking_severity=HIGH`, {}, accessToken);
      setGate(result);
    } catch (caught) { setError(messageOf(caught)); }
  }

  async function triggerCI(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project || !planId || !environmentId || !selectedBrowsers.length) return;
    setBusy(true); setError(""); setNotice(""); setGate(null);
    try {
      const created = await api<Run>("/api/v1/ci/trigger", jsonBody({
        project_id: project.id, plan_id: planId, environment_id: environmentId,
        repository: repository.trim(), commit_sha: commitSha.trim(), branch: branch.trim() || null,
        browser_matrix: browsers.map(({ id }) => id).filter((id) => selectedBrowsers.includes(id)),
        request_intent: requestIntent.trim(),
      }), accessToken);
      setRun(created);
      setNotice("CI Run 已排入持久化 Outbox；门禁状态依据已保存的断言结果计算。");
      await loadGate(created.id);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  function toggleBrowser(browser: Browser) {
    setSelectedBrowsers((current) => current.includes(browser)
      ? current.filter((item) => item !== browser) : [...current, browser]);
  }

  async function retryDelivery(delivery: Delivery) {
    setBusy(true); setError(""); setNotice("");
    try {
      const queued = await api<Delivery>(
        `/api/v1/webhooks/${delivery.integration_id}/deliveries/${delivery.id}/retry`,
        { method: "POST" }, accessToken,
      );
      setDeliveries((current) => current.map((item) => item.id === queued.id
        ? { ...item, ...queued, attempt_history: item.attempt_history } : item));
      setNotice("重试已排队；原始尝试记录已保留。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  return <div className="integrations-workspace">
    <Card className="form-card integrations-scope-card">
      <div className="integrations-scope-copy"><span className="integrations-kicker">PHASE 5 · M14</span><CardTitle>集成、CI 与 Webhook</CardTitle><p>组织级凭据引用和项目级 CI 触发分开授权。门禁仅读取持久化断言结果。</p></div>
      <div className="integrations-scope-controls">
        <div><Label htmlFor="integration-organization">组织范围</Label><select id="integration-organization" className="input" value={organizationId} onChange={(event) => { const nextId = event.target.value; setOrganizationId(nextId); onSelectProject(projects.find((item) => item.organization_id === nextId)?.id ?? ""); }}><option value="">选择组织</option>{organizations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
        <div><Label htmlFor="integration-project">CI 项目</Label><select id="integration-project" className="input" value={project?.id ?? ""} onChange={(event) => onSelectProject(event.target.value)}><option value="">选择项目</option>{visibleProjects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
      </div>
    </Card>

    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {notice && <div className="alert alert-success" role="status">{notice}</div>}
    {!organizations.length && <Card className="integrations-empty"><h2>先创建组织</h2><p>集成凭据和 SecretRef 都属于组织范围。</p></Card>}

    {organizations.length > 0 && <div className="integrations-grid">
      <Card className="form-card">
        <div className="integrations-card-heading"><div><CardTitle>组织集成</CardTitle><p>只登记 SecretRef；密钥值由部署侧密钥提供方解析。</p></div><span className="integrations-count">{integrations.length}</span></div>
        {loading ? <p className="empty-inline">正在加载组织集成…</p> : !integrations.length
          ? <p className="empty-inline">该组织尚未配置集成。</p>
          : <div className="integrations-list">{integrations.map((item) => {
            const ref = visibleSecretRefs.find((candidate) => candidate.id === item.secret_ref_id);
            return <article className={`integration-row ${selectedIntegrationId === item.id ? "integration-row-selected" : ""}`} key={item.id}>
              <button type="button" className="integration-select" aria-pressed={selectedIntegrationId === item.id} onClick={() => setSelectedIntegrationId(item.id)}>
                <span className={`integration-provider-icon integration-provider-${item.kind}`}>{item.kind === "slack" ? "S" : "D"}</span>
                <span className="integration-provider-copy"><strong>{item.kind === "slack" ? "Slack" : "Discord"}</strong><small>{ref ? `${ref.provider} · ${ref.external_key} · v${ref.active_version ?? "?"}` : "未关联可用 SecretRef"}</small></span>
                <span className={`integration-state ${item.enabled ? "integration-state-enabled" : ""}`}>{item.enabled ? "已启用" : "已停用"}</span>
              </button>
              <Button type="button" variant="secondary" disabled={busy} onClick={() => void toggleIntegration(item)}>{item.enabled ? "停用集成" : "启用集成"}</Button>
            </article>;
          })}</div>}
        <form className="integrations-create-form" onSubmit={createIntegration}>
          <h3>添加集成</h3>
          <div><Label htmlFor="integration-kind">集成类型</Label><select id="integration-kind" className="input" value={kind} onChange={(event) => setKind(event.target.value as Integration["kind"])}><option value="slack">Slack</option><option value="discord">Discord</option></select></div>
          <div><Label htmlFor="integration-secret-ref">SecretRef</Label><select id="integration-secret-ref" className="input" value={secretRefId} onChange={(event) => setSecretRefId(event.target.value)} required><option value="">选择 SecretRef</option>{visibleSecretRefs.map((ref) => <option key={ref.id} value={ref.id}>{ref.provider} · {ref.external_key} · v{ref.active_version ?? "?"}</option>)}</select><small>{selectedSecretRef ? `仅使用引用 ${selectedSecretRef.id}，不会读取密钥值。` : "请先在组织凭据中登记 SecretRef。"}</small></div>
          <Button disabled={busy || !organizationId || !secretRefId}>{busy ? "保存中…" : "添加集成"}</Button>
        </form>
      </Card>

      <Card className="form-card ci-card">
        <div className="integrations-card-heading"><div><CardTitle>CI Run 与门禁</CardTitle><p>请求按组织、仓库、Commit、计划、环境和浏览器矩阵幂等。</p></div></div>
        {!project ? <p className="empty-inline">选择项目后配置 CI 触发。</p> : <form className="integrations-ci-form" onSubmit={triggerCI}>
          <div><Label htmlFor="ci-plan">执行计划</Label><select id="ci-plan" className="input" value={planId} onChange={(event) => setPlanId(event.target.value)} required><option value="">选择计划</option>{plans.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
          <div><Label htmlFor="ci-environment">测试环境</Label><select id="ci-environment" className="input" value={environmentId} onChange={(event) => setEnvironmentId(event.target.value)} required><option value="">选择环境</option>{environments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
          <div><Label htmlFor="ci-repository">仓库</Label><Input id="ci-repository" value={repository} onChange={(event) => setRepository(event.target.value)} required maxLength={500} placeholder="acme/storefront" /></div>
          <div className="integrations-inline-fields"><div><Label htmlFor="ci-commit">Commit SHA</Label><Input id="ci-commit" value={commitSha} onChange={(event) => setCommitSha(event.target.value)} required minLength={7} maxLength={64} pattern="[a-fA-F0-9]{7,64}" /></div><div><Label htmlFor="ci-branch">分支（可选）</Label><Input id="ci-branch" value={branch} onChange={(event) => setBranch(event.target.value)} maxLength={255} /></div></div>
          <div><Label htmlFor="ci-intent">触发意图</Label><Input id="ci-intent" value={requestIntent} onChange={(event) => setRequestIntent(event.target.value)} required minLength={1} maxLength={4000} /></div>
          <fieldset className="integrations-browser-set"><legend>浏览器矩阵</legend>{browsers.map((item) => <label key={item.id}><input type="checkbox" checked={selectedBrowsers.includes(item.id)} onChange={() => toggleBrowser(item.id)} />{item.label}</label>)}</fieldset>
          <Button disabled={busy || !planId || !environmentId || !selectedBrowsers.length}>{busy ? "正在触发…" : "触发 CI Run"}</Button>
        </form>}
        {run && <div className="integrations-gate" aria-live="polite"><div><span className="integrations-kicker">RUN {run.id}</span><strong className={`gate-status gate-status-${gate?.status?.toLowerCase() ?? "pending"}`}>{gate?.status ?? "PENDING"}</strong><small>Run 状态：{run.status} · 阻断级别：{gate?.blocking_severity ?? "HIGH"}</small>{gate && <small>断言 {gate.counts.assertions_evaluated}/{gate.counts.assertions_total} · 待处理 {gate.counts.assertions_pending} · 阻断失败 {gate.counts.blocking_failures}</small>}</div><Button type="button" variant="secondary" disabled={busy} onClick={() => void loadGate(run.id)}>刷新门禁</Button></div>}
      </Card>

      <Card className="form-card integrations-deliveries-card">
        <div className="integrations-card-heading"><div><CardTitle>Webhook 投递历史</CardTitle><p>重试次数有上限；耗尽后显示 DEAD，并保留安全错误码。</p></div><Button type="button" variant="secondary" disabled={!selectedIntegration || loadingDeliveries} onClick={() => void loadDeliveries()}>刷新历史</Button></div>
        {!selectedIntegration ? <p className="empty-inline">选择一个集成查看投递记录。</p> : loadingDeliveries ? <p className="empty-inline">正在加载投递记录…</p> : !deliveries.length ? <p className="empty-inline">该集成暂无 Webhook 投递。</p> : <div className="delivery-table-wrap"><table className="delivery-table"><thead><tr><th>事件</th><th>状态</th><th>尝试</th><th>最后错误</th><th>操作</th></tr></thead><tbody>{deliveries.map((item) => {
          const lastAttempt = item.attempt_history[item.attempt_history.length - 1];
          return <tr key={item.id}><td><code>{item.event_id}</code></td><td><span className={`delivery-status delivery-status-${item.status.toLowerCase()}`}>{item.status}</span></td><td>{item.attempts}</td><td>{lastAttempt?.error_code ?? (item.response_code ? `HTTP_${item.response_code}` : "—")}</td><td>{item.status === "FAILED" || item.status === "DEAD" ? <Button type="button" variant="secondary" disabled={busy} onClick={() => void retryDelivery(item)}>重试投递</Button> : "—"}</td></tr>;
        })}</tbody></table></div>}
      </Card>
    </div>}
  </div>;
}
