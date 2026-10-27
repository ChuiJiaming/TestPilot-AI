import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, jsonBody, type CaseVersion, type Project, type TestCase } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "发生未知错误，请稍后重试。";
}

export function CasesPage({ projects, project, onSelectProject, accessToken }: {
  projects: Project[]; project: Project | undefined; onSelectProject: (projectId: string) => void; accessToken: string;
}) {
  const [cases, setCases] = useState<TestCase[]>([]);
  const [versions, setVersions] = useState<CaseVersion[]>([]);
  const [caseId, setCaseId] = useState("");
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState<TestCase["priority"]>("P1");
  const [dsl, setDsl] = useState("");
  const [changelog, setChangelog] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [versionNames, setVersionNames] = useState<Record<string, string>>({});
  const [compareVersionId, setCompareVersionId] = useState("");
  const [diffResult, setDiffResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!project) { setCases([]); setCaseId(""); setVersions([]); return; }
    setError("");
    try {
      const page = await api<{ items: TestCase[]; next_cursor: string | null }>(`/api/v1/projects/${project.id}/test-cases?limit=100`, {}, accessToken);
      setCases(page.items);
      setCaseId((current) => page.items.some((item) => item.id === current) ? current : (page.items[0]?.id ?? ""));
    } catch (caught) { setError(errorMessage(caught)); }
  }, [accessToken, project]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!caseId) { setVersions([]); return; }
    let active = true;
    void api<{ items: CaseVersion[] }>(`/api/v1/test-cases/${caseId}/versions`, {}, accessToken)
      .then((page) => { if (active) setVersions(page.items); })
      .catch((caught: unknown) => { if (active) setError(errorMessage(caught)); });
    return () => { active = false; };
  }, [accessToken, caseId]);

  useEffect(() => {
    if (!versions.some((item) => item.id === compareVersionId)) {
      setCompareVersionId(versions[1]?.id ?? "");
      setDiffResult(null);
    }
  }, [compareVersionId, versions]);

  async function createCase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!project) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<TestCase>(`/api/v1/projects/${project.id}/test-cases`, jsonBody({ title, priority }), accessToken);
      setTitle(""); await refresh(); setCaseId(created.id); setNotice("用例草稿已创建。");
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  async function createVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!caseId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const parsed: unknown = JSON.parse(dsl);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("DSL 必须是 JSON 对象");
      const saved = await api<CaseVersion>(`/api/v1/test-cases/${caseId}/versions`, jsonBody({ dsl: parsed, changelog }), accessToken);
      setVersions((current) => [saved, ...current]); setChangelog(""); setNotice(`v${saved.version} 已保存为不可变草稿。`);
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  async function decideVersion(version: CaseVersion, decision: "APPROVED" | "REJECTED") {
    setBusy(true); setError(""); setNotice("");
    try {
      await api<CaseVersion>("/api/v1/test-case-versions/" + version.id + "/approve", jsonBody({ decision, reason: reviewReason }), accessToken);
      const page = await api<{ items: CaseVersion[] }>("/api/v1/test-cases/" + caseId + "/versions", {}, accessToken);
      setVersions(page.items); setNotice("v" + version.version + (decision === "APPROVED" ? " 已批准。" : " 已拒绝。"));
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  async function publishVersion(version: CaseVersion) {
    const name = (versionNames[version.id] ?? version.version_name ?? version.dsl.title ?? "").trim();
    if (!name) { setError("发布前请填写版本标题。"); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/api/v1/test-case-versions/" + version.id + "/publish", jsonBody({ name }), accessToken);
      await refresh(); setNotice("v" + version.version + " 已发布。");
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  async function rollbackVersion(version: CaseVersion) {
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<CaseVersion>("/api/v1/test-cases/" + caseId + "/rollback",
        jsonBody({ target_version: String(version.version), reason: reviewReason }), accessToken);
      setVersions((current) => [created, ...current]); setNotice("已从 v" + version.version + " 创建新草稿 v" + created.version + "。");
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  async function archiveCase() {
    if (!selected) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/api/v1/test-cases/" + selected.id + "/archive", jsonBody({}), accessToken);
      await refresh(); setNotice("用例已归档；历史版本和运行记录保留。");
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  async function compareVersions(target: CaseVersion) {
    if (!project || !caseId || !compareVersionId) return;
    setBusy(true); setError(""); setDiffResult(null);
    try {
      const path = "/api/v1/projects/" + project.id + "/test-cases/" + caseId + "/versions/" + target.id + "/diff?against_version_id=" + compareVersionId;
      setDiffResult(await api<Record<string, unknown>>(path, {}, accessToken));
    } catch (caught) { setError(errorMessage(caught)); }
    finally { setBusy(false); }
  }

  const selected = cases.find((item) => item.id === caseId);
  return <div className="case-workspace">
    <Card className="form-card">
      <div className="form-card-title"><div><CardTitle>项目用例</CardTitle><p>用例版本只追加保存，不覆盖已存在版本。</p></div><Button variant="secondary" type="button" onClick={() => void refresh()} disabled={busy}>刷新</Button></div>
      <div className="environment-picker"><Label htmlFor="case-project">项目</Label><select id="case-project" className="input" value={project?.id ?? ""} onChange={(event) => onSelectProject(event.target.value)}><option value="">选择项目</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
      {project && <>
        <div className="case-list" aria-label="测试用例">{cases.map((item) => <button type="button" key={item.id} className={`case-list-item ${caseId === item.id ? "selected" : ""}`} onClick={() => setCaseId(item.id)}><span><b>{item.title}</b><small>{item.priority} · {item.status}</small></span><small>{item.published_version_id ? "已发布" : "草稿"}</small></button>)}{cases.length === 0 && <div className="empty-inline">当前项目还没有用例。</div>}</div>
        <form className="case-create-form" onSubmit={createCase}><div><Label htmlFor="case-title">用例标题</Label><Input id="case-title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={240} /></div><div><Label htmlFor="case-priority">优先级</Label><select id="case-priority" className="input" value={priority} onChange={(event) => setPriority(event.target.value as TestCase["priority"])}><option>P0</option><option>P1</option><option>P2</option><option>P3</option></select></div><Button type="submit" disabled={busy}>创建草稿</Button></form>
      </>}
    </Card>
    {selected && <div className="case-detail-grid">
      <Card className="form-card"><div className="form-card-title"><div><CardTitle>新增 DSL 版本</CardTitle><p>{selected.title} · v2 信封必须包含匹配的 case_id</p></div></div><form className="form-stack" onSubmit={createVersion}><div><Label htmlFor="case-dsl">DSL v2 JSON</Label><textarea id="case-dsl" className="input policy-editor case-dsl-editor" value={dsl} onChange={(event) => setDsl(event.target.value)} required spellCheck={false} placeholder={`{\n  "schema_version": 2,\n  "case_id": "${selected.id}",\n  "title": "${selected.title}",\n  "steps": [{"action":"navigate","url":"/"},{"action":"click","locator":{"by":"role","value":"button","name":"Continue","exact":true}},{"action":"fill","locator":{"by":"label","value":"Email"},"text":"qa@example.test"}],\n  "assertions": [{"type":"text","locator":{"by":"text","value":"Welcome"},"expected":"Welcome","match":"contains"},{"type":"url","expected":"/home","match":"contains"}]\n}`} /></div><div><Label htmlFor="case-changelog">变更说明</Label><Input id="case-changelog" value={changelog} onChange={(event) => setChangelog(event.target.value)} maxLength={2000} /></div><small>steps/assertions 已采用受限 DSL v2；草稿需独立审核和发布后方可用于未来运行。</small><Button type="submit" disabled={busy}>保存不可变版本</Button></form></Card>
      <Card className="form-card">
        <div className="form-card-title"><div><CardTitle>版本历史与审核</CardTitle><p>审核人与作者必须不同；发布只切换未来执行引用。</p></div><Button variant="danger" type="button" disabled={busy || selected.status === "archived"} onClick={() => void archiveCase()}>归档用例</Button></div>
        <div className="case-review-tools"><Label htmlFor="case-review-reason">审核/回滚原因</Label><Input id="case-review-reason" value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} required /></div>
        {versions.length > 1 && <div className="case-diff-tools"><Label htmlFor="case-diff-base">Diff 对照版本</Label><select id="case-diff-base" className="input" value={compareVersionId} onChange={(event) => setCompareVersionId(event.target.value)}>{versions.filter((item) => item.id !== versions[0]?.id).map((item) => <option key={item.id} value={item.id}>v{item.version}</option>)}</select></div>}
        <div className="case-version-list">{versions.map((version) => <article className="case-version-row" key={version.id}>
          <div><b>v{version.version} · {version.version_name ?? version.dsl.title ?? "标题未记录"}</b><span className="status-pill">{version.review_status ?? "状态未记录"}</span></div>
          <small>{version.changelog || "无变更说明"} · {new Date(version.created_at).toLocaleString()}</small>
          <div className="button-row">{(version.review_status === "DRAFT" || version.review_status === "PENDING_APPROVAL") && <><Button variant="secondary" type="button" disabled={busy || !reviewReason} onClick={() => void decideVersion(version, "APPROVED")}>批准</Button><Button variant="danger" type="button" disabled={busy || !reviewReason} onClick={() => void decideVersion(version, "REJECTED")}>拒绝</Button></>}{version.review_status === "APPROVED" && <><Label htmlFor={`version-name-${version.id}`}>版本标题</Label><Input id={`version-name-${version.id}`} value={versionNames[version.id] ?? version.version_name ?? version.dsl.title ?? ""} onChange={(event) => setVersionNames((current) => ({ ...current, [version.id]: event.target.value }))} maxLength={250} /><Button type="button" disabled={busy || !(versionNames[version.id] ?? version.version_name ?? version.dsl.title ?? "").trim()} onClick={() => void publishVersion(version)}>发布</Button></>}{versions.length > 1 && <Button variant="secondary" type="button" disabled={busy || !compareVersionId} onClick={() => void compareVersions(version)}>与所选版本比较</Button>}{version.review_status !== null && <Button variant="secondary" type="button" disabled={busy || !reviewReason} onClick={() => void rollbackVersion(version)}>从此版本回滚</Button>}</div>
          <pre>{JSON.stringify(version.dsl, null, 2)}</pre>
        </article>)}{versions.length === 0 && <div className="empty-inline">尚无版本。</div>}</div>
        {diffResult && <pre className="case-diff-result">{JSON.stringify(diffResult, null, 2)}</pre>}
      </Card>
    </div>}
    {error && <div className="alert alert-error page-alert" role="alert">{error}</div>}{notice && <div className="alert alert-success page-alert" role="status">{notice}</div>}
  </div>;
}
