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


settings = Settings()

