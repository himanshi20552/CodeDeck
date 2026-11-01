const API_BASE = import.meta.env.VITE_API_URL ?? '';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData;
  const headers = isFormData
    ? { ...(options?.headers ?? {}) }
    : { 'Content-Type': 'application/json', ...(options?.headers ?? {}) };

  const res = await fetch(`${API_BASE}${path}`, { headers, ...options });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.message || body.detail;
    const msg = typeof detail === 'string' ? detail : `Request failed (${res.status})`;
    throw new Error(msg);
  }

  if (res.status === 204 || res.headers.get('content-length') === '0') return {} as T;
  return res.json().catch(() => ({} as T));
}

export type Project = {
  id: number;
  repository_url: string;
  repository_name: string;
  status: string;
  created_at: string;
};

export type Analysis = {
  project_id: number;
  language: string | null;
  framework: string | null;
  package_manager: string | null;
  entrypoint: string | null;
  test_command: string | null;
  has_dockerfile: boolean;
  security_score: number | null;
  analysis_result: Record<string, unknown> | null;
  is_multiservice?: boolean;
  detected_services?: string[];
  multiservice_notice?: string | null;
  created_at: string;
};

export type PipelineStep = {
  name: string;
  status: string;
  result: string | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type GeneratedFile = { filename: string; content: string };

export type SearchSource = {
  path: string;
  snippet: string;
  score: number;
  line_start: number;
  line_end: number;
};

export type RepositoryAnswer = {
  question: string;
  answer: string;
  sources: SearchSource[];
  provider?: string;
  retrieval_method?: string;
  retrieval_count?: number;
  retrieval_latency_ms?: number;
};

export type EvaluationItem = {
  question: string;
  expected: string;
  answer: string;
  matched_source: string | null;
  status: string;
};

export type Evaluation = {
  questions: number;
  correct: number;
  retrieval_accuracy: number;
  results: EvaluationItem[];
};

export type GeneratedTestCase = { name: string; rationale: string; path: string; code: string };

export type TestInference = {
  name: string;
  target: string;
  inference: string;
};

export type GeneratedTestRun = {
  status: string;
  message: string;
  command: string | null;
  generated_tests: GeneratedTestCase[];
  stdout: string;
  stderr: string;
  failure_analysis: {
    failure?: string;
    likely_cause?: string;
    suggested_fix?: string;
    category?: string;
    evidence?: string[];
  } | null;
  test_inferences?: TestInference[];
};

export type Deployment = {
  id: number;
  environment: string;
  version: string;
  status: string;
  deployment_type: string;
  details: Record<string, unknown> | null;
  created_at: string;
};

export type SystemStatus = {
  deployment_target: string;
  deployment_targets: string[];
  local_kind?: { ok: boolean; clusters?: string[]; message: string };
  worker: { online: boolean; worker_id: string; last_seen: string | null; message: string; queue_length: number | null; details: Record<string, unknown> };
  backend: { ok: boolean; message: string };
  database: { ok: boolean; message: string };
  docker: { ok: boolean; message: string };
  kind: { ok: boolean; available: boolean; clusters: string[]; message: string };
  kubectl: { ok: boolean; message: string };
  ai_provider?: { provider: string; model: string; status: string; message?: string };
  integrations?: Record<string, { status: string; message: string }>;
};

export const getHealth = () => request<{ status: string; service: string }>('/health');
export const getSystemStatus = () => request<SystemStatus>('/api/system/status');
export const listProjects = () => request<Project[]>('/api/projects');
export const createProject = (repository_url: string) => request<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ repository_url }) });
export const getProject = (id: number) => request<Project>(`/api/projects/${id}`);
export const startAnalysis = (id: number) => request<Analysis>(`/api/projects/${id}/analyze`, { method: 'POST' });
export const getAnalysis = (id: number) => request<Analysis | null>(`/api/projects/${id}/analysis`);
export const getPipeline = (id: number) => request<PipelineStep[]>(`/api/projects/${id}/pipeline`);
export const getFiles = (id: number) => request<GeneratedFile[]>(`/api/projects/${id}/files`);
export const askRepositoryQuestion = (id: number, question: string) => request<RepositoryAnswer>(`/api/projects/${id}/qa`, { method: 'POST', body: JSON.stringify({ question }) });
export const getEvaluation = (id: number) => request<Evaluation>(`/api/projects/${id}/evaluation`);
export const runAiTests = (id: number) => request<GeneratedTestRun>(`/api/projects/${id}/ai-tests`, { method: 'POST' });
export const deployStaging = (id: number, target = 'remote') => request<{ status: string; message: string; deployment_id?: number }>(`/api/projects/${id}/deploy/staging`, { method: 'POST', body: JSON.stringify({ target }) });
export const cancelStaging = (id: number) => request<{ status: string; message: string }>(`/api/projects/${id}/deploy/staging/cancel`, { method: 'POST' });
export const deployProduction = (id: number) => request<{ status: string; message: string }>(`/api/projects/${id}/deploy/production`, { method: 'POST' });
export const approveProduction = (id: number) => request<{ status: string; message: string }>(`/api/projects/${id}/deploy/production/approve`, { method: 'POST' });
export const rollbackProduction = (id: number) => request<{ status: string; message: string }>(`/api/projects/${id}/rollback`, { method: 'POST' });
export const listDeployments = (id: number) => request<Deployment[]>(`/api/projects/${id}/deployments`);
export const cleanupDeployment = (projectId: number, deploymentId: number) => request<{ status: string; message: string }>(`/api/projects/${projectId}/deployments/${deploymentId}/cleanup`, { method: 'POST' });
