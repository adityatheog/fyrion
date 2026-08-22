"""
Application entry point.
"""
import logging
from fyrion.config import Config
from fyrion.logging.logger import setup_logging
from fyrion.bot import Fyrion

def main() -> None:
    # 1. Validate environment securely before anything else
    Config.validate()
    
    # 2. Initialize structured logging
    setup_logging()
    log = logging.getLogger("fyrion.main")
    
    # 3. Instantiate bot
    bot = Fyrion()
    
    try:
        log.info("Starting Fyrion process...")
        # log_handler=None prevents discord.py from overriding our custom logging configuration
        bot.run(Config.DISCORD_TOKEN, log_handler=None)
    except Exception as e:
        log.critical(f"Fatal error during runtime: {e}", exc_info=True)

if __name__ == "__main__":
    main()
