from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    grok_api_key: str = ""
    xai_api_key: str = ""
    groq_api_key: str = ""
    grok_base_url: str = "https://api.x.ai/v1"
    grok_model: str = "grok-2-latest"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "qwen/qwen3.8-27b"
    database_url: str = "postgresql+psycopg://deploymind:deploymind@localhost:5432/deploymind"
    github_token: str = ""
    ghcr_username: str = ""
    ghcr_token: str = ""
    workspace_dir: str = "/tmp/deploymind"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    kind_cluster_name: str = "deploymind"
    k8s_namespace: str = "deploymind"
    kubeconfig: str = ""
    deployment_target: Literal["remote", "local"] = "remote"
    local_kind_enabled: bool = False
    remote_worker_url: str = ""
    remote_worker_token: str = ""
    release_approval_token: str = ""
    worker_id: str = "codedeck-worker"
    worker_poll_interval: int = Field(default=5, ge=1, le=60)
    worker_heartbeat_interval: int = Field(default=15, ge=5, le=300)
    k3s_ingress_domain: str = ""
    k3s_ctr_use_sudo: bool = False
    k3s_deployment_timeout: int = Field(default=600, ge=60, le=3600)
    deployment_max_concurrent_jobs: int = Field(default=1, ge=1, le=8)
    deployment_retention_hours: int = Field(default=168, ge=1, le=8760)
    docker_build_timeout: int = 1800
    database_connect_timeout: int = Field(default=5, ge=1, le=60)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_capture_content: bool = False
    semantic_embeddings_enabled: bool = True
    semantic_min_similarity: float = Field(default=0.35, ge=0, le=1)
    llm_timeout_seconds: int = Field(default=45, ge=1, le=600)
    repository_clone_timeout: int = Field(default=300, ge=10, le=1800)
    monitoring_enabled: bool = True
    monitoring_workspace_root: str = ""
    monitoring_exclude_patterns: str = ""
    monitoring_event_debounce_seconds: float = Field(default=0.75, ge=0.05, le=10)
    monitoring_kubernetes_interval_seconds: int = Field(default=30, ge=10, le=600)
    monitoring_restart_alert_threshold: int = Field(default=3, ge=1, le=100)
    monitoring_cpu_alert_millicores: int = Field(default=1500, ge=100, le=100000)
    monitoring_memory_alert_bytes: int = Field(default=1610612736, ge=104857600, le=1099511627776)


settings = Settings()

