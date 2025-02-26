import logging
import sys

# Custom filter to exclude OPTIONS and POST requests
class HTTPFilter(logging.Filter):
    def filter(self, record):
        # Check if this is a Werkzeug access log
        if 'werkzeug' in record.name.lower():
            return False  # Filter out all Werkzeug logs
        return True

def configure_logging(log_level):
    # Configure logging
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)  # Explicitly use stdout
        ],
        force=True  # Force override any existing configuration
    )

    # Add filter to both Werkzeug logger and root logger
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.addFilter(HTTPFilter())
    logging.getLogger().addFilter(HTTPFilter())

    # Also set Werkzeug logger level to ERROR to suppress most messages
    werkzeug_logger.setLevel(logging.ERROR)

    logging.info("=== Application Starting ===")
    logging.debug("Debug logging enabled - running in development mode") 