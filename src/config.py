"""Configuration loading for the Swiggy conversational assistant.

Everything here is local, personal-use config: your own LLM API keys and
the Swiggy MCP servers you want to talk to. No multi-tenant settings,
because this assistant is designed to be run by one person, for themself,
per the manifest's "no third-party app development" constraint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load a .env file from the project root if present.
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_DIR = PROJECT_ROOT / ".mcp_tokens"
TOKEN_DIR.mkdir(exist_ok=True)

# Swiggy MCP services the assistant can talk to, as "name=url" pairs, per
# swiggy-mcp-server-manifest. Each is an independent MCP server; its tools
# are discovered at runtime, so adding one (e.g. scenes=.../scenes) needs
# no code change.
DEFAULT_SWIGGY_SERVICES = (
    "food=https://mcp.swiggy.com/food,"
    "instamart=https://mcp.swiggy.com/im,"
    "dineout=https://mcp.swiggy.com/dineout"
)


# LLM providers, all spoken to through their OpenAI-compatible endpoints.
LLM_PROVIDER_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
}
LLM_PROVIDER_KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}

# Tried in order; a model that is rate limited, out of daily quota, or
# overloaded hands the turn to the next one. Free-tier limits are per
# model, so each entry is a fresh budget. Gemini first: its free tier
# allows far more tokens per minute than Groq's (~8k).
DEFAULT_LLM_MODELS = (
    "gemini:gemini-flash-latest,"
    "gemini:gemini-3.5-flash-lite,"
    "groq:openai/gpt-oss-20b,"
    "groq:openai/gpt-oss-120b,"
    "groq:qwen/qwen3.8-27b"
)


@dataclass(frozen=True)
class Settings:
    llm_api_keys: dict[str, str]
    # "provider:model" entries, in fallback order.
    llm_models: tuple[str, ...]
    default_address_label: str
    # {service name: MCP server URL}, in the order offered to the user.
    swiggy_services: dict[str, str]
    oauth_redirect_host: str
    oauth_redirect_port: int

    @property
    def oauth_redirect_uri(self) -> str:
        return f"http://{self.oauth_redirect_host}:{self.oauth_redirect_port}/callback"


def load_settings() -> Settings:
    api_keys = {
        provider: os.environ[env].strip()
        for provider, env in LLM_PROVIDER_KEY_ENV.items()
        if os.environ.get(env, "").strip()
    }
    # Models whose provider has no key are skipped, so either key alone works.
    models = tuple(
        entry
        for entry in (m.strip() for m in os.environ.get("LLM_MODELS", DEFAULT_LLM_MODELS).split(","))
        if entry and entry.split(":", 1)[0] in api_keys
    )
    if not models:
        raise RuntimeError(
            "No usable LLM configured. Set GEMINI_API_KEY and/or GROQ_API_KEY in .env "
            "(and check LLM_MODELS uses 'provider:model' entries for those providers)."
        )
    return Settings(
        llm_api_keys=api_keys,
        llm_models=models,
        # Label of the saved Swiggy address (as shown in the app, e.g.
        # "Home", "Work") to deliver to unless you name another one.
        # Leave empty to have the assistant ask each time.
        default_address_label=os.environ.get("DEFAULT_ADDRESS_LABEL", "Home").strip(),
        swiggy_services={
            name.strip().lower(): url.strip()
            for name, _, url in (
                pair.partition("=")
                for pair in os.environ.get("SWIGGY_SERVICES", DEFAULT_SWIGGY_SERVICES).split(",")
            )
            if name.strip() and url.strip()
        },
        oauth_redirect_host=os.environ.get("OAUTH_REDIRECT_HOST", "localhost"),
        oauth_redirect_port=int(os.environ.get("OAUTH_REDIRECT_PORT", "8765")),
    )
