from app.core.config import Settings
from app.llm.anthropic import AnthropicProvider
from app.llm.base import LLMError, LLMProvider
from app.llm.ollama import OllamaProvider
from app.llm.openai import OpenAIProvider


def build_llm_provider(settings: Settings) -> LLMProvider:
    """Build the configured provider. The investigation code never sees which one it is."""
    provider = settings.llm_provider.strip().lower()
    model = settings.llm_model  # optional override of the provider's own model setting

    if provider == "ollama":
        return OllamaProvider(
            settings.ollama_base_url,
            model or settings.ollama_model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            keep_alive=settings.ollama_keep_alive,
            num_ctx=settings.ollama_num_ctx,
            allow_cloud_models=settings.ollama_allow_cloud_models,
            max_retries=settings.llm_max_retries,
        )

    if provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise LLMError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        return AnthropicProvider(
            settings.anthropic_api_key.get_secret_value(),
            model or settings.anthropic_model,
            max_tokens=settings.llm_max_output_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            refusal_fallback=settings.anthropic_refusal_fallback,
        )

    if provider in ("openai", "vllm"):
        base_url = settings.openai_base_url
        if provider == "vllm" and not base_url:
            raise LLMError("LLM_PROVIDER=vllm needs OPENAI_BASE_URL (the server's /v1 URL)")
        if settings.openai_api_key is not None:
            api_key = settings.openai_api_key.get_secret_value()
        elif base_url:
            api_key = "not-needed"  # self-hosted servers usually ignore the key
        else:
            raise LLMError("LLM_PROVIDER=openai needs OPENAI_API_KEY")
        return OpenAIProvider(
            api_key,
            model or settings.openai_model,
            base_url=base_url,
            max_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    raise LLMError(f"Unknown LLM provider {provider!r}; use ollama, anthropic, openai or vllm")
