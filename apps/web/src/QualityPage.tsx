import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, type Project } from "./api";
import { Button } from "./components/ui/button";
import { Modal } from "./components/ui/modal";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";

type Page<T> = { items: T[]; next_cursor: string | null };
type Defect = {
  id: string; project_id: string; title: string;
  status: "OPEN" | "TRIAGED" | "IN_PROGRESS" | "RESOLVED" | "WONT_FIX";
  severity: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  classification: "LOCATOR" | "ASSERTION" | "ENVIRONMENT" | "INFRA" | "SECURITY" | "DATA" | "FLAKY";
  root_cause_status: "INSUFFICIENT_EVIDENCE" | "SUGGESTED" | "CONFIRMED";
  analysis: { hypothesis?: string; evidence_status?: string; evidence_ids?: string[]; confidence?: number };
  occurrence_count?: number; created_at: string;
};
type Occurrence = {
  id: string; run_id: string; run_case_id: string; browser: string | null;
  error_code: string | null; evidence_status: string; observed_at: string;
  evidence: { artifact_ids?: string[] };
};
type Artifact = {
  id: string; project_id: string; run_id: string; run_case_id: string | null;
  action_id: string | null; environment_id: string | null; case_version_id: string | null;
  browser: "chromium" | "firefox" | "webkit" | null; artifact_type: string;
  content_type: string; size_bytes: number | null; redacted: boolean;
  viewport: { width: number; height: number } | null; device_scale_factor: number | null;
  color_scheme: "light" | "dark" | null; created_at: string;
};
type Baseline = {
  id: string; project_id: string; case_version_id: string; action_id: string | null;
  environment_id: string | null; browser: string; viewport: Record<string, number>;
  artifact_id: string; device_scale_factor: number; color_scheme: string;
  status: "PENDING" | "APPROVED" | "REJECTED"; created_by: string | null;
  decision_reason?: string | null; mask_regions: Array<{ x: number; y: number; width: number; height: number }>;
  max_pixel_ratio: number; pixel_channel_tolerance: number; comparison_profile: string;
  mask_hash: string; created_at: string;
};
type VisualDiff = {
  id: string; baseline_id: string | null; run_case_id: string;
  actual_artifact_id: string | null; diff_artifact_id: string | null;
  pixel_ratio: number | null; changed_pixels: number | null; compared_pixels: number | null;
  status: "PASS" | "FAIL" | "REVIEW" | "NEED_BASELINE" | "ERROR";
  error_code: string | null; created_at: string;
};

const STATUS_LABEL: Record<Defect["status"], string> = {
  OPEN: "待分诊", TRIAGED: "已分诊", IN_PROGRESS: "处理中", RESOLVED: "已解决", WONT_FIX: "不修复",
};
const CLASS_LABEL: Record<Defect["classification"], string> = {
  LOCATOR: "定位器", ASSERTION: "断言", ENVIRONMENT: "环境", INFRA: "基础设施",
  SECURITY: "安全", DATA: "数据", FLAKY: "不稳定",
};

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

function PrivateArtifactImage({ artifactId, accessToken, alt }: { artifactId: string; accessToken: string; alt: string }) {
  const [source, setSource] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setSource(""); setFailed(false);
    fetch(`/api/v1/artifacts/${artifactId}/download`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      credentials: "include", signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error(`截图读取失败 (${response.status})`);
      const contentType = response.headers.get("content-type") ?? "";
      if (!contentType.toLowerCase().startsWith("image/png")) throw new Error("截图格式无效");
      return response.blob();
    }).then((blob) => {
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob);
      setSource(objectUrl);
    }).catch(() => {
      if (!controller.signal.aborted) setFailed(true);
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [artifactId, accessToken]);
  if (failed) return <div className="quality-image-fallback" role="status">截图暂不可用</div>;
  if (!source) return <div className="quality-image-fallback" role="status">正在读取私有截图…</div>;
  return <img className="quality-evidence-image" src={source} alt={alt} />;
}

export function QualityPage({ projects, project, onSelectProject, accessToken, currentUserId }: {
  projects: Project[]; project: Project | undefined; onSelectProject: (id: string) => void;
  accessToken: string; currentUserId: string;
}) {
  const [defects, setDefects] = useState<Defect[]>([]);
  const [baselines, setBaselines] = useState<Baseline[]>([]);
  const [diffs, setDiffs] = useState<VisualDiff[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedDefectId, setSelectedDefectId] = useState("");
  const [selectedDefect, setSelectedDefect] = useState<Defect | null>(null);
  const [occurrences, setOccurrences] = useState<Occurrence[]>([]);
  const [defectStatus, setDefectStatus] = useState<Defect["status"]>("OPEN");
  const [severity, setSeverity] = useState<Defect["severity"]>("MEDIUM");
  const [classification, setClassification] = useState<Defect["classification"]>("ASSERTION");
  const [rootCause, setRootCause] = useState<Defect["root_cause_status"]>("SUGGESTED");
  const [triageReason, setTriageReason] = useState("");
  const [baselineReason, setBaselineReason] = useState("");
  const [proposalArtifactId, setProposalArtifactId] = useState("");
  const [maskText, setMaskText] = useState("[]");
  const [showProposalForm, setShowProposalForm] = useState(false);
  const [maxPixelRatio, setMaxPixelRatio] = useState("0.01");
  const [channelTolerance, setChannelTolerance] = useState("0");
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const loadGeneration = useRef(0);
  const detailGeneration = useRef(0);

  const loadProjectData = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setSelectedDefectId(""); setSelectedDefect(null); setOccurrences([]);
    setDefects([]); setBaselines([]); setDiffs([]); setArtifacts([]);
    setError(""); setNotice("");
    if (!project) { setLoading(false); return; }
    setLoading(true);
    try {
      const query = "?limit=100";
      const [defectPage, baselinePage, diffPage, artifactPage] = await Promise.all([
        api<Page<Defect>>(`/api/v1/projects/${project.id}/defects${query}`, {}, accessToken),
        api<Page<Baseline>>(`/api/v1/projects/${project.id}/visual-baselines${query}`, {}, accessToken),
        api<Page<VisualDiff>>(`/api/v1/visual-diffs?project_id=${encodeURIComponent(project.id)}&limit=100`, {}, accessToken),
        api<Page<Artifact>>(`/api/v1/projects/${project.id}/artifacts?limit=100&artifact_type=screenshot`, {}, accessToken),
      ]);
      if (generation !== loadGeneration.current) return;
      setDefects(defectPage.items); setBaselines(baselinePage.items);
      setDiffs(diffPage.items); setArtifacts(artifactPage.items.filter((item) => item.redacted));
      setSelectedDefectId(defectPage.items[0]?.id ?? "");
    } catch (caught) {
      if (generation === loadGeneration.current) setError(messageOf(caught));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [accessToken, project]);

  useEffect(() => { void loadProjectData(); }, [loadProjectData]);

  useEffect(() => {
    const generation = ++detailGeneration.current;
    setSelectedDefect(null); setOccurrences([]);
    if (!selectedDefectId || !project) { setDetailLoading(false); return; }
    setDetailLoading(true);
    Promise.all([
      api<Defect>(`/api/v1/defects/${selectedDefectId}`, {}, accessToken),
      api<Page<Occurrence>>(`/api/v1/defects/${selectedDefectId}/occurrences?limit=100`, {}, accessToken),
    ]).then(([item, page]) => {
      if (generation !== detailGeneration.current) return;
      setSelectedDefect(item); setOccurrences(page.items);
      setDefectStatus(item.status); setSeverity(item.severity);
      setClassification(item.classification); setRootCause(item.root_cause_status);
    }).catch((caught: unknown) => {
      if (generation === detailGeneration.current) setError(messageOf(caught));
    }).finally(() => {
      if (generation === detailGeneration.current) setDetailLoading(false);
    });
  }, [accessToken, project, selectedDefectId]);

  const proposalArtifact = useMemo(
    () => artifacts.find((item) => item.id === proposalArtifactId),
    [artifacts, proposalArtifactId],
  );
  const screenshotOptions = artifacts.filter((item) => item.artifact_type === "screenshot"
    && item.content_type === "image/png" && item.case_version_id && item.environment_id
    && item.action_id && item.browser && item.viewport);

  async function saveTriage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedDefect || !triageReason.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await api<Defect>(`/api/v1/defects/${selectedDefect.id}`, {
        method: "PATCH", body: JSON.stringify({
          status: defectStatus, severity, classification, root_cause_status: rootCause,
          reason: triageReason.trim(),
        }),
      }, accessToken);
      setSelectedDefect(updated);
      setDefects((current) => current.map((item) => item.id === updated.id ? { ...item, ...updated } : item));
      setTriageReason(""); setNotice("分诊结果已保存并写入审计记录。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function decideBaseline(baselineId: string, decision: "APPROVE" | "REJECT") {
    if (!baselineReason.trim()) { setError("请填写基线审批原因。"); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await api<Baseline>(`/api/v1/visual-baselines/${baselineId}/decision`, {
        method: "POST", body: JSON.stringify({ decision, reason: baselineReason.trim() }),
      }, accessToken);
      setBaselines((current) => current.map((item) => item.id === updated.id ? updated : item));
      setBaselineReason(""); setNotice(decision === "APPROVE" ? "基线已批准，可用于视觉断言。" : "基线已拒绝。视图结果不会因此通过。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function createProposal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project || !proposalArtifact) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const masks = JSON.parse(maskText);
      if (!Array.isArray(masks)) throw new Error("遮罩区域必须是 JSON 数组。");
      const result = await api<Baseline>("/api/v1/visual-baselines/proposals", {
        method: "POST", body: JSON.stringify({
          project_id: project.id, case_version_id: proposalArtifact.case_version_id,
          action_id: proposalArtifact.action_id, environment_id: proposalArtifact.environment_id,
          browser: proposalArtifact.browser, artifact_id: proposalArtifact.id,
          viewport: proposalArtifact.viewport, mask_regions: masks,
          max_pixel_ratio: Number(maxPixelRatio), pixel_channel_tolerance: Number(channelTolerance),
        }),
      }, accessToken);
      setBaselines((current) => [result, ...current]);
      setShowProposalForm(false);
      setNotice("基线候选已提交，需由另一位有权成员审批后才能用于断言。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  if (!projects.length) return <Card className="quality-empty"><CardTitle>先创建一个项目</CardTitle><p>缺陷、运行截图和视觉基线都按项目隔离。</p></Card>;

  return <div className="quality-workspace">
    <Card className="quality-project-bar">
      <div><span className="quality-kicker">PROJECT QUALITY</span><CardTitle>缺陷分诊与视觉回归</CardTitle><p>AI 分析仅提供可审计假设。视觉基线必须经过另一位项目成员批准。</p></div>
      <div className="quality-project-picker"><Label htmlFor="quality-project">项目</Label><select id="quality-project" className="input" value={project?.id ?? ""} onChange={(event) => onSelectProject(event.target.value)}><option value="" disabled>选择项目</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
      <Button type="button" variant="secondary" onClick={() => void loadProjectData()} disabled={loading}>{loading ? "加载中…" : "刷新"}</Button>
    </Card>

    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {notice && <div className="alert alert-success" role="status">{notice}</div>}
    {!project ? <Card className="quality-empty"><CardTitle>选择项目查看质量记录</CardTitle><p>项目切换会重新加载该项目的缺陷、截图、基线和视觉结果。</p></Card> : <>
      <section className="quality-section" aria-labelledby="defect-heading">
        <div className="section-heading"><div><h2 id="defect-heading">缺陷分诊</h2><p>{defects.length} 条缺陷 · 分组基于确定性失败指纹</p></div></div>
        {loading ? <div className="quality-loading" role="status">正在加载缺陷和证据…</div> : defects.length === 0 ? <Card className="quality-empty"><CardTitle>当前项目没有缺陷</CardTitle><p>运行出现失败后，Analyzer 会按指纹归组并记录 RunCase 发生历史。</p></Card> : <div className="quality-defect-layout">
          <Card className="quality-defect-list"><div className="quality-list-title"><CardTitle>缺陷列表</CardTitle><span>{defects.length}</span></div>{defects.map((item) => <button className={`quality-defect-item ${selectedDefectId === item.id ? "selected" : ""}`} type="button" key={item.id} onClick={() => setSelectedDefectId(item.id)}><span className="quality-defect-item-head"><b>{item.title}</b><span className={`quality-state state-${item.status.toLowerCase()}`}>{STATUS_LABEL[item.status]}</span></span><span className="quality-defect-meta">{CLASS_LABEL[item.classification]} · {item.severity} · {item.occurrence_count ?? 0} 次发生</span><small>{new Date(item.created_at).toLocaleString("zh-CN")}</small></button>)}</Card>
          <Card className="quality-defect-detail"><div className="quality-list-title"><div><CardTitle>{selectedDefect?.title ?? "缺陷详情"}</CardTitle><p>根因状态：{selectedDefect?.root_cause_status === "CONFIRMED" ? "人工确认" : selectedDefect?.root_cause_status === "SUGGESTED" ? "待人工核实" : "证据不足"}</p></div>{selectedDefect && <span className={`quality-state state-${selectedDefect.status.toLowerCase()}`}>{STATUS_LABEL[selectedDefect.status]}</span>}</div>
            {detailLoading ? <div className="quality-loading" role="status">正在读取发生历史…</div> : selectedDefect && <>
              <div className="quality-hypothesis"><b>分析假设</b><p>{selectedDefect.analysis.hypothesis ?? "没有可用的分析假设。"}</p><small>{selectedDefect.analysis.evidence_status === "AVAILABLE" ? "存在可审阅证据" : "证据不足，根因不能确认为已解决"} · 置信度 {Math.round((selectedDefect.analysis.confidence ?? 0) * 100)}%</small></div>
              <form className="quality-triage-form" onSubmit={saveTriage}>
                <div><Label htmlFor="defect-status">缺陷状态</Label><select id="defect-status" className="input" value={defectStatus} onChange={(event) => setDefectStatus(event.target.value as Defect["status"])}>{Object.entries(STATUS_LABEL).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div>
                <div><Label htmlFor="defect-severity">严重程度</Label><select id="defect-severity" className="input" value={severity} onChange={(event) => setSeverity(event.target.value as Defect["severity"])}>{["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((value) => <option key={value}>{value}</option>)}</select></div>
                <div><Label htmlFor="defect-classification">分类</Label><select id="defect-classification" className="input" value={classification} onChange={(event) => setClassification(event.target.value as Defect["classification"])}>{Object.entries(CLASS_LABEL).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div>
                <div><Label htmlFor="defect-root-cause">根因判断</Label><select id="defect-root-cause" className="input" value={rootCause} onChange={(event) => setRootCause(event.target.value as Defect["root_cause_status"])}><option value="INSUFFICIENT_EVIDENCE">证据不足</option><option value="SUGGESTED">建议，待核实</option><option value="CONFIRMED" disabled={selectedDefect.analysis.evidence_status !== "AVAILABLE"}>人工确认</option></select></div>
                <div className="quality-form-wide"><Label htmlFor="triage-reason">分诊原因</Label><Input id="triage-reason" value={triageReason} onChange={(event) => setTriageReason(event.target.value)} maxLength={2000} required placeholder="记录分诊依据，变更会进入审计日志" /></div>
                <Button type="submit" disabled={busy || !triageReason.trim()}>{busy ? "保存中…" : "保存分诊"}</Button>
              </form>
              <div className="quality-occurrences"><h3>发生历史</h3>{occurrences.length === 0 ? <p className="empty-inline">尚无发生记录。</p> : occurrences.map((item) => <article className="quality-occurrence" key={item.id}><div><b>{item.error_code ?? "RUN_CASE_FAILED"}</b><small>{item.browser ?? "未知浏览器"} · RunCase {item.run_case_id.slice(0, 8)} · {new Date(item.observed_at).toLocaleString("zh-CN")}</small></div><span>{item.evidence_status === "AVAILABLE" ? "有证据" : "证据不足"}</span>{(item.evidence.artifact_ids ?? []).map((artifactId) => <PrivateArtifactImage key={artifactId} artifactId={artifactId} accessToken={accessToken} alt="Run 截图证据" />)}</article>)}</div>
            </>}
          </Card>
        </div>}
      </section>

      <section className="quality-section" aria-labelledby="baseline-heading">
        <div className="section-heading"><div><h2 id="baseline-heading">视觉基线</h2><p>审批人需要与候选创建者不同；审批后的上下文、遮罩和阈值作为版本配置保留。</p></div></div>
        <div className="quality-baseline-layout">
          <Card className="quality-proposal-card"><div className="quality-list-title"><CardTitle>提交基线候选</CardTitle><Button type="button" onClick={() => setShowProposalForm(true)}>提交候选</Button></div><p>只能从当前项目的脱敏 PNG 截图创建候选；审批人需要与候选创建者不同。</p>
            {showProposalForm && <Modal wide title="提交基线候选" description="只能从当前项目的脱敏 PNG 截图创建候选。" onClose={() => setShowProposalForm(false)}>
            <form className="form-stack" onSubmit={createProposal}>
              <div><Label htmlFor="proposal-artifact">候选截图</Label><select id="proposal-artifact" className="input" value={proposalArtifactId} onChange={(event) => setProposalArtifactId(event.target.value)}><option value="">选择一张运行截图</option>{screenshotOptions.map((item) => <option key={item.id} value={item.id}>{item.browser} · {item.viewport?.width}×{item.viewport?.height} · {new Date(item.created_at).toLocaleString("zh-CN")}</option>)}</select></div>
              <div><Label htmlFor="proposal-max-ratio">最大像素差异比例</Label><Input id="proposal-max-ratio" type="number" min="0" max="1" step="0.001" value={maxPixelRatio} onChange={(event) => setMaxPixelRatio(event.target.value)} required /></div>
              <div><Label htmlFor="proposal-tolerance">单通道容差（0–255）</Label><Input id="proposal-tolerance" type="number" min="0" max="255" step="1" value={channelTolerance} onChange={(event) => setChannelTolerance(event.target.value)} required /></div>
              <div><Label htmlFor="proposal-masks">遮罩区域 JSON</Label><textarea id="proposal-masks" className="input quality-mask-input" value={maskText} onChange={(event) => setMaskText(event.target.value)} spellCheck={false} /></div>
              {proposalArtifact && <p className="quality-context">固定上下文：版本 {proposalArtifact.case_version_id?.slice(0, 8)} · 环境 {proposalArtifact.environment_id?.slice(0, 8)} · 动作 {proposalArtifact.action_id?.slice(0, 8)} · {proposalArtifact.color_scheme} · DPR {proposalArtifact.device_scale_factor}</p>}
              <Button type="submit" disabled={busy || !proposalArtifact}>{busy ? "提交中…" : "提交基线候选"}</Button>
            </form>
            </Modal>}
          </Card>
          <Card className="quality-baseline-list"><div className="quality-list-title"><CardTitle>基线记录</CardTitle><span>{baselines.length}</span></div>
            {baselines.length === 0 ? <p className="empty-inline">还没有基线候选。</p> : baselines.map((item) => <article className="quality-baseline-item" key={item.id}><div className="quality-baseline-header"><div><b>{item.browser} · {item.viewport.width}×{item.viewport.height}</b><small>版本 {item.case_version_id.slice(0, 8)} · {item.comparison_profile} · 阈值 {item.max_pixel_ratio}</small></div><span className={`quality-state state-${item.status.toLowerCase()}`}>{item.status === "PENDING" ? "等待审批" : item.status === "APPROVED" ? "已批准" : "已拒绝"}</span></div><PrivateArtifactImage artifactId={item.artifact_id} accessToken={accessToken} alt="基线截图" />{item.status === "PENDING" && (item.created_by === currentUserId ? <p className="quality-separation-note" role="note">候选创建者不能审批或拒绝；请由另一位有权项目成员处理。</p> : <div className="quality-decision"><Label htmlFor={`baseline-reason-${item.id}`}>基线审批原因</Label><Input id={`baseline-reason-${item.id}`} value={baselineReason} onChange={(event) => setBaselineReason(event.target.value)} maxLength={2000} placeholder="说明审批依据" /><div className="button-row"><Button type="button" disabled={busy || !baselineReason.trim()} onClick={() => void decideBaseline(item.id, "APPROVE")}>批准基线</Button><Button type="button" variant="danger" disabled={busy || !baselineReason.trim()} onClick={() => void decideBaseline(item.id, "REJECT")}>拒绝基线</Button></div></div>)}{item.decision_reason && <small className="quality-decision-reason">审批备注：{item.decision_reason}</small>}</article>)}
          </Card>
        </div>
      </section>

      <section className="quality-section" aria-labelledby="visual-diffs-heading">
        <div className="section-heading"><div><h2 id="visual-diffs-heading">视觉差异审阅</h2><p>缺少已批准基线会显示 NEED_BASELINE；错误或差异结果不会变为通过。</p></div></div>
        {diffs.length === 0 ? <Card className="quality-empty"><CardTitle>暂无视觉差异记录</CardTitle><p>用例中的视觉断言运行后会记录实际截图、差异比例和结果。</p></Card> : <div className="quality-diff-grid">{diffs.map((item) => { const linkedBaseline = baselines.find((baselineItem) => baselineItem.id === item.baseline_id); return <Card className="quality-diff-card" key={item.id}><div className="quality-baseline-header"><div><b>{item.status}</b><small>RunCase {item.run_case_id.slice(0, 8)} · {new Date(item.created_at).toLocaleString("zh-CN")}</small></div><span className="quality-diff-ratio">{item.pixel_ratio === null ? "—" : `${(item.pixel_ratio * 100).toFixed(3)}%`}</span></div>{item.error_code && <p className="quality-error-code">{item.error_code}</p>}<div className="quality-diff-images">{linkedBaseline && <figure><PrivateArtifactImage artifactId={linkedBaseline.artifact_id} accessToken={accessToken} alt="批准基线截图" /><figcaption>基线</figcaption></figure>}{item.actual_artifact_id && <figure><PrivateArtifactImage artifactId={item.actual_artifact_id} accessToken={accessToken} alt="实际运行截图" /><figcaption>实际</figcaption></figure>}{item.diff_artifact_id && <figure><PrivateArtifactImage artifactId={item.diff_artifact_id} accessToken={accessToken} alt="像素差异图" /><figcaption>差异</figcaption></figure>}</div><small>{item.changed_pixels ?? 0} / {item.compared_pixels ?? 0} 个可比较像素发生变化</small></Card>; })}</div>}
      </section>
    </>}
  </div>;
}
