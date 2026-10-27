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

export async function api<T>(path: string, options: RequestInit = {}, accessToken?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, { ...options, headers, credentials: "include" });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string; title?: string };
      message = body.detail ?? body.title ?? message;
    } catch {
      // Keep a useful status message when the server response is not JSON.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const jsonBody = (value: unknown): Pick<RequestInit, "method" | "body"> => ({
  method: "POST",
  body: JSON.stringify(value),
});
