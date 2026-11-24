import { AUTH_NOT_CONFIGURED, getAuthSession } from "./auth";
import type { AuthSession } from "./auth";

const configuredBase = process.env.EXPO_PUBLIC_API_BASE_URL?.trim();
let apiBaseUrl = configuredBase || "http://localhost:8000";

export function setApiBaseUrl(value: string) {
  const next = value.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(next)) throw new Error("Enter a complete backend URL, including http:// or https://.");
  apiBaseUrl = next;
}

export function getApiBaseUrl() {
  return apiBaseUrl;
}

async function request<T>(path: string, options: RequestInit = {}, requireUserAuth = false, authSession?: AuthSession): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  if (requireUserAuth) {
    const session = authSession || await getAuthSession();
    if (!session?.accessToken) throw new Error(AUTH_NOT_CONFIGURED);
    headers.set("Authorization", `Bearer ${session.accessToken}`);
  }
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, { ...options, headers });
  } catch {
    throw new Error(`Could not reach CodeDeck at ${apiBaseUrl}. Check the server URL and network connection.`);
  }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof body === "object" && body ? body.detail || body.message : body;
    throw new Error(typeof detail === "string" ? detail : `CodeDeck returned HTTP ${response.status}.`);
  }
  return body as T;
}

export const api = {
  projects: () => request<import("../types").Project[]>("/api/projects"),
  workflow: (projectId: number) => request<import("../types").Workflow>(`/api/projects/${projectId}/workflow`),
  analysis: (projectId: number) => request<Record<string, any> | null>(`/api/projects/${projectId}/analysis`),
  deployments: (projectId: number) => request<import("../types").Deployment[]>(`/api/projects/${projectId}/deployments`),
  pendingApprovals: () => request<{ items: import("../types").Approval[]; count: number }>("/api/approvals/pending", {}, true),
  approvalHistory: () => request<{ items: import("../types").Approval[]; count: number }>("/api/approvals/history", {}, true),
  approval: (approvalId: number) => request<import("../types").Approval>(`/api/approvals/${approvalId}`, {}, true),
  decide: async (approvalId: number, decision: "approve" | "reject", comments: string) => {
    const session = await getAuthSession();
    if (!session?.accessToken || !session.approverIdentity) throw new Error(AUTH_NOT_CONFIGURED);
    return request<import("../types").Approval>(`/api/approvals/${approvalId}/${decision}`, {
      method: "POST",
      headers: { "X-Approver-Identity": session.approverIdentity },
      body: JSON.stringify({ comments: comments || null }),
    }, true, session);
  },
  monitoring: (projectId?: number) => request<import("../types").Monitoring>(`/api/monitoring${projectId ? `?project_id=${projectId}` : ""}`),
};
