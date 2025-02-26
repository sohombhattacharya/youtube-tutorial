import os
import logging
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Environment variables
    APP_ENV = os.getenv('APP_ENV', 'production')
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
    AUTH0_AUDIENCE = os.getenv('AUTH0_AUDIENCE')
    STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
    STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
    S3_NOTES_BUCKET_NAME = os.getenv("S3_NOTES_BUCKET_NAME")
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')

    # Database config
    DB_NAME = os.getenv('DB_NAME')
    DB_USER = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_HOST = os.getenv('DB_HOST')
    DB_PORT = os.getenv('DB_PORT')
    SQLALCHEMY_DATABASE_URI = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Proxy settings
    PROXY_USERNAME = os.getenv('PROXY_USERNAME', 'spclyk9gey')
    PROXY_PASSWORD = os.getenv('PROXY_PASSWORD', '2Oujegb7i53~YORtoe')
    PROXY_HOST = os.getenv('PROXY_HOST', 'gate.smartproxy.com')
    PROXY_PORT = os.getenv('PROXY_PORT', '10001')
    PROXY_ROTATE_PORT = os.getenv('PROXY_ROTATE_PORT', '7000')

    # CORS settings
    CORS_ORIGINS = [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://swiftnotes.ai",
        "https://deploy-preview-1--swiftnotesai.netlify.app"
    ]

    # Logging configuration
    LOG_LEVEL = logging.DEBUG if APP_ENV == 'development' else logging.INFO 


class DevelopmentConfig(Config):
    DEBUG = True
    

class ProductionConfig(Config):
    DEBUG = False


# Create config based on environment
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': ProductionConfig
}

# For direct imports
APP_ENV = Config.APP_ENV
LOG_LEVEL = Config.LOG_LEVEL
DB_NAME = Config.DB_NAME
DB_USER = Config.DB_USER
DB_PASSWORD = Config.DB_PASSWORD
DB_HOST = Config.DB_HOST
DB_PORT = Config.DB_PORT 