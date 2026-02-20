import logging

from megafix.shared.settings import get_settings

_NOISY_THIRD_PARTY_LOGGERS = (
    "httpx",
    "httpcore",
    "openai",
    "urllib3",
)


def setup_logging() -> None:
    """Configure global logging based on settings."""
    settings = get_settings()
    app_level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger().setLevel(app_level)
    _configure_third_party_loggers()


def _configure_third_party_loggers() -> None:
    """Keep third-party network clients concise at INFO-level app logging."""
    for logger_name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def setup_logger() -> logging.Logger:
    """Backward-compatible wrapper returning the root logger."""
    setup_logging()
    return logging.getLogger()
