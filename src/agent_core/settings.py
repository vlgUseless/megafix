from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _strtobool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {raw}") from exc


def _read_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {raw}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


@dataclass(frozen=True)
class Settings:
    github_app_id: str | None
    github_private_key: str | None
    github_private_key_path: Path | None
    webhook_secret: str | None
    redis_url: str
    rq_queue: str
    rq_job_timeout: str
    rq_result_ttl: int
    rq_failure_ttl: int
    delivery_ttl_sec: int
    agent_workdir: Path
    keep_workdir: bool
    comment_progress: bool
    github_user_agent: str | None
    github_app_name: str | None
    log_level: str
    apply_cmd: str | None
    review_state_db: Path
    llm_service_url: str | None
    llm_service_api_key: str | None
    llm_service_timeout_sec: int
    llm_service_model: str | None
    llm_max_tokens: int | None
    llm_max_relevant_files: int
    llm_max_file_bytes: int
    llm_max_tree_entries: int
    review_max_diff_chars: int
    review_max_patch_chars: int
    review_max_log_chars: int
    review_rerun_max_attempts: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        github_app_id=os.getenv("GITHUB_APP_ID"),
        github_private_key=os.getenv("GITHUB_PRIVATE_KEY"),
        github_private_key_path=_read_path("GITHUB_PRIVATE_KEY_PATH"),
        webhook_secret=os.getenv("WEBHOOK_SECRET"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        rq_queue=os.getenv("RQ_QUEUE", "default"),
        rq_job_timeout=os.getenv("RQ_JOB_TIMEOUT", "20m"),
        rq_result_ttl=_read_int("RQ_RESULT_TTL", 3600),
        rq_failure_ttl=_read_int("RQ_FAILURE_TTL", 86400),
        delivery_ttl_sec=_read_int("DELIVERY_TTL_SEC", 86400),
        agent_workdir=Path(os.getenv("AGENT_WORKDIR", ".agent_workdir")),
        keep_workdir=_strtobool(os.getenv("KEEP_WORKDIR")),
        comment_progress=_strtobool(os.getenv("COMMENT_PROGRESS")),
        github_user_agent=os.getenv("GITHUB_USER_AGENT"),
        github_app_name=os.getenv("GITHUB_APP_NAME"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        apply_cmd=os.getenv("AGENT_APPLY_CMD"),
        review_state_db=_read_path("REVIEW_STATE_DB") or Path("review_state.db"),
        llm_service_url=os.getenv("LLM_SERVICE_URL"),
        llm_service_api_key=os.getenv("LLM_SERVICE_API_KEY")
        or os.getenv("OPENAI_API_KEY"),
        llm_service_timeout_sec=_read_int("LLM_SERVICE_TIMEOUT_SEC", 60),
        llm_service_model=os.getenv("LLM_SERVICE_MODEL"),
        llm_max_tokens=_read_optional_int("LLM_MAX_TOKENS"),
        llm_max_relevant_files=_read_int("LLM_MAX_RELEVANT_FILES", 12),
        llm_max_file_bytes=_read_int("LLM_MAX_FILE_BYTES", 50_000),
        llm_max_tree_entries=_read_int("LLM_MAX_TREE_ENTRIES", 5_000),
        review_max_diff_chars=_read_int("REVIEW_MAX_DIFF_CHARS", 120_000),
        review_max_patch_chars=_read_int("REVIEW_MAX_PATCH_CHARS", 6_000),
        review_max_log_chars=_read_int("REVIEW_MAX_LOG_CHARS", 4_000),
        review_rerun_max_attempts=_read_int("REVIEW_RERUN_MAX_ATTEMPTS", 5),
    )


def _read_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if not raw:
        return None
    return Path(raw)


def load_private_key(settings: Settings) -> str:
    if settings.github_private_key:
        return settings.github_private_key.replace("\\n", "\n")
    if settings.github_private_key_path:
        return settings.github_private_key_path.read_text(encoding="utf-8")
    raise RuntimeError(
        "Missing GitHub App private key. Set GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH."
    )
