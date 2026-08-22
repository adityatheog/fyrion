"""
Application logging configuration.
"""
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
from fyrion.config import Config

def setup_logging() -> logging.Logger:
    """
    Configures and returns the root logger for Fyrion.
    """
    logger = logging.getLogger("fyrion")
    level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
    logger.setLevel(level)

    if not logger.handlers:
        # Standard Professional Formatter
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        # 1. Console Handler (Standard Output)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 2. File Handler (Rotating, Max 5MB per file, 5 backups)
        os.makedirs("logs", exist_ok=True)
        file_handler = RotatingFileHandler(
            filename="logs/fyrion.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        # Always record DEBUG locally for forensics, regardless of console log level
        file_handler.setLevel(logging.DEBUG) 
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
