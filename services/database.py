import os
import logging
import psycopg2.pool
from flask import g, current_app

def get_db_connection():
    if not hasattr(g, '_database'):
        g._database = current_app.db_pool.getconn()
    return g._database

def setup_database(app):
    try:
        app.db_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dbname=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            host=os.getenv('DB_HOST'),
            port=os.getenv('DB_PORT')
        )
        logging.info("Successfully created database connection pool")
    except Exception as e:
        logging.error(f"Failed to create database connection pool: {str(e)}")
        raise

    @app.teardown_appcontext
    def close_db_connection(exception):
        db = getattr(g, '_database', None)
        if db is not None:
            app.db_pool.putconn(db)
            g._database = None