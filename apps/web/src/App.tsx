import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Activity, Boxes, Building2, CalendarClock, ChevronDown, CircleHelp, FileCode2, KeyRound, Layers3, LogOut, Monitor, Play, Plus, RefreshCw, ShieldCheck, Sparkles, UsersRound, Workflow } from "lucide-react";
import { api, ApiError, jsonBody, type ApiToken, type AuthResult, type Environment, type EnvironmentSnapshot, type Organization, type Project, type SecretRef, type Session } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { CasesPage } from "./CasesPage";
import { RunConsolePage } from "./RunConsolePage";
import { PlanningPage } from "./PlanningPage";
import "./styles.css";
import "./members.css";
import "./environments.css";
import "./cases.css";
import "./planning.css";
import "./run-console.css";

type AuthMode = "login" | "register" | "verify";
type Page = "overview" | "projects" | "members" | "environments" | "cases" | "planning" | "runs" | "sessions" | "access";
type Readiness = { status: "ready" | "not_ready"; checks: Record<string, string> };
let sessionRefreshPromise: Promise<AuthResult> | undefined;

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "发生未知错误，请稍后重试。";
}

export function LoginScreen({ onAuthenticated }: { onAuthenticated: (auth: AuthResult) => void }) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [verificationToken, setVerificationToken] = useState("");
  const [deviceLabel, setDeviceLabel] = useState("Web browser");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(""); setNotice(""); setBusy(true);
    try {
      if (mode === "login") {
        const auth = await api<AuthResult>("/api/v1/auth/login", jsonBody({ email, password, device_label: deviceLabel }));
        onAuthenticated(auth);
      } else if (mode === "register") {
        await api("/api/v1/auth/register", jsonBody({ email, password, display_name: displayName }));
        setMode("verify");
        setNotice("账户已创建为待验证状态。请打开邮件中的验证链接，或粘贴验证令牌。未收到邮件时可重新发送。");
      } else {
        await api("/api/v1/auth/verify-email", jsonBody({ token: verificationToken }));
        setMode("login");
        setNotice("邮箱验证成功，请登录。");
      }
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function resend() {
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/api/v1/auth/verification/resend", jsonBody({ email }));
      setNotice("如果该邮箱存在待验证账户，验证邮件已重新发送。");
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  const title = mode === "login" ? "欢迎回来" : mode === "register" ? "创建 TestPilot 账户" : "验证邮箱";
  return (
    <main className="auth-layout">
      <section className="auth-story">
        <a className="brand brand-large" href="#home"><span className="brand-mark"><Workflow size={22} /></span> TestPilot <b>AI</b></a>
        <div className="story-copy">
          <div className="eyebrow"><Sparkles size={15} /> AI WEB TESTING PLATFORM</div>
          <h1>把复杂的 Web 测试，<br /><em>交给智能体协作。</em></h1>
          <p>从测试资产到运行证据，在一个可靠、可追溯的工作空间里完成端到端质量验证。</p>
          <div className="story-points">
            <div><span><ShieldCheck size={18} /></span><div><b>确定性判定</b><small>测试结果由断言决定，Agent 负责执行与分析</small></div></div>
            <div><span><Layers3 size={18} /></span><div><b>完整运行证据</b><small>步骤、截图与报告关联到不可变运行事实</small></div></div>
          </div>
        </div>
        <div className="story-footer">TestPilot AI · Enterprise Web Quality</div>
      </section>
      <section className="auth-panel">
        <div className="auth-mobile-brand brand"><span className="brand-mark"><Workflow size={20} /></span> TestPilot <b>AI</b></div>
        <Card className="auth-card">
          <div className="auth-card-heading"><div className="eyebrow muted">SECURE WORKSPACE</div><h2>{title}</h2><p>{mode === "login" ? "登录以访问组织和项目。" : mode === "register" ? "使用工作邮箱创建账户，完成验证后即可登录。" : "验证完成后，账户才会激活。"}</p></div>
          <form onSubmit={submit} className="form-stack">
            {mode === "register" && <div><Label htmlFor="display-name">姓名</Label><Input id="display-name" autoComplete="name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required maxLength={80} /></div>}
            {mode !== "verify" && <div><Label htmlFor="email">工作邮箱</Label><Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required maxLength={254} /></div>}
            {mode !== "verify" && <div><Label htmlFor="password">密码</Label><Input id="password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} value={password} onChange={(e) => setPassword(e.target.value)} required minLength={mode === "register" ? 12 : 1} maxLength={128} /></div>}
            {mode === "login" && <div><Label htmlFor="device">设备标签</Label><Input id="device" value={deviceLabel} onChange={(e) => setDeviceLabel(e.target.value)} maxLength={80} /></div>}
            {mode === "verify" && <><div><Label htmlFor="verify-token">邮件验证令牌</Label><Input id="verify-token" value={verificationToken} onChange={(e) => setVerificationToken(e.target.value)} required minLength={32} maxLength={256} /></div><div><Label htmlFor="verify-email">邮箱</Label><Input id="verify-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></div></>}
            {error && <div className="alert alert-error" role="alert">{error}</div>}
            {notice && <div className="alert alert-success" role="status">{notice}</div>}
            <Button type="submit" disabled={busy}>{busy ? "请稍候…" : mode === "login" ? "登录" : mode === "register" ? "创建账户" : "验证邮箱"}</Button>
          </form>
          <div className="auth-actions">
            {mode === "login" && <button type="button" className="text-button" onClick={() => { setMode("register"); setError(""); setNotice(""); }}>创建新账户</button>}
            {mode === "register" && <button type="button" className="text-button" onClick={() => { setMode("login"); setError(""); setNotice(""); }}>返回登录</button>}
            {mode === "verify" && <><button type="button" className="text-button" onClick={resend} disabled={busy}>重新发送验证邮件</button><button type="button" className="text-button" onClick={() => setMode("login")}>返回登录</button></>}
          </div>
          <div className="security-note"><ShieldCheck size={16} /> Refresh 会话凭证使用 HttpOnly Secure Cookie</div>
        </Card>
        <div className="auth-legal">登录即表示你已获授权访问此工作区。</div>
      </section>
    </main>
  );
}

function AppShell({ auth, onSignOut }: { auth: AuthResult; onSignOut: () => void }) {
  const [page, setPage] = useState<Page>("overview");
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [loading, setLoading] = useState(true);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [error, setError] = useState("");
  const [showOrgForm, setShowOrgForm] = useState(false);
  const [showProjectForm, setShowProjectForm] = useState(false);

  const loadWorkspace = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [orgPage, projectList, sessionPage] = await Promise.all([
        api<{ items: Organization[]; next_cursor: string | null }>("/api/v1/organizations", {}, auth.access_token),
        api<Project[]>("/api/v1/projects", {}, auth.access_token),
        api<{ sessions: Session[] }>("/api/v1/auth/sessions", {}, auth.access_token),
      ]);
      setOrganizations(orgPage.items); setProjects(projectList); setSessions(sessionPage.sessions);
      setSelectedProjectId((current) => current && projectList.some((project) => project.id === current) ? current : (projectList[0]?.id ?? ""));
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) onSignOut();
      else setError(messageOf(caught));
    } finally { setLoading(false); }
  }, [auth.access_token, onSignOut]);

  useEffect(() => { void loadWorkspace(); }, [loadWorkspace]);
  useEffect(() => {
    let active = true;
    fetch("/api/v1/health/ready").then(async (response) => {
      const report = await response.json() as Readiness;
      if (active) setReadiness(report);
    }).catch(() => { if (active) setReadiness(null); });
    return () => { active = false; };
  }, []);

  const selectedProject = useMemo(() => projects.find((project) => project.id === selectedProjectId), [projects, selectedProjectId]);
  const pageTitle = { overview: "工作台", projects: "组织与项目", members: "成员与权限", environments: "测试环境", cases: "用例与版本", planning: "生成、套件与调度", sessions: "安全与会话", runs: "运行控制台", access: "邀请与凭据" }[page];

  async function createOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    setError("");
    try {
      await api<Organization>("/api/v1/organizations", jsonBody({ name: data.get("name"), slug: data.get("slug") }), auth.access_token);
      setShowOrgForm(false); await loadWorkspace();
    } catch (caught) { setError(messageOf(caught)); }
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    setError("");
    try {
      const project = await api<Project>("/api/v1/projects", jsonBody({ organization_id: data.get("organization_id"), name: data.get("name"), slug: data.get("slug") }), auth.access_token);
      setShowProjectForm(false); await loadWorkspace(); setSelectedProjectId(project.id);
    } catch (caught) { setError(messageOf(caught)); }
  }

  async function revokeSession(sessionId: string) {
    setError("");
    try { await api(`/api/v1/auth/sessions/${sessionId}`, { method: "DELETE" }, auth.access_token); await loadWorkspace(); }
    catch (caught) { setError(messageOf(caught)); }
  }

  async function signOut() {
    try { await api("/api/v1/auth/logout", jsonBody({ all_sessions: false }), auth.access_token); }
    catch { /* Clear local access even when the server session already expired. */ }
    onSignOut();
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#workspace"><span className="brand-mark"><Workflow size={19} /></span> TestPilot <b>AI</b></a>
        <div className="workspace-switch"><span className="workspace-avatar">{(organizations[0]?.name ?? "T").slice(0, 1).toUpperCase()}</span><div><small>工作空间</small><strong>{organizations[0]?.name ?? "我的工作空间"}</strong></div><ChevronDown size={16} /></div>
        <div className="nav-label">工作区</div>
        <nav className="main-nav" aria-label="主导航">
          <NavItem icon={<Activity size={17} />} label="工作台" active={page === "overview"} onClick={() => setPage("overview")} />
          <NavItem icon={<Boxes size={17} />} label="组织与项目" active={page === "projects"} onClick={() => setPage("projects")} />
          <NavItem icon={<UsersRound size={17} />} label="成员与权限" active={page === "members"} onClick={() => setPage("members")} />
          <NavItem icon={<Layers3 size={17} />} label="测试环境" active={page === "environments"} onClick={() => setPage("environments")} />
          <NavItem icon={<FileCode2 size={17} />} label="用例与版本" active={page === "cases"} onClick={() => setPage("cases")} />
          <NavItem icon={<CalendarClock size={17} />} label="生成、套件与调度" active={page === "planning"} onClick={() => setPage("planning")} />
          <NavItem icon={<Play size={17} />} label="运行控制台" active={page === "runs"} onClick={() => setPage("runs")} />
        </nav>
        <div className="nav-label nav-label-spaced">账户</div>
        <nav className="main-nav" aria-label="账户导航">
          <NavItem icon={<Monitor size={17} />} label="安全与会话" active={page === "sessions"} onClick={() => setPage("sessions")} />
          <NavItem icon={<KeyRound size={17} />} label="邀请与凭据" active={page === "access"} onClick={() => setPage("access")} />
        </nav>
        <div className="sidebar-bottom"><div className="help-card"><span className="help-icon"><CircleHelp size={16} /></span><div><b>需要帮助？</b><small>查看平台使用指南</small></div></div><div className="sidebar-version">TestPilot AI <span>V10.3</span></div></div>
      </aside>
      <main className="main-area">
        <header className="topbar"><div className="breadcrumbs">工作区 <span>/</span> <strong>{pageTitle}</strong></div><div className="top-actions"><Button variant="ghost" className="icon-button" aria-label="刷新数据" onClick={() => void loadWorkspace()}><RefreshCw size={16} /></Button><div className="user-chip"><div className="user-avatar">{(auth.user.display_name || auth.user.email).slice(0, 1).toUpperCase()}</div><div><strong>{auth.user.display_name}</strong><small>{auth.user.email}</small></div><Button variant="ghost" className="icon-button" aria-label="退出登录" onClick={() => void signOut()}><LogOut size={16} /></Button></div></div></header>
        <section className="page-content">
          <div className="page-heading"><div><div className="eyebrow muted">TESTPILOT AI · PLATFORM</div><h1>{pageTitle}</h1><p>{page === "overview" ? "管理组织、项目和运行环境，准备开展可追溯的自动化测试。" : page === "projects" ? "配置团队工作空间，并管理项目级测试权限。" : page === "members" ? "查看项目成员，并按角色控制访问范围。" : page === "environments" ? "管理测试目标和不可变运行快照。" : page === "cases" ? "维护测试用例和版本审核；DSL 细节及历史兼容按契约清单处理。" : page === "planning" ? "管理 AI 生成任务、测试套件、执行计划与调度。" : page === "runs" ? "创建和观察 Run；数据库快照是状态依据，事件流用于进度更新。" : page === "access" ? "发出一次性成员邀请，管理 API Token 与外部密钥版本引用。" : "查看登录设备，并撤销不再使用的访问。"}</p></div><div className="heading-meta" title={readiness ? JSON.stringify(readiness.checks) : "尚未获取就绪状态"}><span className={`system-dot ${readiness?.status === "not_ready" ? "system-dot-error" : readiness ? "" : "system-dot-muted"}`} />{readiness?.status === "ready" ? "API 依赖已就绪" : readiness?.status === "not_ready" ? "API 依赖未就绪" : "API 状态不可用"}</div></div>
          {error && <div className="alert alert-error page-alert" role="alert">{error}</div>}
          {loading ? <Loading /> : page === "overview" ? <Overview organizations={organizations} projects={projects} sessions={sessions} onNavigate={setPage} />
            : page === "projects" ? <ProjectsPage organizations={organizations} projects={projects} showOrgForm={showOrgForm} showProjectForm={showProjectForm} setShowOrgForm={setShowOrgForm} setShowProjectForm={setShowProjectForm} onCreateOrg={createOrganization} onCreateProject={createProject} onSelectProject={(projectId) => { setSelectedProjectId(projectId); setPage("members"); }} />
              : page === "members" ? <MembersPage project={selectedProject} accessToken={auth.access_token} />
              : page === "cases" ? <CasesPage projects={projects} project={selectedProject} onSelectProject={setSelectedProjectId} accessToken={auth.access_token} />
              : page === "planning" ? <PlanningPage projects={projects} project={selectedProject} onSelectProject={setSelectedProjectId} accessToken={auth.access_token} />
              : page === "runs" ? <RunConsolePage project={selectedProject} accessToken={auth.access_token} />
                : page === "sessions" ? <SessionsPage sessions={sessions} onRevoke={revokeSession} />
                : page === "access" ? <AccessPage organizations={organizations} projects={projects} accessToken={auth.access_token} />
                  : <EnvironmentPage project={selectedProject} accessToken={auth.access_token} />}
        </section>
      </main>
    </div>
  );
}

function NavItem({ icon, label, active, onClick }: { icon: ReactNode; label: string; active: boolean; onClick: () => void }) {
  return <button type="button" onClick={onClick} className={`nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined}>{icon}<span>{label}</span>{active && <i />}</button>;
}

function Loading() { return <div className="loading-panel"><span className="spinner" />正在安全加载工作区…</div>; }

function Overview({ organizations, projects, sessions, onNavigate }: { organizations: Organization[]; projects: Project[]; sessions: Session[]; onNavigate: (page: Page) => void }) {
  return <>
    <div className="welcome-banner"><div className="welcome-pattern" /><div className="welcome-copy"><span className="welcome-kicker"><Sparkles size={14} /> QUALITY ENGINEERING, REIMAGINED</span><h2>为下一次可靠发布做好准备。</h2><p>先组织项目与测试环境；运行和分析能力将在后续模块接入。</p></div><div className="welcome-art"><div className="art-orbit orbit-one" /><div className="art-orbit orbit-two" /><div className="art-core"><Workflow size={28} /></div><span className="art-node node-a" /><span className="art-node node-b" /><span className="art-node node-c" /></div></div>
    <div className="stat-grid"><StatCard icon={<Building2 />} label="组织" value={organizations.length} note="你有权访问的工作空间" tone="blue" /><StatCard icon={<Boxes />} label="项目" value={projects.length} note="已配置的测试项目" tone="violet" /><StatCard icon={<Monitor />} label="活跃会话" value={sessions.filter((item) => !item.revoked_at).length} note="当前账户的设备会话" tone="green" /></div>
    <div className="section-heading"><div><h2>开始使用</h2><p>从基础工作区配置开始，逐步建立测试资产。</p></div></div>
    <div className="quick-grid"><QuickCard icon={<Building2 />} index="01" title="配置组织与项目" text="建立项目边界，并分配团队访问权限。" onClick={() => onNavigate("projects")} /><QuickCard icon={<Layers3 />} index="02" title="添加测试环境" text="登记测试目标并关联受控的密钥引用。" onClick={() => onNavigate("environments")} /><QuickCard icon={<KeyRound />} index="03" title="检查账户安全" text="查看登录设备，及时撤销旧会话。" onClick={() => onNavigate("sessions")} /></div>
    <div className="info-strip"><ShieldCheck size={17} /><span>执行结果由确定性断言判定。AI Agent 负责规划、探索、恢复建议与分析，不会自行决定测试通过。</span></div>
  </>;
}

function StatCard({ icon, label, value, note, tone }: { icon: ReactNode; label: string; value: number; note: string; tone: string }) {
  return <Card className="stat-card"><span className={`stat-icon ${tone}`}>{icon}</span><div className="stat-content"><div className="stat-label">{label}</div><div className="stat-value">{value}</div><div className="stat-note">{note}</div></div></Card>;
}

function QuickCard({ icon, index, title, text, onClick }: { icon: ReactNode; index: string; title: string; text: string; onClick: () => void }) {
  return <button className="quick-card" type="button" onClick={onClick}><div className="quick-top"><span className="quick-icon">{icon}</span><small>{index}</small></div><h3>{title}</h3><p>{text}</p><span className="quick-link">打开 <span>→</span></span></button>;
}

function ProjectsPage({ organizations, projects, showOrgForm, showProjectForm, setShowOrgForm, setShowProjectForm, onCreateOrg, onCreateProject, onSelectProject }: {
  organizations: Organization[]; projects: Project[]; showOrgForm: boolean; showProjectForm: boolean;
  setShowOrgForm: (value: boolean) => void; setShowProjectForm: (value: boolean) => void;
  onCreateOrg: (event: FormEvent<HTMLFormElement>) => void; onCreateProject: (event: FormEvent<HTMLFormElement>) => void;
  onSelectProject: (projectId: string) => void;
}) {
  return <>
    <div className="action-row"><div className="scope-summary"><span className="mini-icon"><Building2 size={16} /></span><span>可访问 <b>{organizations.length}</b> 个组织、<b>{projects.length}</b> 个项目</span></div><div className="button-row"><Button variant="secondary" onClick={() => setShowOrgForm(!showOrgForm)}><Plus size={16} /> 新建组织</Button><Button onClick={() => setShowProjectForm(!showProjectForm)} disabled={organizations.length === 0}><Plus size={16} /> 新建项目</Button></div></div>
    {showOrgForm && <Card className="form-card"><div className="form-card-title"><div><CardTitle>创建组织</CardTitle><p>组织名称和 slug 均需显式填写。</p></div><Button variant="ghost" onClick={() => setShowOrgForm(false)}>取消</Button></div><form className="inline-form" onSubmit={onCreateOrg}><div><Label htmlFor="org-name">组织名称</Label><Input id="org-name" name="name" required minLength={1} /></div><div><Label htmlFor="org-slug">组织 slug</Label><Input id="org-slug" name="slug" required minLength={1} /></div><Button type="submit">创建组织</Button></form></Card>}
    {showProjectForm && <Card className="form-card"><div className="form-card-title"><div><CardTitle>创建项目</CardTitle><p>项目创建者自动获得 project_admin。</p></div><Button variant="ghost" onClick={() => setShowProjectForm(false)}>取消</Button></div><form className="inline-form" onSubmit={onCreateProject}><div><Label htmlFor="project-org">所属组织</Label><select id="project-org" className="input" name="organization_id" required>{organizations.map((org) => <option key={org.id} value={org.id}>{org.name} · {org.role}</option>)}</select></div><div><Label htmlFor="project-name">项目名称</Label><Input id="project-name" name="name" required maxLength={120} /></div><div><Label htmlFor="project-slug">项目 slug</Label><Input id="project-slug" name="slug" required pattern="[a-z0-9-]{2,64}" maxLength={64} /></div><Button type="submit">创建项目</Button></form></Card>}
    <div className="section-heading section-heading-spaced"><div><h2>项目列表</h2><p>项目权限由项目成员角色与组织管理员权限共同决定。</p></div></div>
    {projects.length === 0 ? <EmptyState icon={<Boxes />} title="还没有项目" text={organizations.length ? "为组织创建第一个项目，开始配置测试空间。" : "先创建组织，再添加项目。"} /> : <div className="project-grid">{projects.map((project) => <Card className="project-card" key={project.id}><div className="project-top"><span className="project-logo">{project.name.slice(0, 1).toUpperCase()}</span><span className={`role-pill ${project.role === "project_admin" ? "role-admin" : ""}`}>{project.role ?? "组织管理员"}</span></div><h3>{project.name}</h3><p>{organizations.find((org) => org.id === project.organization_id)?.name ?? "组织"} <span>·</span> {project.slug}</p><div className="project-footer"><span><span className="system-dot" /> 项目空间</span><Button variant="ghost" className="small-action" onClick={() => onSelectProject(project.id)}>查看项目 <span>→</span></Button></div></Card>)}</div>}
  </>;
}

export function MembersPage({ project, accessToken }: { project: Project | undefined; accessToken: string }) {
  const [members, setMembers] = useState<Array<{ user_id: string; email: string; display_name: string; role: string }>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [organizationMembers, setOrganizationMembers] = useState<Array<{ user_id: string; email: string; display_name: string; role: string }>>([]);
  const [organizationMembersCursor, setOrganizationMembersCursor] = useState<string | null>(null);
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("tester");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadMembers = useCallback(async (cursor?: string, append = false) => {
    if (!project) { setMembers([]); setNextCursor(null); return; }
    setBusy(true); setError("");
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (cursor) query.set("cursor", cursor);
      const page = await api<{ items: typeof members; next_cursor: string | null }>(`/api/v1/projects/${project.id}/members?${query}`, {}, accessToken);
      setMembers((current) => append ? [...current, ...page.items] : page.items);
      setNextCursor(page.next_cursor);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }, [accessToken, project]);

  const loadOrganizationMembers = useCallback(async (cursor?: string, append = false) => {
    if (!project) { setOrganizationMembers([]); setOrganizationMembersCursor(null); return; }
    setError("");
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (cursor) query.set("cursor", cursor);
      const page = await api<{ items: typeof organizationMembers; next_cursor: string | null }>(`/api/v1/organizations/${project.organization_id}/members?${query}`, {}, accessToken);
      setOrganizationMembers((current) => append ? [...current, ...page.items] : page.items);
      setOrganizationMembersCursor(page.next_cursor);
    } catch (caught) { setError(messageOf(caught)); }
  }, [accessToken, project]);

  useEffect(() => { void loadMembers(); void loadOrganizationMembers(); }, [loadMembers, loadOrganizationMembers]);

  async function saveMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!project) return;
    setBusy(true); setError("");
    try {
      await api(`/api/v1/projects/${project.id}/members/${encodeURIComponent(userId)}`, { method: "PUT", body: JSON.stringify({ role }) }, accessToken);
      setUserId(""); await loadMembers();
    } catch (caught) { setError(messageOf(caught)); setBusy(false); }
  }

  async function removeMember(targetUserId: string) {
    if (!project) return;
    setBusy(true); setError("");
    try {
      await api(`/api/v1/projects/${project.id}/members/${encodeURIComponent(targetUserId)}`, { method: "DELETE" }, accessToken);
      await loadMembers();
    } catch (caught) { setError(messageOf(caught)); setBusy(false); }
  }

  return <>
    <Card className="security-banner"><span className="security-banner-icon"><UsersRound /></span><div><h2>{project?.name ?? "尚未选择项目"}</h2><p>项目管理员可调整项目角色。组织管理员拥有该组织全部项目的隐式管理权限。</p></div></Card>
    {project && <Card className="form-card snapshot-lookup"><div className="form-card-title"><div><CardTitle>授予项目角色</CardTitle><p>只能选择该项目所属组织的成员。</p></div></div><form className="inline-form" onSubmit={saveMember}><div><Label htmlFor="member-user-id">组织成员</Label><select id="member-user-id" className="input" value={userId} onChange={(event) => setUserId(event.target.value)} required><option value="">选择组织成员</option>{organizationMembers.map((member) => <option key={member.user_id} value={member.user_id}>{member.display_name} · {member.email} · {member.role}</option>)}</select>{organizationMembersCursor && <button className="text-button member-load-more" type="button" onClick={() => void loadOrganizationMembers(organizationMembersCursor, true)}>加载更多组织成员</button>}</div><div><Label htmlFor="member-role">项目角色</Label><select id="member-role" className="input" value={role} onChange={(event) => setRole(event.target.value)}><option value="project_admin">project_admin</option><option value="tester">tester</option><option value="developer">developer</option><option value="viewer">viewer</option></select></div><Button type="submit" disabled={busy || !organizationMembers.length}><Plus size={15} /> 保存角色</Button></form></Card>}
    {error && <div className="alert alert-error page-alert" role="alert">{error}</div>}
    <div className="section-heading section-heading-spaced"><div><h2>项目成员</h2><p>移除项目成员后，该成员对项目的后续访问会立即失效。</p></div></div>
    {!project ? <EmptyState icon={<UsersRound />} title="先选择一个项目" text="从组织与项目页选择项目后，即可查看成员权限。" /> : members.length === 0 && !busy ? <EmptyState icon={<UsersRound />} title="项目成员列表为空" text="当前用户可能没有项目访问权限，或还没有显式项目成员。" /> : <Card className="table-card"><div className="session-table-head"><span>成员</span><span>用户 ID</span><span>项目角色</span><span>操作</span></div>{members.map((member) => <div className="session-row" key={member.user_id}><div className="device-cell"><span className="device-icon"><UsersRound size={16} /></span><div><b>{member.display_name}</b><small>{member.email}</small></div></div><code className="member-id">{member.user_id}</code><span><span className="role-pill">{member.role}</span></span><div><Button variant="danger" className="revoke-button" disabled={busy} onClick={() => void removeMember(member.user_id)}>移除</Button></div></div>)}{nextCursor && <div className="table-more"><Button variant="secondary" disabled={busy} onClick={() => void loadMembers(nextCursor, true)}>加载更多</Button></div>}</Card>}
  </>;
}

function SessionsPage({ sessions, onRevoke }: { sessions: Session[]; onRevoke: (id: string) => void }) {
  return <>
    <Card className="security-banner"><span className="security-banner-icon"><ShieldCheck /></span><div><h2>账户访问由会话控制</h2><p>Refresh 凭证保存在 HttpOnly Secure Cookie 中；撤销会话后，其访问令牌将无法继续使用。</p></div></Card>
    <div className="section-heading section-heading-spaced"><div><h2>设备会话</h2><p>最近活动时间与设备标签仅用于账户安全审查。</p></div></div>
    {sessions.length === 0 ? <EmptyState icon={<Monitor />} title="没有设备会话" text="登录后会在这里列出当前设备会话。" /> : <Card className="table-card"><div className="session-table-head"><span>设备</span><span>创建时间</span><span>最近使用</span><span>状态 / 操作</span></div>{sessions.map((session) => <div className="session-row" key={session.id}><div className="device-cell"><span className="device-icon"><Monitor size={17} /></span><div><b>{session.device_label}</b><small>Session · {session.id.slice(0, 8)}</small></div></div><span>{formatDate(session.created_at)}</span><span>{formatDate(session.last_used_at)}</span><div>{session.revoked_at ? <span className="status-pill revoked">已撤销</span> : <Button variant="danger" className="revoke-button" onClick={() => onRevoke(session.id)}>撤销</Button>}</div></div>)}</Card>}
  </>;
}

const apiScopes = ["approval:decide", "organization:create", "organization:read", "organization:write", "platform:admin", "resource:read", "resource:write", "run:create", "run:read", "user:self"];

function AccessPage({ organizations, projects, accessToken }: { organizations: Organization[]; projects: Project[]; accessToken: string }) {
  const [organizationId, setOrganizationId] = useState(organizations[0]?.id ?? "");
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [secretRefs, setSecretRefs] = useState<SecretRef[]>([]);
  const [oneTimeToken, setOneTimeToken] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!organizations.length) return;
    setBusy(true); setError("");
    try {
      const [tokenRows, refs] = await Promise.all([
        api<ApiToken[]>("/api/v1/api-tokens", {}, accessToken),
        api<SecretRef[]>("/api/v1/secret-refs", {}, accessToken),
      ]);
      setTokens(tokenRows); setSecretRefs(refs);
      setOrganizationId((current) => organizations.some((org) => org.id === current) ? current : organizations[0].id);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }, [accessToken, organizations]);
  useEffect(() => { void load(); }, [load]);

  async function issueInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const projectId = String(data.get("project_id") ?? "");
    setBusy(true); setError(""); setNotice(""); setOneTimeToken("");
    try {
      const invite = await api<{ token: string }>(`/api/v1/organizations/${organizationId}/invitations`, jsonBody({
        email: data.get("email"), role: data.get("role"), project_id: projectId || null,
        expires_at: new Date(String(data.get("expires_at"))).toISOString(),
      }), accessToken);
      setOneTimeToken(invite.token); setNotice("邀请已创建。完整令牌仅显示本次，请安全发送给受邀成员。");
      form.reset();
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function issueApiToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const scopes = data.getAll("scopes").map(String);
    setBusy(true); setError(""); setNotice(""); setOneTimeToken("");
    try {
      const token = await api<ApiToken>(`/api/v1/organizations/${organizationId}/api-tokens`, jsonBody({
        name: data.get("token_name"), scopes,
        expires_at: data.get("token_expires") ? new Date(String(data.get("token_expires"))).toISOString() : null,
      }), accessToken);
      setOneTimeToken(token.token ?? ""); setNotice("API Token 已创建。明文只会显示本次，请立即复制保存。");
      form.reset(); await load();
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function createSecretRef(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    setBusy(true); setError(""); setNotice("");
    try {
      await api(`/api/v1/organizations/${organizationId}/secret-refs`, jsonBody({
        provider: data.get("provider"), external_key: data.get("external_key"), provider_version: data.get("provider_version"),
      }), accessToken);
      setNotice("SecretRef 元数据已创建。实际密钥值需在外部密钥提供方管理。"); form.reset(); await load();
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function rotateToken(id: string) {
    setBusy(true); setError(""); setNotice(""); setOneTimeToken("");
    try { const token = await api<ApiToken>(`/api/v1/api-tokens/${id}/rotate`, jsonBody({}), accessToken); setOneTimeToken(token.token ?? ""); setNotice("API Token 已轮换，旧明文立即失效。新明文仅显示本次。"); await load(); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function revokeToken(id: string) {
    setBusy(true); setError("");
    try { await api(`/api/v1/api-tokens/${id}`, { method: "DELETE" }, accessToken); await load(); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }
  async function rotateSecret(id: string, providerVersion: string) {
    setBusy(true); setError("");
    try { await api(`/api/v1/organizations/${organizationId}/secret-refs/${id}/rotate`, jsonBody({ provider_version: providerVersion }), accessToken); setNotice("SecretRef 指向新版本；旧版本已退役。"); await load(); }
    catch (caught) { setError(messageOf(caught)); } finally { setBusy(false); }
  }

  const projectsForOrg = projects.filter((project) => project.organization_id === organizationId);
  return <>
    <VerificationOutboxAdmin accessToken={accessToken} />
    <GenerationOutboxAdmin accessToken={accessToken} />
    {!organizations.length ? <EmptyState icon={<Building2 />} title="先创建组织" text="邀请、API Token 和 SecretRef 都属于组织范围。" /> : <>
      <Card className="form-card snapshot-lookup"><div className="form-card-title"><div><CardTitle>组织范围</CardTitle><p>写入操作要求组织管理员；项目邀请还允许对应项目管理员。</p></div><Button variant="secondary" type="button" onClick={() => void load()} disabled={busy}>刷新</Button></div>
        <div className="environment-picker"><Label htmlFor="access-organization">组织</Label><select id="access-organization" className="input" value={organizationId} onChange={(event) => setOrganizationId(event.target.value)}>{organizations.map((org) => <option key={org.id} value={org.id}>{org.name} · {org.role}</option>)}</select></div>
      </Card>
      <div className="access-grid">
        <Card className="form-card"><div className="form-card-title"><div><CardTitle>成员邀请</CardTitle><p>显式指定过期时间；接受时要求已验证账户邮箱完全匹配。</p></div></div>
          <form className="form-stack" onSubmit={issueInvitation}><div><Label htmlFor="invite-email">邮箱</Label><Input id="invite-email" name="email" type="email" required /></div><div><Label htmlFor="invite-role">邀请角色</Label><select id="invite-role" name="role" className="input"><option value="org_member">org_member</option><option value="org_admin">org_admin</option>{projectsForOrg.length > 0 && <><option value="project_admin">project_admin</option><option value="tester">tester</option><option value="developer">developer</option><option value="viewer">viewer</option></>}</select></div>{projectsForOrg.length > 0 && <div><Label htmlFor="invite-project">项目（项目角色时选择）</Label><select id="invite-project" name="project_id" className="input"><option value="">组织邀请</option>{projectsForOrg.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></div>}<div><Label htmlFor="invite-expiry">过期时间</Label><Input id="invite-expiry" name="expires_at" type="datetime-local" required /></div><Button type="submit" disabled={busy}>创建邀请</Button></form>
        </Card>
        <Card className="form-card"><div className="form-card-title"><div><CardTitle>API Token</CardTitle><p>权限范围限于 OpenAPI 权限注册表，完整密钥只显示一次。</p></div></div>
          <form className="form-stack" onSubmit={issueApiToken}><div><Label htmlFor="token-name">名称</Label><Input id="token-name" name="token_name" required maxLength={120} /></div><div className="scope-checks">{apiScopes.map((scope) => { const globalScope = scope === "organization:create" || scope === "platform:admin"; return <label key={scope} title={globalScope ? "组织绑定的 Token 不授予全局权限" : undefined}><input type="checkbox" name="scopes" value={scope} disabled={globalScope} />{scope}{globalScope ? "（全局）" : ""}</label>; })}</div><small className="scope-note">Token 绑定到一个组织；组织级 Token 不授予全局组织创建或平台管理员权限。</small><div><Label htmlFor="token-expires">过期时间（可选）</Label><Input id="token-expires" name="token_expires" type="datetime-local" /></div><Button type="submit" disabled={busy}>创建 API Token</Button></form>
          <div className="access-token-list">{tokens.map((token) => <div className="access-token-row" key={token.id}><div><b>{token.name}</b><small>{token.scopes.join(", ")} · {token.revoked_at ? "已撤销" : "有效"}</small></div>{!token.revoked_at && <div className="button-row"><Button variant="secondary" disabled={busy} onClick={() => void rotateToken(token.id)}>轮换</Button><Button variant="danger" disabled={busy} onClick={() => void revokeToken(token.id)}>撤销</Button></div>}</div>)}</div>
        </Card>
        <Card className="form-card"><div className="form-card-title"><div><CardTitle>SecretRef 外部引用</CardTitle><p>仅登记密钥提供方的引用与版本指针，不传输或保存密钥值。</p></div></div>
          <form className="form-stack" onSubmit={createSecretRef}><div><Label htmlFor="secret-provider">提供方</Label><Input id="secret-provider" name="provider" required placeholder="例如 vault" /></div><div><Label htmlFor="secret-key">外部键</Label><Input id="secret-key" name="external_key" required /></div><div><Label htmlFor="secret-version">提供方版本标识</Label><Input id="secret-version" name="provider_version" required /></div><Button type="submit" disabled={busy}>登记 SecretRef</Button></form>
          <div className="access-token-list">{secretRefs.filter((ref) => ref.organization_id === organizationId).map((ref) => <div className="access-token-row" key={ref.id}><div><b>{ref.provider} · {ref.external_key}</b><small>活动版本 {ref.active_version ?? "未读取"}</small></div><form className="button-row" onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void rotateSecret(ref.id, String(data.get("version"))); }}><Input name="version" aria-label={`新版本 ${ref.external_key}`} required placeholder="新版本 ID" /><Button variant="secondary" disabled={busy}>轮换</Button></form></div>)}</div>
        </Card>
      </div>
      {error && <div className="alert alert-error page-alert" role="alert">{error}</div>}{notice && <div className="alert alert-success page-alert" role="status">{notice}</div>}{oneTimeToken && <Card className="one-time-token"><b>本次可见的一次性令牌</b><code>{oneTimeToken}</code></Card>}
    </>}
  </>;
}

function VerificationOutboxAdmin({ accessToken }: { accessToken: string }) {
  type DeadEmail = { id: string; recipient_email: string; status: "DEAD"; attempts: number; last_error: string | null; created_at: string; expires_at: string };
  const [authorized, setAuthorized] = useState(false);
  const [checked, setChecked] = useState(false);
  const [items, setItems] = useState<DeadEmail[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const page = await api<{ items: DeadEmail[] }>("/api/v1/admin/verification-email-outbox/dead?limit=50", {}, accessToken);
      setItems(page.items); setAuthorized(true);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) setAuthorized(false);
      else { setAuthorized(true); setError(messageOf(caught)); }
    } finally { setChecked(true); setBusy(false); }
  }, [accessToken]);

  useEffect(() => { void load(); }, [load]);
  if (!checked || !authorized) return null;
  return <Card className="form-card snapshot-lookup">
    <div className="form-card-title"><div><CardTitle>验证邮件死信</CardTitle><p>仅平台管理员会话可查看失败投递；令牌密文不会通过此接口返回。</p></div><Button type="button" variant="secondary" onClick={() => void load()} disabled={busy}>刷新</Button></div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {!error && items.length === 0 ? <p className="empty-inline">当前没有死信邮件。</p> : <div className="access-token-list">{items.map((item) => <div className="access-token-row" key={item.id}><div><b>{item.recipient_email}</b><small>{item.attempts} 次尝试 · 过期 {formatDate(item.expires_at)}</small></div><span className="status-pill revoked">{item.last_error ?? "DELIVERY_FAILED"}</span></div>)}</div>}
  </Card>;
}

function GenerationOutboxAdmin({ accessToken }: { accessToken: string }) {
  type DeadEvent = { id: number; aggregate_id: string; event_type: string; status: "DEAD"; attempts: number; last_error: string | null; created_at: string; available_at: string };
  const [authorized, setAuthorized] = useState(false);
  const [checked, setChecked] = useState(false);
  const [items, setItems] = useState<DeadEvent[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const page = await api<{ items: DeadEvent[] }>("/api/v1/admin/generation-outbox/dead?limit=50", {}, accessToken);
      setItems(page.items); setAuthorized(true);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) setAuthorized(false);
      else { setAuthorized(true); setError(messageOf(caught)); }
    } finally { setChecked(true); setBusy(false); }
  }, [accessToken]);
  useEffect(() => { void load(); }, [load]);
  if (!checked || !authorized) return null;
  return <Card className="form-card snapshot-lookup">
    <div className="form-card-title"><div><CardTitle>生成任务 Outbox 死信</CardTitle><p>平台管理员会话可查看无法投递的生成任务事件；不会展示提示词或令牌。</p></div><Button type="button" variant="secondary" onClick={() => void load()} disabled={busy}>刷新</Button></div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {!error && items.length === 0 ? <p className="empty-inline">当前没有生成任务死信。</p> : <div className="access-token-list">{items.map((item) => <div className="access-token-row" key={item.id}><div><b>{item.event_type} · {item.aggregate_id}</b><small>{item.attempts} 次尝试 · {formatDate(item.created_at)}</small></div><span className="status-pill revoked">{item.last_error ?? "DISPATCH_FAILED"}</span></div>)}</div>}
  </Card>;
}

export function EnvironmentPage({ project, accessToken }: { project: Project | undefined; accessToken: string }) {
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [secretRefs, setSecretRefs] = useState<SecretRef[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [snapshots, setSnapshots] = useState<EnvironmentSnapshot[] | null>(null);
  const [healthResult, setHealthResult] = useState<{ id: string; status: "HEALTHY" | "UNHEALTHY" | "BLOCKED" | "ERROR"; checked_at: string; http_status: number | null; error_code: string | null } | null>(null);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [networkPolicy, setNetworkPolicy] = useState("{}");
  const [secretRefId, setSecretRefId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadEnvironments = useCallback(async (cursor?: string, append = false) => {
    if (!project) { setEnvironments([]); setNextCursor(null); setSelectedId(""); return; }
    setLoading(true); setError("");
    try {
      const query = new URLSearchParams({ limit: "30" });
      if (cursor) query.set("cursor", cursor);
      const [page, refs] = await Promise.all([
        api<{ items: Environment[]; next_cursor: string | null }>(`/api/v1/projects/${project.id}/environments?${query}`, {}, accessToken),
        api<SecretRef[]>("/api/v1/secret-refs", {}, accessToken),
      ]);
      setSecretRefs(refs.filter((ref) => ref.organization_id === project.organization_id));
      setEnvironments((current) => append ? [...current, ...page.items] : page.items);
      setNextCursor(page.next_cursor);
      setSelectedId((current) => current && (append || page.items.some((item) => item.id === current)) ? current : (page.items[0]?.id ?? ""));
    } catch (caught) { setError(messageOf(caught)); }
    finally { setLoading(false); }
  }, [accessToken, project]);

  useEffect(() => { void loadEnvironments(); setSnapshots(null); }, [loadEnvironments]);

  const selectedEnvironment = environments.find((environment) => environment.id === selectedId);
  useEffect(() => {
    setName(selectedEnvironment?.name ?? "");
    setBaseUrl(selectedEnvironment?.base_url ?? "");
    setNetworkPolicy(JSON.stringify(selectedEnvironment?.network_policy ?? {}, null, 2));
    setSecretRefId(selectedEnvironment?.secret_ref_id ?? "");
    setSnapshots(null);
  }, [selectedEnvironment?.id, selectedEnvironment?.name, selectedEnvironment?.base_url, selectedEnvironment?.network_policy, selectedEnvironment?.secret_ref_id]);

  async function saveEnvironment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!project) return;
    setBusy(true); setError("");
    try {
      const parsedNetworkPolicy = JSON.parse(networkPolicy) as Record<string, unknown>;
      if (!parsedNetworkPolicy || Array.isArray(parsedNetworkPolicy) || typeof parsedNetworkPolicy !== "object") {
        throw new Error("网络策略必须是 JSON 对象。");
      }
      if (selectedEnvironment) {
        const updated = await api<Environment>(`/api/v1/environments/${selectedEnvironment.id}`, {
          method: "PATCH", body: JSON.stringify({ name, base_url: baseUrl, network_policy: parsedNetworkPolicy, secret_ref_id: secretRefId || null }),
        }, accessToken);
        setEnvironments((current) => current.map((environment) => environment.id === updated.id ? updated : environment));
      } else {
        const created = await api<Environment>(`/api/v1/projects/${project.id}/environments`, jsonBody({ name, base_url: baseUrl, network_policy: parsedNetworkPolicy, secret_ref_id: secretRefId || null }), accessToken);
        setEnvironments((current) => [...current, created].sort((left, right) => left.name.localeCompare(right.name)));
        setSelectedId(created.id);
      }
      setSnapshots(null);
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }

  async function loadSnapshots() {
    if (!selectedEnvironment) return;
    setBusy(true); setError(""); setSnapshots(null);
    try { setSnapshots(await api<EnvironmentSnapshot[]>(`/api/v1/environments/${selectedEnvironment.id}/snapshots`, {}, accessToken)); }
    catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }
  async function checkHealth() {
    if (!selectedEnvironment) return;
    setBusy(true); setError(""); setHealthResult(null);
    try {
      setHealthResult(await api<{ id: string; status: "HEALTHY" | "UNHEALTHY" | "BLOCKED" | "ERROR"; checked_at: string; http_status: number | null; error_code: string | null }>(`/api/v1/environments/${selectedEnvironment.id}/health-check`, jsonBody({ name: selectedEnvironment.name }), accessToken));
    } catch (caught) { setError(messageOf(caught)); }
    finally { setBusy(false); }
  }
  return <>
    <Card className="environment-callout"><span className="environment-icon"><Layers3 /></span><div><h2>{project ? `当前项目：${project.name}` : "先创建或选择一个项目"}</h2><p>环境配置属于项目范围；已创建的运行快照保持不可变，并固定当时的环境配置版本。</p></div></Card>
    {!project ? <EmptyState icon={<Layers3 />} title="还没有可用项目" text="先创建组织和项目，再管理测试环境。" /> : <>
      <Card className="form-card snapshot-lookup"><div className="form-card-title"><div><CardTitle>{selectedEnvironment ? "编辑测试环境" : "创建测试环境"}</CardTitle><p>仅保存目标 URL 和网络策略元数据，不会在保存时访问目标站点。</p></div><Button variant="secondary" type="button" disabled={loading} onClick={() => void loadEnvironments()}>刷新</Button></div>
        {environments.length > 0 && <div className="environment-picker"><Label htmlFor="environment-select">项目环境</Label><select id="environment-select" className="input" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}><option value="">新建环境</option>{environments.map((environment) => <option key={environment.id} value={environment.id}>{environment.name} · {environment.base_url}</option>)}</select></div>}
        <form className="inline-form environment-form" onSubmit={saveEnvironment}><div><Label htmlFor="environment-name">环境名称</Label><Input id="environment-name" value={name} onChange={(event) => setName(event.target.value)} required maxLength={120} /></div><div><Label htmlFor="environment-base-url">目标 URL</Label><Input id="environment-base-url" type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} required placeholder="https://staging.example.com" /></div><Button type="submit" disabled={busy || loading}>{busy ? "保存中…" : selectedEnvironment ? "保存修改" : <><Plus size={15} /> 创建环境</>}</Button></form>
        <div className="environment-picker"><Label htmlFor="environment-secret-ref">SecretRef（可选）</Label><select id="environment-secret-ref" className="input" value={secretRefId} onChange={(event) => setSecretRefId(event.target.value)}><option value="">不关联</option>{secretRefs.map((ref) => <option key={ref.id} value={ref.id}>{ref.provider} · {ref.external_key} · v{ref.active_version ?? "?"}</option>)}</select></div>
        <div className="environment-policy"><Label htmlFor="environment-network-policy">网络策略 JSON</Label><textarea id="environment-network-policy" className="input policy-editor" value={networkPolicy} onChange={(event) => setNetworkPolicy(event.target.value)} spellCheck={false} aria-describedby="environment-policy-help" /><small id="environment-policy-help">健康检查要求 `allowed_domains` 精确列出目标主机（不支持通配符）；实际出站访问由独立 Worker 和出口策略执行。</small></div>
        {loading && <div className="empty-inline" role="status">正在加载环境…</div>}
        {!loading && environments.length === 0 && <div className="empty-inline">当前项目还没有环境。创建后即可查看对应快照。</div>}
        {nextCursor && <div className="table-more"><Button variant="secondary" type="button" disabled={loading} onClick={() => void loadEnvironments(nextCursor, true)}>加载更多环境</Button></div>}
      </Card>
      <Card className="form-card snapshot-lookup"><div className="form-card-title"><div><CardTitle>不可变运行快照</CardTitle><p>快照展示固定配置和 SecretRef 版本 ID，不包含密钥值。</p></div><Button type="button" variant="secondary" disabled={!selectedEnvironment || busy} onClick={() => void loadSnapshots()}>{busy ? "读取中…" : "读取快照"}</Button></div>
        {selectedEnvironment && <div className="selected-environment"><b>{selectedEnvironment.name}</b><span>{selectedEnvironment.base_url}</span></div>}
        {error && <div className="alert alert-error" role="alert">{error}</div>}
        {snapshots && (snapshots.length ? <div className="snapshot-list">{snapshots.map((snapshot) => <div className="snapshot-row" key={snapshot.id}><div><b>{snapshot.base_url}</b><small>{formatDate(snapshot.created_at)} · {snapshot.secret_ref_version_id ? `SecretRef ${snapshot.secret_ref_version_id.slice(0, 8)}` : "未关联 SecretRef"}</small></div><code title={snapshot.snapshot_hash}>{snapshot.snapshot_hash.slice(0, 16)}…</code></div>)}</div> : <div className="empty-inline">该环境尚无快照。</div>)}
      </Card>
      <Card className="form-card snapshot-lookup"><div className="form-card-title"><div><CardTitle>环境健康检查</CardTitle><p>目标 URL 来自已保存环境；检查经独立 Browser Gateway/Worker 发出，不接受请求体覆盖目标。</p></div><Button type="button" variant="secondary" disabled={!selectedEnvironment || busy} onClick={() => void checkHealth()}>{busy ? "检查中…" : "检查环境"}</Button></div>{selectedEnvironment?.secret_ref_id && <small>该环境已关联 SecretRef；当前 Worker 尚无外部凭据解析器，检查会返回 `SECRET_PROVIDER_UNAVAILABLE`。</small>}{healthResult && <div className="selected-environment" role="status" aria-live="polite"><b>{healthResult.status}</b><span>{healthResult.http_status ? `HTTP ${healthResult.http_status}` : healthResult.error_code ?? "无 HTTP 响应"} · {formatDate(healthResult.checked_at)}</span></div>}</Card>
    </>}
  </>;
}

function EmptyState({ icon, title, text }: { icon: ReactNode; title: string; text: string }) {
  return <Card className="empty-state"><span className="empty-icon">{icon}</span><h3>{title}</h3><p>{text}</p></Card>;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export default function App() {
  const [auth, setAuth] = useState<AuthResult | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const clearAuth = useCallback(() => setAuth(null), []);
  useEffect(() => {
    let active = true;
    sessionRefreshPromise ??= api<AuthResult>("/api/v1/auth/refresh", { method: "POST" }).finally(() => { sessionRefreshPromise = undefined; });
    sessionRefreshPromise.then((result) => { if (active) setAuth(result); }).catch(() => undefined).finally(() => { if (active) setCheckingSession(false); });
    return () => { active = false; };
  }, []);
  if (checkingSession) return <div className="boot-screen"><span className="spinner" /><span>正在验证安全会话…</span></div>;
  return auth ? <AppShell auth={auth} onSignOut={clearAuth} /> : <LoginScreen onAuthenticated={setAuth} />;
}
