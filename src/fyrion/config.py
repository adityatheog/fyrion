"""
Configuration and environment variable management.
Never import or log DISCORD_TOKEN directly outside of the bot initialization.
"""
import os
import sys
from typing import Final
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

class Config:
    # Security: Token is required.
    DISCORD_TOKEN: Final[str | None] = os.getenv("DISCORD_TOKEN")
    
    # Database configuration
    DATABASE_URL: Final[str] = os.getenv("DATABASE_URL", "fyrion.db")
    
    # Application settings
    LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO").upper()
    ENVIRONMENT: Final[str] = os.getenv("ENVIRONMENT", "production").lower()

    @classmethod
    def validate(cls) -> None:
        """
        Validates that all required configuration variables are present.
        Fails fast if security constraints are not met.
        """
        if not cls.DISCORD_TOKEN or cls.DISCORD_TOKEN == "your_bot_token_here":
            # We print to stderr directly because the logger might not be fully initialized yet
            sys.stderr.write("CRITICAL ERROR: DISCORD_TOKEN is missing or invalid in environment.\n")
            sys.exit(1)
