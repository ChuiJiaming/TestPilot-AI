import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Activity, Bell, Check, Download, FileClock, Plus, RefreshCw, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import { api, ApiError, jsonBody, type Organization } from "./api";
import { Button } from "./components/ui/button";
import { Modal } from "./components/ui/modal";
import { Card, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { identifyProvider, matchPreset, modelPresets, type ModelPreset } from "./modelPresets";

type QuotaPolicy = {
  organization_id: string;
  max_concurrent_runs: number;
  monthly_token_limit: number;
  monthly_tokens_committed: number;
  monthly_tokens_reserved: number;
  monthly_tokens_remaining: number;
  quota_exhausted: boolean;
  period_month: string;
  updated_at: string | null;
};
type UsageMetric = { organization_id: string; metric_name: string; value: number; unit: string };
type AuditEntry = {
  id: number;
  actor_user_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  metadata: Record<string, unknown>;
  previous_hash: string | null;
  entry_hash: string;
  created_at: string;
};
type Notification = { id: string; kind: string; payload: Record<string, unknown>; read_at: string | null; created_at: string };
type Preference = { channel: "in_app" | "email" | "webhook"; event_type: string; enabled: boolean };
type DeadDelivery = {
  delivery_id: string;
  notification_id: string;
  channel: "email" | "webhook";
  status: "DEAD";
  attempts: number;
  error_code: string;
  created_at: string;
};
type Setting = { key: string; value_hash: string; updated_at: string };
type SettingChange = {
  id: string;
  key: string;
  status: "PENDING" | "APPROVED" | "REJECTED" | "STALE";
  previous_value_hash: string;
  proposed_value_hash: string;
  requested_by: string;
  approved_by: string | null;
  reason: string;
  created_at: string;
  decided_at: string | null;
};
type AuditVerification = {
  valid: boolean;
  entries_checked: number;
  first_invalid_entry_id: number | null;
  expected_hash: string | null;
  actual_hash: string | null;
};
type AuditFilters = {
  action: string;
  resource_type: string;
  actor_user_id: string;
  resource_id: string;
  created_at_from: string;
  created_at_to: string;
};
type ModelPricing = { model: string; input_usd_per_million: number; output_usd_per_million: number };
type ModelConfigView = {
  source: "database" | "environment" | "default";
  base_url: string;
  api_key_env: string;
  api_key_configured: boolean;
  planner_model: string;
  run_agent_model: string;
  pricing: ModelPricing[];
  ready: boolean;
  issues: { code: string; message: string }[];
  updated_at: string | null;
};
type ModelProbe = { ok: boolean; model: string; latency_ms: number; error_code: string; message: string };
type ModelDraft = {
  base_url: string;
  api_key_env: string;
  planner_model: string;
  run_agent_model: string;
  pricing: { model: string; input: string; output: string }[];
  reason: string;
};
type Tab = "quota" | "notifications" | "audit" | "models" | "settings";

const emptyAuditFilters: AuditFilters = {
  action: "", resource_type: "", actor_user_id: "", resource_id: "", created_at_from: "", created_at_to: "",
};

const settingKeys = [
  "maintenance_mode",
  "max_concurrent_runs",
  "monthly_token_limit",
  "provider_timeout_seconds",
  "notification_retry_limit",
] as const;

const emptyModelDraft: ModelDraft = {
  base_url: "",
  api_key_env: "TESTPILOT_LLM_API_KEY",
  planner_model: "",
  run_agent_model: "",
  pricing: [{ model: "", input: "", output: "" }],
  reason: "",
};

const changeStatusLabels: Record<string, string> = {
  PENDING: "待审批",
  APPROVED: "已生效",
  REJECTED: "已拒绝",
  STALE: "已过期",
};

const sourceLabels: Record<ModelConfigView["source"], string> = {
  database: "数据库设置（已生效）",
  environment: "环境变量",
  default: "内置默认值",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败，请稍后重试。";
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function formatNumber(value: number) {
  return Number.isFinite(value) ? new Intl.NumberFormat("zh-CN").format(value) : "—";
}

function queryPath(path: string, query: URLSearchParams) {
  return `${path}?${query.toString()}`;
}

export function OperationsPage({ organizations, accessToken, currentUserId }: {
  organizations: Organization[];
  accessToken: string;
  currentUserId: string;
}) {
  const [selectedOrganizationId, setSelectedOrganizationId] = useState(organizations[0]?.id ?? "");
  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? organizations[0];
  const organizationId = organization?.id;
  const [tab, setTab] = useState<Tab>("quota");
  const [quota, setQuota] = useState<QuotaPolicy | null>(null);
  const [usage, setUsage] = useState<UsageMetric[]>([]);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaReload, setQuotaReload] = useState(0);
  const [quotaBusy, setQuotaBusy] = useState(false);
  const [showQuotaForm, setShowQuotaForm] = useState(false);
  const [quotaError, setQuotaError] = useState("");
  const [quotaLimitDraft, setQuotaLimitDraft] = useState("");
  const [concurrencyDraft, setConcurrencyDraft] = useState("");

  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [preferences, setPreferences] = useState<Preference[]>([]);
  const [deadDeliveries, setDeadDeliveries] = useState<DeadDelivery[]>([]);
  const [notificationLoading, setNotificationLoading] = useState(false);
  const [preferenceBusy, setPreferenceBusy] = useState(false);
  const [readingNotificationId, setReadingNotificationId] = useState("");
  const [notificationError, setNotificationError] = useState("");
  const [notificationNotice, setNotificationNotice] = useState("");

  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [auditFiltersDraft, setAuditFiltersDraft] = useState<AuditFilters>(emptyAuditFilters);
  const [auditFilters, setAuditFilters] = useState<AuditFilters>(emptyAuditFilters);
  const [auditVerification, setAuditVerification] = useState<AuditVerification | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditBusy, setAuditBusy] = useState(false);
  const [auditError, setAuditError] = useState("");

  const [platformAccess, setPlatformAccess] = useState<"checking" | "allowed" | "denied" | "error">("checking");
  const [settings, setSettings] = useState<Setting[]>([]);
  const [settingChanges, setSettingChanges] = useState<SettingChange[]>([]);
  const [settingKey, setSettingKey] = useState<(typeof settingKeys)[number]>("maintenance_mode");
  const [settingValue, setSettingValue] = useState("true");
  const [settingReason, setSettingReason] = useState("");
  const [decisionReasons, setDecisionReasons] = useState<Record<string, string>>({});
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsNotice, setSettingsNotice] = useState("");
  const [settingHistory, setSettingHistory] = useState<SettingChange[]>([]);

  const [modelConfig, setModelConfig] = useState<ModelConfigView | null>(null);
  const [modelConfigLoading, setModelConfigLoading] = useState(false);
  const [modelConfigError, setModelConfigError] = useState("");
  const [modelConfigNotice, setModelConfigNotice] = useState("");
  const [modelDraft, setModelDraft] = useState<ModelDraft>(emptyModelDraft);
  const [showModelForm, setShowModelForm] = useState(false);
  const [modelFormError, setModelFormError] = useState("");
  const [modelBusy, setModelBusy] = useState(false);
  const [modelProbe, setModelProbe] = useState<ModelProbe | null>(null);
  const [modelProbeBusy, setModelProbeBusy] = useState(false);

  const isOrganizationAdmin = organization?.role === "org_admin";
  // 平台管理员保存即生效，模型页展示「待审批（历史遗留）+ 最近生效」两类记录。
  const modelChanges = useMemo(
    () => [...settingChanges, ...settingHistory]
      .filter((change) => change.key === "llm_config")
      .slice(0, 5),
    [settingChanges, settingHistory],
  );
  // 摘要卡按接口地址反推供应商，编辑器按同一地址回显选中的预设。
  const providerIdentity = useMemo(
    () => identifyProvider(modelConfig?.base_url ?? ""),
    [modelConfig?.base_url],
  );
  const activePreset = useMemo(() => matchPreset(modelDraft.base_url), [modelDraft.base_url]);
  const candidateModels = useMemo(() => {
    const names = new Set(activePreset?.models ?? []);
    for (const row of modelDraft.pricing) if (row.model.trim()) names.add(row.model.trim());
    return [...names];
  }, [activePreset, modelDraft.pricing]);
  const activePreferences = useMemo(() => new Map(
    preferences.map((preference) => [`${preference.event_type}:${preference.channel}`, preference.enabled]),
  ), [preferences]);

  useEffect(() => {
    if (!organizations.some((item) => item.id === selectedOrganizationId)) {
      setSelectedOrganizationId(organizations[0]?.id ?? "");
    }
  }, [organizations, selectedOrganizationId]);

  useEffect(() => {
    let active = true;
    api<{ items: Setting[]; pending_changes: SettingChange[]; recent_changes?: SettingChange[] }>("/api/v1/system-settings", {}, accessToken)
      .then((page) => {
        if (!active) return;
        setSettings(page.items ?? []);
        setSettingChanges(page.pending_changes ?? []);
        setSettingHistory(page.recent_changes ?? []);
        setPlatformAccess("allowed");
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof ApiError && error.status === 403) setPlatformAccess("denied");
        else setPlatformAccess("error");
      });
    return () => { active = false; };
  }, [accessToken]);

  useEffect(() => {
    if (!organizationId) {
      setQuota(null);
      setUsage([]);
      return;
    }
    let active = true;
    const controller = new AbortController();
    setQuotaLoading(true);
    setQuotaError("");
    setQuota(null);
    setUsage([]);
    const organizationPath = `/api/v1/organizations/${organizationId}`;
    Promise.all([
      api<QuotaPolicy>(`${organizationPath}/quotas`, { signal: controller.signal }, accessToken),
      api<{ items: UsageMetric[] }>(`${organizationPath}/usage`, { signal: controller.signal }, accessToken),
    ]).then(([policy, page]) => {
      if (!active) return;
      setQuota(policy);
      setUsage(page.items);
      setQuotaLimitDraft(String(policy.monthly_token_limit));
      setConcurrencyDraft(String(policy.max_concurrent_runs));
    }).catch((error: unknown) => {
      if (active && !(error instanceof DOMException && error.name === "AbortError")) setQuotaError(errorMessage(error));
    }).finally(() => { if (active) setQuotaLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [accessToken, organizationId, quotaReload]);

  useEffect(() => {
    if (tab === "notifications") void loadNotifications();
    if (tab === "audit") void loadAudit();
    if (tab === "models" && platformAccess === "allowed") void loadModelConfig();
    if (tab === "settings" && platformAccess === "allowed") void loadSettings();
    // Each tab owns its request lifecycle; the selected organization is included where the API is scoped.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, organization?.id, platformAccess]);

  async function loadNotifications() {
    setNotificationLoading(true);
    setNotificationError("");
    const deliveryQuery = new URLSearchParams({ status: "DEAD", limit: "100" });
    if (organization) deliveryQuery.set("organization_id", organization.id);
    try {
      const [notificationPage, preferencePage, deadPage] = await Promise.all([
        api<{ items: Notification[]; next_cursor: string | null }>("/api/v1/notifications?limit=100", {}, accessToken),
        api<{ items: Preference[] }>("/api/v1/notifications/preferences", {}, accessToken),
        api<{ items: DeadDelivery[]; next_cursor: string | null }>(queryPath("/api/v1/notification-deliveries", deliveryQuery), {}, accessToken),
      ]);
      setNotifications(notificationPage.items);
      setPreferences(preferencePage.items);
      setDeadDeliveries(deadPage.items);
    } catch (error) {
      setNotificationError(errorMessage(error));
    } finally {
      setNotificationLoading(false);
    }
  }

  async function loadAudit(filters = auditFilters) {
    if (!organization) return;
    setAuditLoading(true);
    setAuditError("");
    const query = new URLSearchParams({ limit: "50" });
    for (const [key, value] of Object.entries(filters)) {
      if (!value) continue;
      const normalized = key === "created_at_from" || key === "created_at_to"
        ? new Date(value).toISOString()
        : value;
      query.set(key, normalized);
    }
    try {
      const page = await api<{ items: AuditEntry[]; next_cursor: string | null }>(
        queryPath(`/api/v1/organizations/${organization.id}/audit-logs`, query), {}, accessToken,
      );
      setAuditEntries(page.items);
      setAuditFilters(filters);
    } catch (error) {
      setAuditError(errorMessage(error));
    } finally {
      setAuditLoading(false);
    }
  }

  async function loadSettings() {
    setSettingsError("");
    try {
      const page = await api<{ items: Setting[]; pending_changes: SettingChange[]; recent_changes?: SettingChange[] }>("/api/v1/system-settings", {}, accessToken);
      setSettings(page.items ?? []);
      setSettingChanges(page.pending_changes ?? []);
      setSettingHistory(page.recent_changes ?? []);
    } catch (error) {
      setSettingsError(errorMessage(error));
    }
  }

  async function saveQuota(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!organization || !isOrganizationAdmin) return;
    const monthlyTokenLimit = Number(quotaLimitDraft);
    const maxConcurrentRuns = Number(concurrencyDraft);
    if (!Number.isSafeInteger(monthlyTokenLimit) || monthlyTokenLimit < 0
      || !Number.isSafeInteger(maxConcurrentRuns) || maxConcurrentRuns < 1 || maxConcurrentRuns > 1000) {
      setQuotaError("请输入有效额度：月 token 额度为非负整数，并发 Run 为 1 到 1000。");
      return;
    }
    setQuotaBusy(true);
    setQuotaError("");
    try {
      const updated = await api<QuotaPolicy>(`/api/v1/organizations/${organization.id}/quotas`, {
        method: "PUT", body: JSON.stringify({ max_concurrent_runs: maxConcurrentRuns, monthly_token_limit: monthlyTokenLimit }),
      }, accessToken);
      setQuota(updated);
      setQuotaLimitDraft(String(updated.monthly_token_limit));
      setConcurrencyDraft(String(updated.max_concurrent_runs));
      setShowQuotaForm(false);
    } catch (error) {
      setQuotaError(errorMessage(error));
    } finally {
      setQuotaBusy(false);
    }
  }

  async function markRead(notificationId: string) {
    setReadingNotificationId(notificationId);
    setNotificationError("");
    setNotificationNotice("");
    try {
      await api(`/api/v1/notifications/${notificationId}/read`, jsonBody({}), accessToken);
      setNotifications((current) => current.map((item) => item.id === notificationId
        ? { ...item, read_at: new Date().toISOString() } : item));
      setNotificationNotice("通知已标记为已读。");
    } catch (error) {
      setNotificationError(errorMessage(error));
    } finally {
      setReadingNotificationId("");
    }
  }

  async function savePreferences() {
    setPreferenceBusy(true);
    setNotificationError("");
    setNotificationNotice("");
    try {
      const page = await api<{ items: Preference[] }>("/api/v1/notifications/preferences", {
        method: "PUT", body: JSON.stringify({ items: preferences }),
      }, accessToken);
      setPreferences(page.items);
      setNotificationNotice("通知偏好已保存。");
    } catch (error) {
      setNotificationError(errorMessage(error));
    } finally {
      setPreferenceBusy(false);
    }
  }

  function togglePreference(key: string) {
    const [eventType, channel] = key.split(":") as [string, Preference["channel"]];
    setPreferences((current) => {
      const found = current.find((item) => item.event_type === eventType && item.channel === channel);
      return found
        ? current.map((item) => item === found ? { ...item, enabled: !item.enabled } : item)
        : [...current, { event_type: eventType, channel, enabled: true }];
    });
  }

  async function verifyAudit() {
    if (!organization) return;
    setAuditBusy(true);
    setAuditError("");
    setAuditVerification(null);
    const query = new URLSearchParams({ organization_id: organization.id });
    try {
      setAuditVerification(await api<AuditVerification>(queryPath("/api/v1/audit-logs/verify", query), {}, accessToken));
    } catch (error) {
      setAuditError(errorMessage(error));
    } finally {
      setAuditBusy(false);
    }
  }

  function applyAuditFilters() {
    if (auditFiltersDraft.created_at_from && auditFiltersDraft.created_at_to
      && new Date(auditFiltersDraft.created_at_from) > new Date(auditFiltersDraft.created_at_to)) {
      setAuditError("开始时间不能晚于结束时间。");
      return;
    }
    void loadAudit(auditFiltersDraft);
  }

  async function exportAudit() {
    if (!organization) return;
    setAuditBusy(true);
    setAuditError("");
    const filters = Object.fromEntries(Object.entries(auditFilters).filter(([, value]) => value));
    try {
      const result = await api<{ filename: string; media_type: string; content: string }>("/api/v1/audit-logs/export", {
        method: "POST", body: JSON.stringify({ organization_id: organization.id, filters, limit: 100 }),
      }, accessToken);
      const anchor = document.createElement("a");
      const objectUrl = URL.createObjectURL(new Blob([result.content], { type: result.media_type }));
      anchor.href = objectUrl;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(objectUrl);
    } catch (error) {
      setAuditError(errorMessage(error));
    } finally {
      setAuditBusy(false);
    }
  }

  async function requestSetting(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSettingsBusy(true);
    setSettingsError("");
    setSettingsNotice("");
    let value: boolean | number;
    if (settingKey === "maintenance_mode") {
      value = settingValue === "true";
    } else {
      value = Number(settingValue);
      if (!Number.isSafeInteger(value)) {
        setSettingsError("此设置必须是安全范围内的整数。");
        setSettingsBusy(false);
        return;
      }
    }
    try {
      await api<SettingChange>("/api/v1/system-settings", {
        method: "PUT", body: JSON.stringify({ key: settingKey, value, reason: settingReason }),
      }, accessToken);
      setSettingReason("");
      setSettingsNotice("设置已保存并立即生效。");
      await loadSettings();
    } catch (error) {
      setSettingsError(errorMessage(error));
    } finally {
      setSettingsBusy(false);
    }
  }

  async function decideSetting(change: SettingChange, decision: "approve" | "reject") {
    setSettingsBusy(true);
    setSettingsError("");
    setSettingsNotice("");
    try {
      await api<SettingChange>(`/api/v1/admin/system-setting-changes/${change.id}/decision`, jsonBody({
        decision, reason: decisionReasons[change.id] ?? "",
      }), accessToken);
      setSettingsNotice(decision === "approve" ? "设置变更已批准并生效。" : "设置变更已拒绝。");
      await loadSettings();
    } catch (error) {
      setSettingsError(errorMessage(error));
      if (error instanceof ApiError && error.status === 409) {
        setSettingChanges((current) => current.map((item) => item.id === change.id ? { ...item, status: "STALE" } : item));
      }
    } finally {
      setSettingsBusy(false);
    }
  }

  async function loadModelConfig() {
    setModelConfigLoading(true);
    setModelConfigError("");
    try {
      const [view] = await Promise.all([
        api<ModelConfigView>("/api/v1/admin/model-config", {}, accessToken),
        loadSettings(),
      ]);
      setModelConfig(view);
    } catch (error) {
      setModelConfigError(errorMessage(error));
    } finally {
      setModelConfigLoading(false);
    }
  }

  function openModelForm() {
    if (!modelConfig) return;
    setModelFormError("");
    setModelDraft({
      base_url: modelConfig.base_url,
      api_key_env: modelConfig.api_key_env,
      planner_model: modelConfig.planner_model,
      run_agent_model: modelConfig.run_agent_model,
      pricing: modelConfig.pricing.length > 0
        ? modelConfig.pricing.map((row) => ({
          model: row.model,
          input: String(row.input_usd_per_million),
          output: String(row.output_usd_per_million),
        }))
        : [{ model: modelConfig.run_agent_model, input: "", output: "" }],
      reason: "",
    });
    setShowModelForm(true);
  }

  function updatePricingRow(index: number, patch: Partial<ModelDraft["pricing"][number]>) {
    setModelDraft((current) => ({
      ...current,
      pricing: current.pricing.map((row, position) => position === index ? { ...row, ...patch } : row),
    }));
  }

  /** 套用供应商预设：填地址、凭据变量名和模型名，已填过的价格原样保留。 */
  function applyPreset(preset: ModelPreset) {
    setModelDraft((current) => {
      if (!preset.baseUrl) return current;
      const priced = new Map(current.pricing.map((row) => [row.model.trim(), row]));
      const pricing = preset.models.map((model) => {
        const existing = priced.get(model);
        return { model, input: existing?.input ?? "", output: existing?.output ?? "" };
      });
      return {
        ...current,
        base_url: preset.baseUrl,
        api_key_env: preset.apiKeyEnv,
        planner_model: preset.models[0] ?? "",
        run_agent_model: preset.models[1] ?? preset.models[0] ?? "",
        pricing: pricing.length > 0 ? pricing : current.pricing,
      };
    });
  }

  async function saveModelConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setModelFormError("");
    const pricing: Record<string, { input_usd_per_million: number; output_usd_per_million: number }> = {};
    for (const row of modelDraft.pricing) {
      const model = row.model.trim();
      if (!model) continue;
      // 空字符串会被 Number() 静默转成 0，等于把成本核算写成零，必须显式拦下。
      if (!row.input.trim() || !row.output.trim()) {
        setModelFormError(`模型 ${model} 的输入/输出单价不能为空，请按供应商官网当前定价填写。`);
        return;
      }
      const input = Number(row.input);
      const output = Number(row.output);
      if (!Number.isFinite(input) || input < 0 || !Number.isFinite(output) || output < 0) {
        setModelFormError(`模型 ${model} 的单价必须是非负数字。`);
        return;
      }
      if (pricing[model]) {
        setModelFormError(`模型 ${model} 重复出现在定价表中。`);
        return;
      }
      pricing[model] = { input_usd_per_million: input, output_usd_per_million: output };
    }
    if (Object.keys(pricing).length === 0) {
      setModelFormError("至少需要为一个模型配置定价。");
      return;
    }
    for (const model of [modelDraft.planner_model.trim(), modelDraft.run_agent_model.trim()]) {
      if (model && !pricing[model]) {
        setModelFormError(`模型 ${model} 不在定价表中，请先补充单价。`);
        return;
      }
    }
    setModelBusy(true);
    try {
      await api<SettingChange>("/api/v1/system-settings", {
        method: "PUT",
        body: JSON.stringify({
          key: "llm_config",
          value: {
            base_url: modelDraft.base_url.trim(),
            api_key_env: modelDraft.api_key_env.trim(),
            planner_model: modelDraft.planner_model.trim(),
            run_agent_model: modelDraft.run_agent_model.trim(),
            pricing,
          },
          reason: modelDraft.reason.trim(),
        }),
      }, accessToken);
      setShowModelForm(false);
      setModelConfigNotice("模型配置已保存并立即生效，worker 下次调用即采用新配置。");
      await Promise.all([loadModelConfig(), loadSettings()]);
    } catch (error) {
      setModelFormError(errorMessage(error));
    } finally {
      setModelBusy(false);
    }
  }

  async function probeModelConfig() {
    setModelProbeBusy(true);
    setModelProbe(null);
    setModelConfigError("");
    try {
      setModelProbe(await api<ModelProbe>("/api/v1/admin/model-config/test", jsonBody({}), accessToken));
    } catch (error) {
      setModelConfigError(errorMessage(error));
    } finally {
      setModelProbeBusy(false);
    }
  }

  function selectTab(next: Tab) {
    setTab(next);
  }

  const tabs: { id: Tab; label: string; icon: ReactNode }[] = [
    { id: "quota", label: "额度与用量", icon: <Activity size={16} /> },
    { id: "notifications", label: "通知与投递", icon: <Bell size={16} /> },
    { id: "audit", label: "审计日志", icon: <FileClock size={16} /> },
    ...(platformAccess === "allowed" ? [
      { id: "models" as const, label: "AI 模型", icon: <Sparkles size={16} /> },
      { id: "settings" as const, label: "平台设置", icon: <ShieldCheck size={16} /> },
    ] : []),
  ];

  if (organizations.length === 0) {
    return <Card className="operations-empty"><h2>没有可用组织</h2><p>先加入一个组织，再查看其额度、用量和审计记录。</p></Card>;
  }

  return <div className="operations-page">
    <Card className="operations-toolbar">
      <div className="operations-toolbar-copy"><span className="operations-kicker">OPERATIONS</span><h2>组织运营与平台治理</h2><p>查看用量和运行阻断状态；通知按当前账户隔离，审计和配额按所选组织隔离。</p></div>
      <div className="operations-org-picker"><Label htmlFor="operations-organization">组织范围</Label><select id="operations-organization" className="input" value={organization?.id ?? ""} onChange={(event) => setSelectedOrganizationId(event.target.value)}>
        {organizations.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}
      </select></div>
    </Card>

    <div className="operations-tabs" role="tablist" aria-label="运营功能">
      {tabs.map((item) => <button key={item.id} id={`operations-tab-${item.id}`} role="tab" type="button"
        aria-selected={tab === item.id} aria-controls={`operations-panel-${item.id}`} className={tab === item.id ? "active" : ""}
        onClick={() => selectTab(item.id)}>{item.icon}{item.label}</button>)}
      {platformAccess === "checking" && <span className="operations-tab-note">正在检查平台设置权限…</span>}
      {platformAccess === "denied" && <span className="operations-tab-note">平台设置仅平台管理员可见</span>}
      {platformAccess === "error" && <span className="operations-tab-note">平台设置权限暂不可用</span>}
    </div>

    {tab === "quota" && <section id="operations-panel-quota" role="tabpanel" aria-labelledby="operations-tab-quota" className="operations-panel">
      {quotaLoading && <div className="operations-loading" role="status">正在加载组织额度和用量…</div>}
      {quotaError && <div className="alert alert-error" role="alert">{quotaError}</div>}
      {!quotaLoading && !quotaError && !quota && <Card className="operations-empty"><p>当前组织暂无配额数据。</p></Card>}
      {quota && <>
        {quota.quota_exhausted && <div className="operations-quota-alert" role="status" aria-label="模型调用额度已耗尽">
          <span><Activity size={18} /></span><div><b>模型调用额度已耗尽</b><p>月额度已由已结算调用和活动预留占满，新的模型调用会被拒绝。</p></div>
        </div>}
        <div className="operations-metrics">
          <Card className="operations-metric"><small>本月额度</small><strong>{formatNumber(quota.monthly_token_limit)}</strong><span>tokens · {quota.period_month}</span></Card>
          <Card className="operations-metric"><small>已结算</small><strong>{formatNumber(quota.monthly_tokens_committed)}</strong><span>已结算 {formatNumber(quota.monthly_tokens_committed)} / {formatNumber(quota.monthly_token_limit)} tokens</span></Card>
          <Card className="operations-metric"><small>预留中</small><strong>{formatNumber(quota.monthly_tokens_reserved)}</strong><span>预留 {formatNumber(quota.monthly_tokens_reserved)} tokens</span></Card>
          <Card className="operations-metric"><small>剩余额度</small><strong>{formatNumber(quota.monthly_tokens_remaining)}</strong><span>tokens 可用于新调用</span></Card>
        </div>
        <div className="operations-two-column">
          <Card className="operations-card">
            <div className="operations-card-heading"><div><CardTitle>配额策略</CardTitle><p>预留量与已结算用量共同参与调用前额度检查。</p></div><span className={`operations-role ${isOrganizationAdmin ? "admin" : ""}`}>{isOrganizationAdmin ? "组织管理员" : "只读"}</span></div>
            {isOrganizationAdmin && <div className="operations-card-heading-actions"><Button type="button" onClick={() => setShowQuotaForm(true)}>调整配额策略</Button></div>}
            {showQuotaForm && isOrganizationAdmin && <Modal title="调整配额策略" description="预留量与已结算用量共同参与调用前额度检查。" onClose={() => setShowQuotaForm(false)}>
              <form className="form-stack" onSubmit={(event) => void saveQuota(event)}>
              <div><Label htmlFor="quota-monthly-token-limit">每月模型 Token 额度</Label><Input id="quota-monthly-token-limit" type="number" min={0} step={1} value={quotaLimitDraft} onChange={(event) => setQuotaLimitDraft(event.target.value)} required /></div>
              <div><Label htmlFor="quota-max-concurrent-runs">最大并发 Run</Label><Input id="quota-max-concurrent-runs" type="number" min={1} max={1000} step={1} value={concurrencyDraft} onChange={(event) => setConcurrencyDraft(event.target.value)} required /></div>
              <Button type="submit" disabled={quotaBusy}>{quotaBusy ? "正在保存…" : "保存配额策略"}</Button>
              </form>
            </Modal>}
          </Card>
          <Card className="operations-card">
            <div className="operations-card-heading"><div><CardTitle>用量指标</CardTitle><p>按组织记录的模型调用和执行资源指标。</p></div><Button variant="ghost" className="operations-refresh" type="button" onClick={() => setQuotaReload((current) => current + 1)} aria-label="刷新用量"><RefreshCw size={15} /></Button></div>
            {usage.length === 0 ? <p className="operations-empty-inline">当前时间范围没有用量记录。</p> : <div className="operations-usage-list">{usage.map((item, index) => <div className="operations-usage-row" key={`${item.metric_name}-${index}`}><b>{item.metric_name}</b><span>{formatNumber(item.value)} {item.unit}</span></div>)}</div>}
          </Card>
        </div>
      </>}
    </section>}

    {tab === "notifications" && <section id="operations-panel-notifications" role="tabpanel" aria-labelledby="operations-tab-notifications" className="operations-panel">
      <div className="operations-section-heading"><div><h2>通知与投递</h2><p>通知仅显示给当前账户；死信按所选组织和账户权限过滤。</p></div><Button variant="secondary" type="button" onClick={() => void loadNotifications()} disabled={notificationLoading}><RefreshCw size={14} />刷新</Button></div>
      {notificationError && <div className="alert alert-error" role="alert">{notificationError}</div>}
      {notificationNotice && <div className="alert alert-success" role="status">{notificationNotice}</div>}
      {notificationLoading ? <div className="operations-loading" role="status">正在加载通知和投递…</div> : <>
        <div className="operations-two-column">
          <Card className="operations-card"><div className="operations-card-heading"><div><CardTitle>通知偏好</CardTitle><p>选择站内、邮件和 Webhook 通知渠道。</p></div></div>
            {preferences.length === 0 ? <p className="operations-empty-inline">当前没有已配置的通知偏好。</p> : <div className="operations-preferences">{preferences.map((preference) => {
              const key = `${preference.event_type}:${preference.channel}`;
              return <label className="operations-preference-row" key={key}><span><b>{preference.event_type}</b><small>{preference.channel}</small></span><input type="checkbox" aria-label={`${preference.event_type} · ${preference.channel}`} checked={activePreferences.get(key) ?? preference.enabled} onChange={() => togglePreference(key)} /></label>;
            })}</div>}
            <Button type="button" onClick={() => void savePreferences()} disabled={preferenceBusy}>{preferenceBusy ? "正在保存…" : "保存通知偏好"}</Button>
          </Card>
          <Card className="operations-card"><div className="operations-card-heading"><div><CardTitle>死信投递</CardTitle><p>显示达到重试上限的失败投递及安全错误码。</p></div><span className="operations-count">{deadDeliveries.length}</span></div>
            {deadDeliveries.length === 0 ? <p className="operations-empty-inline">当前没有死信投递。</p> : <div className="operations-dead-list">{deadDeliveries.map((delivery) => <article className="operations-dead-row" key={delivery.delivery_id}>
              <div><b>{delivery.channel} · {delivery.status}</b><small>{delivery.attempts} 次尝试 · {formatDate(delivery.created_at)}</small></div><code>{delivery.error_code}</code>
            </article>)}</div>}
          </Card>
        </div>
        <Card className="operations-card operations-notification-card"><div className="operations-card-heading"><div><CardTitle>账户通知</CardTitle><p>通知载荷保留来源标识，避免把其他账户的通知混入列表。</p></div><span className="operations-count">{notifications.length}</span></div>
          {notifications.length === 0 ? <p className="operations-empty-inline">当前没有通知。</p> : <div className="operations-notification-list">{notifications.map((notification) => <article className="operations-notification-row" key={notification.id}>
            <div className="operations-notification-icon"><Bell size={15} /></div><div className="operations-notification-copy"><b>{notification.kind}</b><small>{formatDate(notification.created_at)} · {JSON.stringify(notification.payload)}</small></div>
            {notification.read_at ? <span className="operations-read-state"><Check size={14} />已读</span> : <Button variant="secondary" type="button" disabled={readingNotificationId === notification.id} onClick={() => void markRead(notification.id)}>{readingNotificationId === notification.id ? "正在更新…" : "标记为已读"}</Button>}
          </article>)}</div>}
        </Card>
      </>}
    </section>}

    {tab === "audit" && <section id="operations-panel-audit" role="tabpanel" aria-labelledby="operations-tab-audit" className="operations-panel">
      <div className="operations-section-heading"><div><h2>组织审计日志</h2><p>筛选操作并验证当前组织哈希链；导出会应用相同筛选条件。</p></div><Button variant="secondary" type="button" onClick={() => void loadAudit()} disabled={auditLoading}><RefreshCw size={14} />刷新</Button></div>
      <Card className="operations-card operations-audit-controls"><div className="operations-audit-filter">
        <div><Label htmlFor="audit-action">操作类型</Label><Input id="audit-action" value={auditFiltersDraft.action} onChange={(event) => setAuditFiltersDraft((current) => ({ ...current, action: event.target.value }))} placeholder="例如 case.updated" maxLength={160} /></div>
        <div><Label htmlFor="audit-resource-type">资源类型</Label><Input id="audit-resource-type" value={auditFiltersDraft.resource_type} onChange={(event) => setAuditFiltersDraft((current) => ({ ...current, resource_type: event.target.value }))} maxLength={160} /></div>
        <div><Label htmlFor="audit-actor">操作人 ID</Label><Input id="audit-actor" value={auditFiltersDraft.actor_user_id} onChange={(event) => setAuditFiltersDraft((current) => ({ ...current, actor_user_id: event.target.value }))} /></div>
        <div><Label htmlFor="audit-resource-id">资源 ID</Label><Input id="audit-resource-id" value={auditFiltersDraft.resource_id} onChange={(event) => setAuditFiltersDraft((current) => ({ ...current, resource_id: event.target.value }))} /></div>
        <div><Label htmlFor="audit-from">开始时间</Label><Input id="audit-from" type="datetime-local" value={auditFiltersDraft.created_at_from} onChange={(event) => setAuditFiltersDraft((current) => ({ ...current, created_at_from: event.target.value }))} /></div>
        <div><Label htmlFor="audit-to">结束时间</Label><Input id="audit-to" type="datetime-local" value={auditFiltersDraft.created_at_to} onChange={(event) => setAuditFiltersDraft((current) => ({ ...current, created_at_to: event.target.value }))} /></div>
        <div className="operations-audit-actions"><Button type="button" disabled={auditLoading} onClick={applyAuditFilters}>筛选</Button><Button variant="secondary" type="button" disabled={auditBusy || auditLoading} onClick={() => void verifyAudit()}>验证审计链</Button><Button variant="secondary" type="button" disabled={auditBusy || auditLoading} onClick={() => void exportAudit()}><Download size={14} />导出审计日志</Button></div>
      </div>
        {auditError && <div className="alert alert-error" role="alert">{auditError}</div>}
        {auditVerification && <div className={`operations-verification ${auditVerification.valid ? "valid" : "invalid"}`} role="status">{auditVerification.valid ? `链验证通过 · ${auditVerification.entries_checked} 条记录` : `链验证失败 · 第 ${auditVerification.first_invalid_entry_id ?? "未知"} 条记录`}</div>}
      </Card>
      <Card className="operations-card operations-audit-list"><div className="operations-card-heading"><div><CardTitle>审计事件</CardTitle><p>{auditFilters.action ? `当前筛选：${auditFilters.action}` : "显示最近的组织审计事件。"}</p></div><span className="operations-count">{auditEntries.length}</span></div>
        {auditLoading ? <div className="operations-loading" role="status">正在加载审计日志…</div> : auditEntries.length === 0 ? <p className="operations-empty-inline">没有匹配的审计记录。</p> : <div className="operations-audit-table" role="table" aria-label="审计事件">
          <div className="operations-audit-head" role="row"><span role="columnheader">操作</span><span role="columnheader">资源</span><span role="columnheader">时间</span><span role="columnheader">哈希</span></div>
          {auditEntries.map((entry) => <div className="operations-audit-row" role="row" key={entry.id}><span role="cell"><b>{entry.action}</b><small>记录 #{entry.id}</small></span><span role="cell">{entry.resource_type}<small>{entry.resource_id ?? "—"}</small></span><span role="cell">{formatDate(entry.created_at)}</span><code role="cell" title={entry.entry_hash}>{entry.entry_hash}</code></div>)}
        </div>}
      </Card>
    </section>}

    {tab === "models" && platformAccess === "allowed" && <section id="operations-panel-models" role="tabpanel" aria-labelledby="operations-tab-models" className="operations-panel">
      <div className="operations-section-heading"><div><h2>AI 模型</h2><p>平台调用大模型的接口地址、凭据来源与计费单价。API Key 只以环境变量名引用，不会写入数据库或审计日志。</p></div>
        <div className="button-row"><Button variant="secondary" type="button" onClick={() => void loadModelConfig()} disabled={modelConfigLoading}><RefreshCw size={14} />刷新</Button></div>
      </div>
      {modelConfigError && <div className="alert alert-error" role="alert">{modelConfigError}</div>}
      {modelConfigNotice && <div className="alert alert-success" role="status">{modelConfigNotice}</div>}
      {modelConfigLoading && !modelConfig && <div className="operations-loading" role="status">正在读取模型配置…</div>}
      {modelConfig && <>
        <Card className={`operations-card model-hero ${modelConfig.ready ? "ready" : "blocked"}`}>
          <div className="model-hero-main">
            <span className="model-logo" style={{ background: providerIdentity.accent }} aria-hidden="true">{providerIdentity.mark}</span>
            <div className="model-hero-copy">
              <div className="model-hero-title"><b>{providerIdentity.name}</b><span className={`model-state-pill ${modelConfig.ready ? "ok" : "bad"}`}>{modelConfig.ready ? "调用已就绪" : "调用未就绪"}</span></div>
              <code className="model-hero-url">{modelConfig.base_url}</code>
            </div>
            <div className="model-hero-actions">
              <Button variant="secondary" type="button" onClick={() => void probeModelConfig()} disabled={modelProbeBusy}>{modelProbeBusy ? "正在测试…" : "测试连接"}</Button>
              <Button type="button" onClick={openModelForm}>编辑配置</Button>
            </div>
          </div>
          <div className="model-facts">
            <div className="model-fact"><span>凭据变量</span><b><code>{modelConfig.api_key_env}</code><span className={`model-badge ${modelConfig.api_key_configured ? "ok" : "bad"}`}>{modelConfig.api_key_configured ? "已注入" : "未注入"}</span></b></div>
            <div className="model-fact"><span>场景生成模型</span><b>{modelConfig.planner_model || "未配置"}</b></div>
            <div className="model-fact"><span>Run Agent 模型</span><b>{modelConfig.run_agent_model || "未配置"}</b></div>
            <div className="model-fact"><span>生效来源</span><b>{sourceLabels[modelConfig.source]}{modelConfig.updated_at ? <small>{formatDate(modelConfig.updated_at)}</small> : null}</b></div>
          </div>
          {modelConfig.issues.length > 0 && <ul className="model-issues">{modelConfig.issues.map((issue) => <li key={issue.code}><code>{issue.code}</code>{issue.message}</li>)}</ul>}
          {modelProbe && <div className={`model-probe ${modelProbe.ok ? "ok" : "bad"}`} role="status">{modelProbe.ok ? `连接成功 · ${modelProbe.model} · ${modelProbe.latency_ms}ms` : `连接失败 · ${modelProbe.error_code} ${modelProbe.message}`}</div>}
        </Card>
        <Card className="operations-card">
          <div className="operations-card-heading"><div><CardTitle>模型定价</CardTitle><p>按每百万 token 计价，用于成本核算与额度结算。</p></div><span className="operations-count">{modelConfig.pricing.length}</span></div>
          {modelConfig.pricing.length === 0 ? <p className="operations-empty-inline">尚未配置模型定价。</p> : <div className="operations-audit-table model-pricing-table" role="table" aria-label="模型定价">
            <div className="operations-audit-head" role="row"><span role="columnheader">模型</span><span role="columnheader">输入 / 百万 token</span><span role="columnheader">输出 / 百万 token</span></div>
            {modelConfig.pricing.map((row) => <div className="operations-audit-row" role="row" key={row.model}><span role="cell"><b>{row.model}</b></span><span role="cell">${row.input_usd_per_million}</span><span role="cell">${row.output_usd_per_million}</span></div>)}
          </div>}
        </Card>
                {modelChanges.length > 0 && <Card className="operations-card">
          <div className="operations-card-heading"><div><CardTitle>模型配置变更记录</CardTitle><p>平台管理员保存后立即生效，以下为最近 5 条变更留痕。</p></div><span className="operations-count">{modelChanges.length}</span></div>
          <div className="operations-change-list">{modelChanges.map((change) => <article className="operations-change-card" key={change.id}>
            <div className="operations-change-title"><div><b>llm_config</b><span className={`operations-change-status ${change.status.toLowerCase()}`}>{changeStatusLabels[change.status] ?? change.status}</span></div><small>{formatDate(change.created_at)}</small></div>
            <p>{change.reason}</p>
            <div className="operations-actors"><span>申请人 <b>{change.requested_by}</b></span><span>审批人 <b>{change.approved_by ?? "待审批"}</b></span></div>
            {change.status === "PENDING" && (change.requested_by === currentUserId
              ? <p className="operations-separation-note">发起人不能审批自己的变更</p>
              : <Button variant="secondary" type="button" onClick={() => selectTab("settings")}>前往平台设置审批</Button>)}
          </article>)}</div>
        </Card>}
      </>}
      {showModelForm && <Modal title="编辑 AI 模型配置" description="选择供应商预设会自动填好接口地址与模型名；保存后立即生效。" wide onClose={() => setShowModelForm(false)}>
        <form className="form-stack" onSubmit={(event) => void saveModelConfig(event)}>
          {modelFormError && <div className="alert alert-error" role="alert">{modelFormError}</div>}
          <div className="model-picker">
            <div className="model-picker-head"><b>供应商预设</b><small>点选即填入接口地址、凭据变量名与模型名，单价请按官网当前定价填写</small></div>
            <div className="model-preset-grid">
              {modelPresets.map((preset) => <button key={preset.id} type="button" aria-pressed={activePreset?.id === preset.id} className={`model-preset${activePreset?.id === preset.id ? " active" : ""}`} onClick={() => applyPreset(preset)}>
                <span className="model-preset-logo" style={{ background: preset.accent }} aria-hidden="true">{preset.mark}</span>
                <span className="model-preset-copy"><b>{preset.name}</b><small>{preset.hint}</small></span>
              </button>)}
            </div>
          </div>
          <fieldset className="model-form-section">
            <legend>连接</legend>
            <div className="model-form-pair">
              <div><Label htmlFor="model-base-url">接口地址</Label><Input id="model-base-url" value={modelDraft.base_url} onChange={(event) => setModelDraft((current) => ({ ...current, base_url: event.target.value }))} placeholder="https://api.openai.com/v1" required maxLength={2048} /></div>
              <div><Label htmlFor="model-api-key-env">API Key 环境变量名</Label><Input id="model-api-key-env" value={modelDraft.api_key_env} onChange={(event) => setModelDraft((current) => ({ ...current, api_key_env: event.target.value }))} placeholder="TESTPILOT_LLM_API_KEY" required maxLength={64} /><small className="model-field-note">只填变量名，Key 本身不会进数据库或审计日志。</small></div>
            </div>
          </fieldset>
          <fieldset className="model-form-section">
            <legend>模型</legend>
            <div className="model-form-pair">
              <div><Label htmlFor="model-planner">场景生成模型</Label><Input id="model-planner" list="model-candidates" value={modelDraft.planner_model} onChange={(event) => setModelDraft((current) => ({ ...current, planner_model: event.target.value }))} placeholder="glm-4-flash" required maxLength={120} /></div>
              <div><Label htmlFor="model-run-agent">Run Agent 模型</Label><Input id="model-run-agent" list="model-candidates" value={modelDraft.run_agent_model} onChange={(event) => setModelDraft((current) => ({ ...current, run_agent_model: event.target.value }))} placeholder="glm-4.6" required maxLength={120} /></div>
            </div>
            <datalist id="model-candidates">{candidateModels.map((model) => <option key={model} value={model} />)}</datalist>
            <small className="model-field-note">场景生成负责把需求拆成测试步骤；Run Agent 决定浏览器里每一步怎么操作，两者可以相同。</small>
          </fieldset>
          <fieldset className="model-pricing-editor model-form-section">
            <legend>模型定价（美元 / 百万 token）</legend>
            {modelDraft.pricing.map((row, index) => <div className="model-pricing-row" key={`pricing-${index}`}>
              <Input aria-label={`定价模型 ${index + 1}`} value={row.model} onChange={(event) => updatePricingRow(index, { model: event.target.value })} placeholder="模型名称" maxLength={120} />
              <Input aria-label={`输入单价 ${index + 1}`} type="number" min={0} step="0.001" value={row.input} onChange={(event) => updatePricingRow(index, { input: event.target.value })} placeholder="输入" />
              <Input aria-label={`输出单价 ${index + 1}`} type="number" min={0} step="0.001" value={row.output} onChange={(event) => updatePricingRow(index, { output: event.target.value })} placeholder="输出" />
              <button type="button" className="model-pricing-remove" aria-label={`删除定价行 ${index + 1}`} onClick={() => setModelDraft((current) => ({ ...current, pricing: current.pricing.filter((_, position) => position !== index) }))} disabled={modelDraft.pricing.length === 1}><Trash2 size={14} /></button>
            </div>)}
            <Button variant="secondary" type="button" onClick={() => setModelDraft((current) => ({ ...current, pricing: [...current.pricing, { model: "", input: "", output: "" }] }))}><Plus size={14} />新增模型定价</Button>
          </fieldset>
          <div><Label htmlFor="model-change-reason">变更理由</Label><Input id="model-change-reason" value={modelDraft.reason} onChange={(event) => setModelDraft((current) => ({ ...current, reason: event.target.value }))} maxLength={500} required /></div>
          <Button type="submit" disabled={modelBusy}>{modelBusy ? "正在保存…" : "保存并生效"}</Button>
        </form>
      </Modal>}

    </section>}

    {tab === "settings" && platformAccess === "allowed" && <section id="operations-panel-settings" role="tabpanel" aria-labelledby="operations-tab-settings" className="operations-panel">
      <div className="operations-section-heading"><div><h2>平台设置</h2><p>平台管理员保存后立即生效；页面只展示旧值和新值的哈希摘要，变更留痕在审计日志中。</p></div><Button variant="secondary" type="button" onClick={() => void loadSettings()} disabled={settingsBusy}><RefreshCw size={14} />刷新</Button></div>
      {settingsError && <div className="alert alert-error" role="alert">{settingsError}</div>}
      {settingsNotice && <div className="alert alert-success" role="status">{settingsNotice}</div>}
      <Card className="operations-card"><div className="operations-card-heading"><div><CardTitle>申请系统设置变更</CardTitle><p>仅允许非敏感的布尔值和整数设置；设置值不会从 API 回传。</p></div></div>
        <form className="operations-setting-form" onSubmit={(event) => void requestSetting(event)}>
          <div><Label htmlFor="setting-key">设置项</Label><select id="setting-key" className="input" value={settingKey} onChange={(event) => { const key = event.target.value as (typeof settingKeys)[number]; setSettingKey(key); setSettingValue(key === "maintenance_mode" ? "true" : "1"); }}>
            {settingKeys.map((key) => <option key={key} value={key}>{key}</option>)}
          </select></div>
          <div><Label htmlFor="setting-value">申请值</Label>{settingKey === "maintenance_mode" ? <select id="setting-value" className="input" value={settingValue} onChange={(event) => setSettingValue(event.target.value)}><option value="true">开启</option><option value="false">关闭</option></select> : <Input id="setting-value" type="number" step={1} value={settingValue} onChange={(event) => setSettingValue(event.target.value)} required />}</div>
          <div className="operations-form-wide"><Label htmlFor="setting-request-reason">申请理由</Label><Input id="setting-request-reason" value={settingReason} onChange={(event) => setSettingReason(event.target.value)} maxLength={500} required /></div>
          <Button type="submit" disabled={settingsBusy}>{settingsBusy ? "正在保存…" : "保存并生效"}</Button>
        </form>
      </Card>
      <Card className="operations-card"><div className="operations-card-heading"><div><CardTitle>当前设置摘要</CardTitle><p>用于检查设置版本，不包含原始设置值。</p></div><span className="operations-count">{settings.length}</span></div>
        {settings.length === 0 ? <p className="operations-empty-inline">尚无已保存的平台设置。</p> : <div className="operations-setting-list">{settings.map((setting) => <div className="operations-setting-row" key={setting.key}><b>{setting.key}</b><code>{setting.value_hash}</code><small>{formatDate(setting.updated_at)}</small></div>)}</div>}
      </Card>
      <Card className="operations-card"><div className="operations-card-heading"><div><CardTitle>待审批变更</CardTitle><p>平台管理员保存即生效，这里只保留改造前遗留的待审批项。</p></div><span className="operations-count">{settingChanges.filter((item) => item.status === "PENDING").length}</span></div>
        {settingChanges.length === 0 ? <p className="operations-empty-inline">当前没有待审批设置变更（平台管理员保存即生效）。</p> : <div className="operations-change-list">{settingChanges.map((change) => {
          const isRequester = change.requested_by === currentUserId;
          const pending = change.status === "PENDING";
          return <article className="operations-change-card" key={change.id}>
            <div className="operations-change-title"><div><b>{change.key}</b><span className={`operations-change-status ${change.status.toLowerCase()}`}>{changeStatusLabels[change.status] ?? change.status}</span></div><small>{formatDate(change.created_at)}</small></div>
            <p>{change.reason}</p>
            <dl className="operations-hash-pair"><div><dt>原值哈希</dt><dd>{change.previous_value_hash}</dd></div><div><dt>新值哈希</dt><dd>{change.proposed_value_hash}</dd></div></dl>
            <div className="operations-actors"><span>申请人 <b>{change.requested_by}</b></span><span>审批人 <b>{change.approved_by ?? "待审批"}</b></span></div>
            {pending && isRequester && <p className="operations-separation-note">发起人不能审批自己的变更</p>}
            {pending && <div className="operations-decision-form"><div><Label htmlFor={`setting-reason-${change.id}`}>审批理由</Label><Input id={`setting-reason-${change.id}`} value={decisionReasons[change.id] ?? ""} onChange={(event) => setDecisionReasons((current) => ({ ...current, [change.id]: event.target.value }))} maxLength={500} required disabled={isRequester || settingsBusy} /></div><div className="button-row"><Button type="button" disabled={isRequester || settingsBusy || !(decisionReasons[change.id] ?? "").trim()} onClick={() => void decideSetting(change, "approve")}>批准变更</Button><Button variant="secondary" type="button" disabled={isRequester || settingsBusy || !(decisionReasons[change.id] ?? "").trim()} onClick={() => void decideSetting(change, "reject")}>拒绝变更</Button></div></div>}
          </article>;
        })}</div>}
      </Card>
    </section>}
  </div>;
}
