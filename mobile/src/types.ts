export type Project = {
  id: number;
  repository_url: string;
  repository_name: string;
  status: string;
  created_at: string;
};

export type WorkflowStage = {
  id: string;
  name: string;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_seconds?: number | null;
  waiting_seconds?: number | null;
  execution_seconds?: number | null;
  dependencies: string[];
  actor?: string | null;
  logs?: (string | { time?: string; message?: string })[];
  output?: string | null;
};

export type Workflow = {
  project_id: number;
  release: {
    repository_name: string;
    repository_url: string;
    branch?: string | null;
    commit_id?: string | null;
    commit_message?: string | null;
    environment?: string | null;
    version?: string | null;
    status: string;
    approval_status: string;
  };
  stages: WorkflowStage[];
  repository_snapshot: {
    files?: string[];
    file_count?: number | null;
    comparison_available?: boolean;
    added?: string[];
    modified?: string[];
    deleted?: string[];
    change_summary?: string;
    branch?: string | null;
    commit_id?: string | null;
    commit_message?: string | null;
  };
  deployment_history: Deployment[];
  analytics: { total_duration_seconds?: number | null; duration_basis?: string };
};

export type Deployment = {
  id: number;
  environment: string;
  version: string;
  status: string;
  deployment_type: string;
  created_at: string;
  details?: Record<string, any>;
};

export type Approval = {
  id: number;
  project_id: number;
  project?: { id: number; name: string; repository_url: string } | null;
  status: string;
  demo_mode?: boolean;
  release_version?: string | null;
  branch?: string | null;
  commit_id?: string | null;
  commit_message?: string | null;
  requested_by?: string | null;
  requester_identity_verified?: boolean;
  requested_at?: string | null;
  approver_identity?: string | null;
  approver_identity_verified?: boolean | null;
  decision?: string | null;
  decision_timestamp?: string | null;
  comments?: string | null;
  summary: Record<string, any>;
  staging_deployment?: Record<string, any> | null;
  production_deployment?: Record<string, any> | null;
  events?: { id: number; event_type: string; to_status: string; occurred_at: string; comments?: string | null }[];
};

export type Monitoring = {
  system_health: Record<string, { status: string; message?: string; last_seen?: string | null }>;
  monitoring_service: { status: string; last_error?: string | null };
  kubernetes: {
    status?: string;
    message?: string;
    sampled_at?: string | null;
    pods?: Record<string, any>[];
    deployments?: Record<string, any>[];
    hpas?: Record<string, any>[];
    metrics?: { status?: string; message?: string; pods?: Record<string, any>[] };
  };
  alerts: Record<string, any>[];
  deployment_events: Record<string, any>[];
  file_activity: Record<string, any>[];
};
