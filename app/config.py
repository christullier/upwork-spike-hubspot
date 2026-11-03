"""Settings loaded from environment variables. No secrets live in code."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    return int(raw) if raw else default


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    hubspot_token: str
    pipeline_id: str
    # request_type value ("quote", "coi", ...) or "review" -> HubSpot stage ID
    stage_ids: dict[str, str]
    owner_ids: dict[str, str] = field(default_factory=dict)
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_timeout_seconds: int = 15
    anthropic_max_retries: int = 2
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_max_retries: int = 3
    hubspot_idempotency_property: str | None = "intake_idempotency_key"
    signature_tolerance_seconds: int = 300
    idempotency_db_path: str = "intake_ledger.db"
    idempotency_window_seconds: int = 3600
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        stage_keys = ["quote", "coi", "policy_change", "renewal", "review"]
        required = ["WEBHOOK_SECRET", "HUBSPOT_TOKEN", "HUBSPOT_PIPELINE_ID"]
        required += [f"HUBSPOT_STAGE_{k.upper()}" for k in stage_keys]
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        owners = {k: v for k in stage_keys if (v := _opt(env, f"HUBSPOT_OWNER_{k.upper()}"))}
        idem_prop = env.get("HUBSPOT_IDEMPOTENCY_PROPERTY", "intake_idempotency_key").strip()
        return cls(
            webhook_secret=env["WEBHOOK_SECRET"].strip(),
            hubspot_token=env["HUBSPOT_TOKEN"].strip(),
            pipeline_id=env["HUBSPOT_PIPELINE_ID"].strip(),
            stage_ids={k: env[f"HUBSPOT_STAGE_{k.upper()}"].strip() for k in stage_keys},
            owner_ids=owners,
            anthropic_api_key=_opt(env, "ANTHROPIC_API_KEY"),
            anthropic_model=_opt(env, "ANTHROPIC_MODEL") or "claude-haiku-5-5",
            anthropic_timeout_seconds=_int(env, "ANTHROPIC_TIMEOUT_SECONDS", 15),
            anthropic_max_retries=_int(env, "ANTHROPIC_MAX_RETRIES", 2),
            hubspot_base_url=_opt(env, "HUBSPOT_BASE_URL") or "https://api.hubapi.com",
            hubspot_max_retries=_int(env, "HUBSPOT_MAX_RETRIES", 3),
            hubspot_idempotency_property=idem_prop or None,
            signature_tolerance_seconds=_int(env, "SIGNATURE_TOLERANCE_SECONDS", 300),
            idempotency_db_path=_opt(env, "IDEMPOTENCY_DB_PATH") or "intake_ledger.db",
            idempotency_window_seconds=_int(env, "IDEMPOTENCY_WINDOW_SECONDS", 3600),
            log_level=_opt(env, "LOG_LEVEL") or "INFO",
        )
