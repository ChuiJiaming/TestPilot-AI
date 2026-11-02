import { useCallback, useEffect, useRef, useState } from "react";
import { Download, FileText, RefreshCw, RotateCcw } from "lucide-react";
import { ApiError, api, type Project } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Label } from "./components/ui/label";

type RunStatus = "QUEUED" | "PREPARING" | "RUNNING" | "WAITING_APPROVAL" | "PAUSING" | "PAUSED"
  | "FINALIZING" | "PASSED" | "FAILED" | "ERROR" | "CANCELLING" | "CANCELLED";
type RunItem = {
  id: string; project_id: string; status: RunStatus; branch: string | null;
  commit_sha: string | null; trigger_source: string; browser_matrix: string[];
  created_at: string; finished_at: string | null;
};
type ArtifactReference = {
  id: string; artifact_type: string; content_type?: string; status: string;
  size_bytes?: number | null;
};
type Report = {
  id: string; run_id: string; branch: string | null; commit_sha: string | null;
  summary: { run?: { status?: string; branch?: string | null; commit_sha?: string | null };
    summary?: { counts?: Record<string, number> } };
  artifact_references: ArtifactReference[]; next_cursor: string | null;
};
type ReportExport = {
  id: string; run_id: string; format: ExportFormat;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  artifact_id: string | null;
};
type ExportFormat = "html" | "pdf" | "json" | "junit";

const terminalStatuses = new Set<RunStatus>(["PASSED", "FAILED", "ERROR", "CANCELLED"]);
const statusLabels: Record<ReportExport["status"], string> = {
  QUEUED: "导出排队中",
  RUNNING: "正在生成导出",
  SUCCEEDED: "导出已完成",
  FAILED: "导出失败，可重试",
};
const formatLabels: Record<ExportFormat, string> = {
  html: "HTML 网页", pdf: "PDF 文档", json: "JSON 数据", junit: "JUnit XML",
};

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

function idempotencyKey() {
  return globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export function ReportsPage({ projects, project, onSelectProject, accessToken }: {
  projects: Project[]; project: Project | undefined; onSelectProject: (id: string) => void;
  accessToken: string;
}) {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedRunProjectId, setSelectedRunProjectId] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [exportJob, setExportJob] = useState<ReportExport | null>(null);
  const [format, setFormat] = useState<ExportFormat>("html");
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [loadingReport, setLoadingReport] = useState(false);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const runsGeneration = useRef(0);
  const reportGeneration = useRef(0);

  const loadRuns = useCallback(async () => {
    const generation = ++runsGeneration.current;
    setRuns([]); setSelectedRunId(""); setSelectedRunProjectId("");
    setReport(null); setExportJob(null); setError(""); setNotice("");
    if (!project) { setLoadingRuns(false); return; }
    setLoadingRuns(true);
    try {
      const page = await api<{ items: RunItem[]; next_cursor: string | null }>(
        `/api/v1/projects/${project.id}/test-runs?limit=100`, {}, accessToken,
      );
      if (generation !== runsGeneration.current) return;
      const terminalRuns = page.items.filter((item) => terminalStatuses.has(item.status));
      setRuns(terminalRuns);
      setSelectedRunProjectId(project.id);
      setSelectedRunId((current) => terminalRuns.some((item) => item.id === current)
        ? current : (terminalRuns[0]?.id ?? ""));
    } catch (caught) {
      if (generation === runsGeneration.current) setError(messageOf(caught));
    } finally {
      if (generation === runsGeneration.current) setLoadingRuns(false);
    }
  }, [accessToken, project]);

  useEffect(() => { void loadRuns(); }, [loadRuns]);

  useEffect(() => {
    const generation = ++reportGeneration.current;
    setReport(null); setExportJob(null); setNotice("");
    if (!project || !selectedRunId || selectedRunProjectId !== project.id) {
      setLoadingReport(false);
      return;
    }
    setLoadingReport(true); setError("");
    api<Report>(`/api/v1/test-runs/${selectedRunId}/report`, {}, accessToken)
      .then((value) => {
        if (generation === reportGeneration.current) setReport(value);
      })
      .catch((caught: unknown) => {
        if (generation === reportGeneration.current) {
          if (caught instanceof ApiError && caught.status === 404) {
            setNotice("该运行尚无可读取的报告快照。");
          } else {
            setError(messageOf(caught));
          }
        }
      })
      .finally(() => {
        if (generation === reportGeneration.current) setLoadingReport(false);
      });
    return () => { reportGeneration.current += 1; };
  }, [accessToken, project, selectedRunId, selectedRunProjectId]);

  useEffect(() => {
    if (!exportJob || (exportJob.status !== "QUEUED" && exportJob.status !== "RUNNING")) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void api<ReportExport>(`/api/v1/report-exports/${exportJob.id}`, {}, accessToken)
        .then((value) => { if (active) setExportJob(value); })
        .catch((caught: unknown) => { if (active) setError(messageOf(caught)); });
    }, 500);
    return () => { active = false; window.clearTimeout(timer); };
  }, [accessToken, exportJob]);

  async function createExport() {
    if (!report) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<ReportExport>(`/api/v1/reports/${report.id}/exports`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey() },
        body: JSON.stringify({ format }),
      }, accessToken);
      setExportJob(created);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function downloadExport() {
    if (!exportJob?.artifact_id) return;
    setDownloading(true); setError("");
    try {
      const response = await fetch(`/api/v1/artifacts/${exportJob.artifact_id}/download`, {
        method: "GET",
        headers: { Authorization: `Bearer ${accessToken}` },
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) {
        let detail = `报告下载失败 (${response.status})`;
        try {
          const body = await response.json() as { detail?: string };
          detail = body.detail ?? detail;
        } catch { /* Keep the status message for non-JSON errors. */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `${exportJob.run_id}-report.${exportJob.format === "junit" ? "xml" : exportJob.format}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setDownloading(false); }
  }

  const expiredArtifacts = report?.artifact_references.filter(
    (artifact) => artifact.status === "EXPIRED_ARTIFACT",
  ) ?? [];

  if (projects.length === 0) {
    return <Card className="reports-empty"><CardTitle>先创建一个项目</CardTitle><p>运行报告按当前项目权限读取和下载。</p></Card>;
  }

  return <div className="reports-workspace">
    <Card className="reports-project-bar">
      <div><span className="reports-kicker">RUN REPORTS</span><CardTitle>运行报告与导出</CardTitle>
        <p>报告来自不可变运行快照，导出文件只通过当前授权的私有 API 下载。</p></div>
      <div className="reports-project-picker"><Label htmlFor="reports-project">项目</Label>
        <select id="reports-project" className="input" value={project?.id ?? ""}
          onChange={(event) => onSelectProject(event.target.value)}>
          <option value="" disabled>选择项目</option>
          {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </div>
      <Button type="button" variant="secondary" onClick={() => void loadRuns()} disabled={loadingRuns}>
        <RefreshCw size={15} />{loadingRuns ? "加载中…" : "刷新"}
      </Button>
    </Card>

    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {notice && <div className="alert alert-success" role="status">{notice}</div>}
    {expiredArtifacts.length > 0 && <div className="reports-expired-notice" role="status">
      有 {expiredArtifacts.length} 个历史证据已过期（EXPIRED_ARTIFACT）；运行报告仍可查看。
    </div>}

    <Card className="reports-run-bar">
      <div className="reports-run-label"><Label htmlFor="reports-run">已完成的运行</Label>
        <select id="reports-run" className="input" value={selectedRunId}
          onChange={(event) => { setSelectedRunProjectId(project?.id ?? ""); setSelectedRunId(event.target.value); }}
          disabled={!runs.length || loadingRuns}>
          {runs.length === 0 && <option value="">没有已完成的运行</option>}
          {runs.map((item) => <option key={item.id} value={item.id}>
            {item.branch || item.id.slice(0, 8)} · {item.status} · {item.commit_sha?.slice(0, 10) ?? "无 Commit"}
          </option>)}
        </select>
      </div>
      {selectedRunId && <div className="reports-run-meta">
        <span>Run <code>{selectedRunId}</code></span>
        <span>状态 <b>{runs.find((item) => item.id === selectedRunId)?.status ?? "—"}</b></span>
        <span>Commit <code>{runs.find((item) => item.id === selectedRunId)?.commit_sha ?? "—"}</code></span>
      </div>}
    </Card>

    {loadingReport && <div className="reports-loading" role="status"><span className="spinner" />正在读取报告快照…</div>}
    {!loadingRuns && !loadingReport && !selectedRunId && !error && !notice
      && <Card className="reports-empty"><FileText size={22} /><CardTitle>当前项目没有可查看的运行报告</CardTitle>
        <p>完成一次运行后，报告快照会出现在这里。</p></Card>}

    {report && <div className="reports-layout">
      <Card className="reports-summary-card">
        <div className="reports-card-heading"><div><span className="reports-kicker">IMMUTABLE SNAPSHOT</span>
          <CardTitle>报告摘要</CardTitle></div><span className={`reports-status reports-status-${(report.summary.run?.status ?? "unknown").toLowerCase()}`}>
            {report.summary.run?.status ?? "未知"}
          </span></div>
        <dl className="reports-facts">
          <div><dt>分支</dt><dd>{report.branch ?? report.summary.run?.branch ?? "—"}</dd></div>
          <div><dt>Commit</dt><dd><code>{report.commit_sha ?? report.summary.run?.commit_sha ?? "—"}</code></dd></div>
          <div><dt>运行 ID</dt><dd><code>{report.run_id}</code></dd></div>
          <div><dt>报告 ID</dt><dd><code>{report.id}</code></dd></div>
        </dl>
        <div className="reports-counts">
          {Object.entries(report.summary.summary?.counts ?? {}).map(([name, count]) =>
            <div key={name}><span>{name.replaceAll("_", " ")}</span><strong>{count}</strong></div>)}
        </div>
        <div className="reports-artifacts">
          <h3>运行证据</h3>
          {report.artifact_references.length === 0 ? <p>此报告没有关联证据文件。</p>
            : report.artifact_references.map((artifact) => <div className="reports-artifact-row" key={artifact.id}>
              <span><FileText size={15} />{artifact.artifact_type}</span>
              <code>{artifact.id}</code>
              <span className={artifact.status === "EXPIRED_ARTIFACT" ? "reports-expired" : "reports-available"}>
                {artifact.status}
              </span>
            </div>)}
        </div>
      </Card>

      <Card className="reports-export-card">
        <span className="reports-kicker">PRIVATE EXPORT</span><CardTitle>生成报告文件</CardTitle>
        <p>导出在后台生成。失败不会更改运行状态，下载时会重新检查项目权限。</p>
        <div className="reports-export-controls">
          <div><Label htmlFor="reports-format">导出格式</Label>
            <select id="reports-format" className="input" value={format}
              onChange={(event) => setFormat(event.target.value as ExportFormat)}
              disabled={busy || exportJob?.status === "QUEUED" || exportJob?.status === "RUNNING"}>
              {(Object.keys(formatLabels) as ExportFormat[]).map((item) =>
                <option key={item} value={item}>{formatLabels[item]}</option>)}
            </select>
          </div>
          {!exportJob || exportJob.status === "FAILED"
            ? <Button type="button" onClick={() => void createExport()} disabled={busy}>
              {exportJob?.status === "FAILED" ? <><RotateCcw size={15} />重试导出</> : "生成导出"}
            </Button>
            : exportJob.status === "SUCCEEDED"
              ? <Button type="button" onClick={() => void downloadExport()} disabled={downloading || !exportJob.artifact_id}>
                <Download size={15} />{downloading ? "准备下载…" : "下载报告"}
              </Button>
              : <Button type="button" variant="secondary" disabled><RefreshCw size={15} />{statusLabels[exportJob.status]}</Button>}
        </div>
        {exportJob && <div className={`reports-export-status reports-export-${exportJob.status.toLowerCase()}`} role="status">
          <strong>{statusLabels[exportJob.status]}</strong>
          <span>{formatLabels[exportJob.format]} · {exportJob.id}</span>
        </div>}
        {exportJob?.status === "FAILED" && <p className="reports-retry-note">可以重新提交导出；原运行状态保持不变。</p>}
      </Card>
    </div>}
  </div>;
}
