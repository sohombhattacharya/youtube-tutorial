import logging
import sys
import os
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv
from services.auth_service import setup_auth
from services.database import setup_database
from config import config, APP_ENV, LOG_LEVEL

# Configure logging - must be first!
class HTTPFilter(logging.Filter):
    def filter(self, record):
        if 'werkzeug' in record.name.lower():
            return False
        return True

def setup_logging():
    log_level = logging.INFO if os.getenv('APP_ENV') == 'development' else logging.INFO
    
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True
    )

    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.addFilter(HTTPFilter())
    logging.getLogger().addFilter(HTTPFilter())
    werkzeug_logger.setLevel(logging.ERROR)

    logging.info("=== Application Starting ===")
    logging.debug("Debug logging enabled - running in development mode")

def create_app(config_name=APP_ENV):
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Configure CORS using settings from config
    CORS(app, origins=app.config['CORS_ORIGINS'], supports_credentials=True)

    # Setup services
    setup_logging()
    setup_database(app)
    setup_auth(app)

    # Register blueprints
    from routes.search import search_bp
    from routes.user import user_bp
    from routes.reports import reports_bp
    from routes.notes import notes_bp
    from routes.payments import payments_bp
    from routes.feedback import feedback_bp

    app.register_blueprint(search_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(feedback_bp)

    return app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
