"""Structured logging configuration."""
import logging
import sys
from typing import Any

from aiogram import loggers


def setup_logger(level: str = "INFO") -> None:
    """Setup structured logging for the application.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Configure aiogram loggers
    loggers.event.setLevel(logging.WARNING)
    loggers.dispatcher.setLevel(logging.INFO)

    # Silence verbose aiohttp and httpx loggers
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get logger instance.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class LoggerMixin:
    """Mixin to add logger property to classes."""

    @property
    def logger(self) -> logging.Logger:
        """Get logger for this class."""
        return logging.getLogger(self.__class__.__name__)
