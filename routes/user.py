from services.auth_service import auth0_validator, AUTH0_DOMAIN
from services.database import get_db_connection
from flask import Blueprint, request, jsonify, g, send_file, make_response, current_app
import psycopg2.extras
import logging
import os
from authlib.jose import jwt
from services.youtube_service import transcribe_youtube_video, generate_tldr

user_bp = Blueprint('user', __name__)

@user_bp.route('/get_user', methods=['GET'])
def get_user():
    # Get the token from the Authorization header
    token = request.headers.get('Authorization').split(' ')[1]
    
    # Decode the JWT token with verification
    decoded = jwt.decode(
        token,
        auth0_validator.public_key,  # Use the public key from your validator
        claims_options={
            "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
            "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
        }
    )
    auth0_id = decoded['sub']  # Get the Auth0 user ID from the decoded token
    email = request.args.get('email')

    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Try to find existing user
            cur.execute("""
                SELECT id, email, auth0_id, subscription_status, subscription_cancelled_period_ends_at
                FROM users 
                WHERE auth0_id = %s
            """, (auth0_id,))
            
            user = cur.fetchone()
            
            if user is None:
                # User doesn't exist, create new user
                cur.execute("""
                    INSERT INTO users 
                    (email, auth0_id, subscription_status, created_at, updated_at)
                    VALUES (%s, %s, 'INACTIVE', NOW(), NOW())
                    RETURNING id, email, auth0_id, subscription_status, subscription_cancelled_period_ends_at
                """, (email, auth0_id))
                user = cur.fetchone()
                conn.commit()
                logging.info(f"Created new user with auth0_id: {auth0_id}")
            
            # Convert to dictionary for JSON response
            user_data = {
                'id': user['id'],
                'email': user['email'],
                'auth0_id': user['auth0_id'],
                'subscription_status': user['subscription_status'],
                'subscription_ends_at': user['subscription_cancelled_period_ends_at'].isoformat() if user['subscription_cancelled_period_ends_at'] else None,
            }
            return jsonify(user_data), 200

    except Exception as e:
        logging.error(f"Database error in get_user: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
