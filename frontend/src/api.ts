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

export type WorkflowStage = {
  id: string;
  name: string;
  status: 'Not Started' | 'Queued' | 'Running' | 'Completed' | 'Failed' | 'Blocked' | 'Skipped' | string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  waiting_seconds: number | null;
  execution_seconds: number | null;
  dependencies: string[];
  actor: string;
  logs: Array<string | { time?: string; message?: string }>;
  output: string | null;
};

export type ReleaseWorkflow = {
  project_id: number;
  release: {
    repository_name: string;
    repository_url: string;
    branch: string | null;
    commit_id: string | null;
    commit_message: string | null;
    environment: string | null;
    version: string | null;
    status: string;
    approval_status: string;
  };
  stages: WorkflowStage[];
  repository_snapshot: {
    branch: string | null;
    commit_id: string | null;
    commit_message: string | null;
    files: string[];
    file_count: number | null;
    comparison_available: boolean;
    added: string[];
    modified: string[];
    deleted: string[];
    change_summary: string;
    snapshot_available: boolean;
  };
  deployment_history: Deployment[];
  analytics: { total_duration_seconds: number | null; duration_basis: string };
};

export type MonitoringDashboard = {
  filters: { project_id: number | null; deployment_id: number | null; since: string | null; limit: number };
  system_health: Record<string, { status: string; message?: string; sampled_at?: string | null; last_seen?: string | null }>;
  monitoring_service: { enabled: boolean; watchdog_available: boolean; observer_running: boolean; event_processor_running: boolean; monitored_project_count: number; last_event_at: string | null; last_error: string | null; kubernetes_mode: string; kubernetes_snapshot_at: string | null; status: string };
  kubernetes: {
    status: string; message?: string; sampled_at?: string; namespaces?: string[];
    nodes?: Array<{ name: string; ready: boolean; reason?: string | null }>;
    deployments: Array<{ project_id: number | null; namespace: string; name: string; status: string; desired_replicas: number; ready_replicas: number; available_replicas: number; updated_replicas: number; reason?: string | null }>;
    pods: Array<{ project_id: number | null; namespace: string; name: string; phase: string; ready: boolean; reason?: string | null; restart_count: number; deployment_names?: string[] }>;
    hpas: Array<{ project_id: number | null; namespace: string; name: string; target_name?: string; current_replicas: number; desired_replicas: number; min_replicas: number; max_replicas: number | null; conditions: Array<{ type: string; status: string; reason?: string | null }> }>;
    metrics: { status: string; message: string; sampled_at?: string; pods: Array<{ project_id: number | null; namespace: string; name: string; deployment_names?: string[]; cpu_millicores: number; memory_bytes: number }>; nodes: Array<{ name: string; cpu_millicores: number | null; memory_bytes: number | null }> };
    alerts: Array<{ fingerprint: string; project_id: number | null; source: string; alert_type: string; severity: string; title: string; message: string }>;
  };
  file_activity: Array<{ id: number; project_id: number; deployment_id: number | null; analysis_id: number | null; path: string; event_type: string; workflow_context: string; processing_status: string; occurred_at: string; summary: string | null }>;
  deployment_events: Array<{ id: number; project_id: number; environment: string; version: string; status: string; deployment_type: string; created_at: string; logs: Array<{ time?: string; message: string }> }>;
  alerts: Array<{ id: number; project_id: number | null; deployment_id: number | null; source: string; type: string; severity: string; title: string; message: string; occurrence_count: number; is_resolved: boolean; first_seen: string; last_seen: string; resolved_at: string | null }>;
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

export type AutoscalingConfiguration = {
  min_replicas: number;
  max_replicas: number;
  target_cpu_utilization: number;
  target_memory_utilization: number | null;
  behavior: { scale_up_stabilization_seconds: number; scale_down_stabilization_seconds: number };
};

export type AutoscalingStatus = {
  deployment_id: number;
  namespace: string;
  deployment_name: string;
  configuration: { config?: AutoscalingConfiguration; original_replicas?: number; name?: string } | null;
  operation: { job_id?: number; status: string; completed_at?: string | null; message?: string } | null;
  monitoring_status: string;
  message?: string | null;
  rollout?: { status: string; desired_replicas: number; ready_replicas: number; available_replicas: number; updated_replicas: number } | null;
  sampled_at?: string | null;
  metrics: { status: string; message: string; pods?: unknown[]; nodes?: unknown[] };
  hpa: { name: string; current_replicas: number | null; desired_replicas: number | null; min_replicas: number; max_replicas: number; current_metrics?: Array<{ name: string; average_utilization: number | null; average_value: string | null }>; targets?: Array<{ name: string; average_utilization: number | null }>; conditions?: Array<{ type: string; status: string; reason?: string | null; message?: string | null }> } | null;
  events: Array<{ id: number; event_type: string; occurred_at: string; current_replicas: number | null; desired_replicas: number | null; details: Record<string, unknown> }>;
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
export const getReleaseWorkflow = (id: number) => request<ReleaseWorkflow>(`/api/projects/${id}/workflow`);
export const getMonitoringDashboard = (filters: { project_id?: number | null; deployment_id?: number | null; since?: string | null; limit?: number } = {}) => {
  const params = new URLSearchParams();
  if (filters.project_id != null) params.set('project_id', String(filters.project_id));
  if (filters.deployment_id != null) params.set('deployment_id', String(filters.deployment_id));
  if (filters.since) params.set('since', filters.since);
  if (filters.limit != null) params.set('limit', String(filters.limit));
  const query = params.toString();
  return request<MonitoringDashboard>(`/api/monitoring${query ? `?${query}` : ''}`);
};
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
export const getAutoscaling = (projectId: number, deploymentId: number) => request<AutoscalingStatus>(`/api/projects/${projectId}/deployments/${deploymentId}/autoscaling`);
export const configureAutoscaling = (projectId: number, deploymentId: number, config: AutoscalingConfiguration) => request<{ status: string; job_id?: number; message: string }>(`/api/projects/${projectId}/deployments/${deploymentId}/autoscaling`, { method: 'POST', body: JSON.stringify(config) });
export const disableAutoscaling = (projectId: number, deploymentId: number) => request<{ status: string; job_id?: number; message: string }>(`/api/projects/${projectId}/deployments/${deploymentId}/autoscaling`, { method: 'DELETE' });
export const cleanupDeployment = (projectId: number, deploymentId: number) => request<{ status: string; message: string }>(`/api/projects/${projectId}/deployments/${deploymentId}/cleanup`, { method: 'POST' });
export const clearProjectWorkspace = (id: number) => request<{ status: string; message: string; removed: string[] }>(`/api/projects/${id}/workspace/clear`, { method: 'POST' });
export const deleteProject = (id: number, confirmation: string) => request<{ status: string; message: string }>(`/api/projects/${id}?confirmation=${encodeURIComponent(confirmation)}`, { method: 'DELETE' });
