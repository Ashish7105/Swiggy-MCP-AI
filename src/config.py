"""Configuration loading for the Swiggy conversational assistant.

Everything here is local, personal-use config: your own Groq API key and
the Swiggy MCP server you want to talk to. No multi-tenant settings,
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

# Official Swiggy MCP endpoints, per swiggy-mcp-server-manifest.
SWIGGY_FOOD_URL = os.environ.get("SWIGGY_FOOD_MCP_URL", "https://mcp.swiggy.com/food")
SWIGGY_INSTAMART_URL = os.environ.get("SWIGGY_INSTAMART_MCP_URL", "https://mcp.swiggy.com/im")
SWIGGY_DINEOUT_URL = os.environ.get("SWIGGY_DINEOUT_MCP_URL", "https://mcp.swiggy.com/dineout")


@dataclass(frozen=True)
class Settings:
    groq_api_key: str
    groq_model: str
    groq_fallback_models: tuple[str, ...]
    default_address_label: str
    swiggy_food_url: str
    oauth_redirect_host: str
    oauth_redirect_port: int

    @property
    def oauth_redirect_uri(self) -> str:
        return f"http://{self.oauth_redirect_host}:{self.oauth_redirect_port}/callback"


def load_settings() -> Settings:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return Settings(
        groq_api_key=api_key,
        # Must be a Groq model that supports tool/function calling; which
        # ones your key can use varies (run list_groq_models.py to check).
        groq_model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
        # Tried in order when the main model hits a Groq rate limit or
        # daily quota. Free-tier limits are per model, so each fallback
        # is a fresh budget.
        groq_fallback_models=tuple(
            m.strip()
            for m in os.environ.get(
                "GROQ_FALLBACK_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b"
            ).split(",")
            if m.strip()
        ),
        # Label of the saved Swiggy address (as shown in the app, e.g.
        # "Home", "Work") to deliver to unless you name another one.
        # Leave empty to have the assistant ask each time.
        default_address_label=os.environ.get("DEFAULT_ADDRESS_LABEL", "Home").strip(),
        swiggy_food_url=SWIGGY_FOOD_URL,
        oauth_redirect_host=os.environ.get("OAUTH_REDIRECT_HOST", "localhost"),
        oauth_redirect_port=int(os.environ.get("OAUTH_REDIRECT_PORT", "8765")),
    )
