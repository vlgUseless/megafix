import logging

from agent_core.settings import get_settings


def setup_logging() -> None:
    """Configure global logging based on settings."""
    settings = get_settings()
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))


def setup_logger() -> logging.Logger:
    """Backward-compatible wrapper returning the root logger."""
    setup_logging()
    return logging.getLogger()
