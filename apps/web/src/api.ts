export type Organization = { id: string; name: string; slug: string; role: string };
export type Project = {
  id: string;
  organization_id: string;
  name: string;
  slug: string;
  created_by: string;
  role: string | null;
};
export type Session = {
  id: string;
  device_label: string;
  created_at: string;
  last_used_at: string;
  revoked_at: string | null;
};
export type ApiToken = {
  id: string;
  organization_id: string;
  name: string;
  scopes: string[];
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
  token?: string | null;
};
export type SecretRef = {
  id: string;
  organization_id: string;
  provider: string;
  external_key: string;
  rotated_at: string | null;
  created_at: string;
  active_version: number | null;
};
export type Environment = {
  id: string;
  project_id: string;
  name: string;
  base_url: string;
  secret_ref_id: string | null;
  network_policy: Record<string, unknown>;
  created_at: string;
};
export type EnvironmentSnapshot = {
  id: string;
  base_url: string;
  snapshot_hash: string;
  created_at: string;
  secret_ref_version_id: string | null;
};
export type TestCase = {
  id: string;
  project_id: string;
  title: string;
  priority: "P0" | "P1" | "P2" | "P3";
  status: "draft" | "active" | "archived";
  published_version_id: string | null;
  created_at: string;
};
export type CaseVersion = {
  id: string;
  case_id: string;
  version: number;
  version_name: string | null;
  review_status: "DRAFT" | "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "SUPERSEDED" | null;
  dsl: { schema_version: number; case_id: string; title: string | null; steps: Record<string, unknown>[]; assertions: Record<string, unknown>[] };
  changelog: string;
  created_at: string;
};
export type AuthResult = {
  access_token: string;
  expires_in: number;
  user: { id: string; email: string; display_name: string; status: string };
};

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

/** 这些端点本身不带访问令牌，401 代表凭据错误而非会话过期，不能触发续期重试。 */
const NO_REFRESH_PATHS = new Set([
  "/api/v1/auth/login",
  "/api/v1/auth/register",
  "/api/v1/auth/verify-email",
  "/api/v1/auth/verification/resend",
  "/api/v1/auth/password-reset/request",
  "/api/v1/auth/password-reset/confirm",
  "/api/v1/auth/refresh",
]);

let refreshInFlight: Promise<AuthResult> | null = null;
let sessionListener: ((auth: AuthResult) => void) | null = null;

/** 注册监听：访问令牌静默续期成功后，把新凭证同步给应用状态。 */
export function onSessionRefreshed(listener: (auth: AuthResult) => void): () => void {
  sessionListener = listener;
  return () => {
    if (sessionListener === listener) sessionListener = null;
  };
}

/** 用 HttpOnly Refresh Cookie 静默换新访问令牌；并发请求共享同一次续期。 */
export function refreshSession(): Promise<AuthResult> {
  refreshInFlight ??= request<AuthResult>("/api/v1/auth/refresh", { method: "POST" })
    .then((result) => {
      sessionListener?.(result);
      return result;
    })
    .finally(() => { refreshInFlight = null; });
  return refreshInFlight;
}

async function request<T>(path: string, options: RequestInit = {}, accessToken?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, { ...options, headers, credentials: "include" });
  if (!response.ok) {
    // 后端统一返回 problem-details（code/message/trace_id），优先展示服务端消息。
    let message = `请求失败 (${response.status})`;
    try {
      const body = (await response.json()) as { message?: string };
      if (body.message) message = body.message;
    } catch {
      // 响应不是 JSON 时保留状态码消息。
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function api<T>(path: string, options: RequestInit = {}, accessToken?: string): Promise<T> {
  try {
    return await request<T>(path, options, accessToken);
  } catch (error) {
    const retriable = error instanceof ApiError && error.status === 401
      && accessToken !== undefined && !NO_REFRESH_PATHS.has(path);
    if (!retriable) throw error;
    // 访问令牌 15 分钟过期：用刷新凭证静默续期后重试一次。
    try {
      const refreshed = await refreshSession();
      return await request<T>(path, options, refreshed.access_token);
    } catch {
      throw new ApiError("登录状态已过期，请重新登录。", 401);
    }
  }
}

export const jsonBody = (value: unknown): Pick<RequestInit, "method" | "body"> => ({
  method: "POST",
  body: JSON.stringify(value),
});
