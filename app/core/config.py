"""Application settings, loaded from environment variables (and .env if present)."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "DevInvestigator"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    log_format: str = "text"  # "text" or "json"

    # GitHub. Auth is either a token or a GitHub App (app id + private key); the App wins if both are set.
    github_api_url: str = "https://api.github.com"
    github_token: SecretStr | None = None
    github_app_id: str | None = None
    github_app_private_key: SecretStr | None = None  # PEM contents; literal "\n" sequences are accepted
    github_app_private_key_path: str | None = None
    github_webhook_secret: SecretStr | None = None
    github_request_timeout_seconds: float = 30.0
    github_log_max_bytes: int = 200_000  # tail of each job log kept as evidence
    github_max_failed_job_logs: int = 5

    # LLM. "ollama" (local, needs a GPU) or "anthropic" / "openai" / "vllm" (no GPU).
    # The investigation code only sees the LLMProvider interface.
    llm_provider: str = "ollama"
    llm_model: str | None = None  # overrides the selected provider's model below
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 300.0
    llm_max_output_tokens: int = 16_000  # API providers; Ollama has no fixed limit
    llm_max_retries: int = 2  # transient failures: connection errors, rate limits, 5xx

    # Chosen over qwen3:8b after evaluation: exact citations and correct root causes (slower).
    ollama_model: str = "hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M"
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_keep_alive: str = "2m"
    ollama_num_ctx: int = 16384  # set explicitly: Ollama's default window is too small for agent runs
    ollama_allow_cloud_models: bool = False  # ":cloud" models send evidence off this host

    # API providers, for servers without a GPU. Evidence leaves the host when these are used.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"
    anthropic_refusal_fallback: bool = True  # a declined request is retried on a fallback model
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o"  # any model that supports JSON-schema output
    openai_base_url: str | None = None  # set for vLLM or another OpenAI-compatible server

    # Agent investigations (Phase 7), triggered manually via `python -m app.cli investigate`
    agent_max_tool_calls: int = 6
    agent_tool_budget_chars: int = 24_000  # total tool output the agent may add to the prompt

    # Investigations: stored in a database that also serves as the queue; one worker processes them in order.
    # SecretStr because a PostgreSQL URL contains a password.
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./data/devinvestigator.db")
    auto_analyze: bool = True  # False: collect and store evidence only
    analysis_mode: Literal["agent", "single_pass"] = "agent"
    investigation_timeout_seconds: float = 900.0
    worker_poll_seconds: float = 5.0

    # Notifications. Posting a comment is the only write DevInvestigator can do, and it is off
    # by default with its own token, so the investigation token stays read-only.
    notify_github_comments: bool = False
    github_comment_token: SecretStr | None = None
    public_base_url: str | None = None  # e.g. https://devinvestigator.example.com, for links in comments

    # Web page (/investigations), HTTP Basic auth. Disabled until DASHBOARD_PASSWORD is set.
    dashboard_username: str = "admin"
    dashboard_password: SecretStr | None = None
    enable_api_docs: bool = False  # /docs and /openapi.json; off because the service is internet-facing

    @property
    def active_model(self) -> str:
        """The model the configured provider will use."""
        if self.llm_model:
            return self.llm_model
        provider = self.llm_provider.strip().lower()
        if provider == "anthropic":
            return self.anthropic_model
        if provider in ("openai", "vllm"):
            return self.openai_model
        return self.ollama_model

    @property
    def github_auth_mode(self) -> str:
        if self.github_app_id:
            return "app"
        if self.github_token:
            return "token"
        return "none"


@lru_cache
def get_settings() -> Settings:
    return Settings()
