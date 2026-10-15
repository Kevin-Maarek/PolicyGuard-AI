from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    database_url: str = "postgresql+psycopg://policyguard:local_policy_demo@policy-db:5432/policyguard"
    sas_base_url: str = "http://sas-mock:8000"
    mock_admin_token: str = ""
    sas_target: Literal["simulator", "viya"] = "simulator"
    sas_access_token: str = ""
    sas_write_enabled: bool = True
    llm_provider: str = "ollama"
    llm_model: str = "qwen3.8:latest"
    llm_base_url: str = "http://host.docker.internal:11434"
    llm_api_key: str = ""
    ollama_chat_format: str = "auto"
    embedding_provider: str = "ollama"
    embedding_model: str = "qwen3-embedding:latest"
    embedding_base_url: str = "http://host.docker.internal:11434"
    embedding_api_key: str = ""
    embedding_dimensions: int = 1024
    ai_timeout_seconds: int = 600
    sync_interval_seconds: int = 300
    max_upload_mb: int = 15


settings = Settings()
