from flask import Blueprint, request, jsonify, g
import logging
import os
import uuid
from authlib.jose import jwt
from services.auth_service import auth0_validator, AUTH0_DOMAIN
from services.database import get_db_connection
from datetime import datetime, timezone
import calendar

api_customer_bp = Blueprint('api_customer', __name__)

@api_customer_bp.route('/create_api_key', methods=['POST'])
def create_api_key():
    """
    Create a new API key for the authenticated user.
    
    Request body (JSON):
    - name: Optional name for the API key. Default is 'Default API Key'.
    
    Authentication:
    - Requires a valid Auth0 Bearer token in the Authorization header
    
    Restrictions:
    - Users are limited to one API key per account
    
    Returns:
    - 201 Created: Successfully created API key
      {
        "api_key": "uuid-string",
        "name": "string"
      }
    
    Errors:
    - 400 Bad Request: Invalid request format
    - 401 Unauthorized: Missing or invalid authentication
    - 403 Forbidden: API key limit reached
      {
        "error": "API key limit reached",
        "message": "You can only have one API key per account"
      }
    - 404 Not Found: User not found
    - 500 Internal Server Error: Server-side error
    """
    try:
        # Get and validate auth header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401

        # Process authentication token
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
                token,
                auth0_validator.public_key,
                claims_options={
                    "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                    "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )
            auth0_id = decoded_token['sub']
        except Exception as e:
            logging.error(f"Error verifying token: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401

        # Get optional name for the API key
        data = request.get_json() or {}
        key_name = data.get('name', 'Default API Key')

        # Get user from database
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Get user ID
                cur.execute(
                    "SELECT id FROM users WHERE auth0_id = %s",
                    (auth0_id,)
                )
                result = cur.fetchone()
                if not result:
                    return jsonify({'error': 'User not found'}), 404
                
                user_id = result[0]
                
                # Check if user already has an API key
                cur.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE user_id = %s",
                    (user_id,)
                )
                key_count = cur.fetchone()[0]
                
                if key_count > 0:
                    return jsonify({
                        'error': 'API key limit reached',
                        'message': 'You can only have one API key per account'
                    }), 403
                
                # Generate a new API key
                api_key = str(uuid.uuid4())
                
                # Store the API key
                cur.execute(
                    """
                    INSERT INTO api_keys (user_id, api_key, name)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (user_id, api_key, key_name)
                )
                conn.commit()
                
                return jsonify({
                    'api_key': api_key,
                    'name': key_name,
                }), 201
                
        except Exception as e:
            conn.rollback()
            logging.error(f"Database error: {str(e)}")
            return jsonify({'error': 'Failed to create API key'}), 500
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error in create_api_key: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@api_customer_bp.route('/list_api_keys', methods=['GET'])
def list_api_keys():
    """
    List all API keys belonging to the authenticated user.
    
    Authentication:
    - Requires a valid Auth0 Bearer token in the Authorization header
    
    Returns:
    - 200 OK: List of API keys
      {
        "api_keys": [
          {
            "id": "string",
            "api_key": "uuid-string",
            "name": "string",
            "created_at": "ISO-8601 datetime string"
          }
        ]
      }
    
    Errors:
    - 401 Unauthorized: Missing or invalid authentication
    - 404 Not Found: User not found
    - 500 Internal Server Error: Server-side error
    """
    try:
        # Get and validate auth header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401

        # Process authentication token
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
                token,
                auth0_validator.public_key,
                claims_options={
                    "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                    "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )
            auth0_id = decoded_token['sub']
        except Exception as e:
            logging.error(f"Error verifying token: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401

        # Get user's API keys
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Get user ID
                cur.execute(
                    "SELECT id FROM users WHERE auth0_id = %s",
                    (auth0_id,)
                )
                result = cur.fetchone()
                if not result:
                    return jsonify({'error': 'User not found'}), 404
                
                user_id = result[0]
                
                # Get all API keys for this user
                cur.execute(
                    """
                    SELECT id, api_key, name, created_at
                    FROM api_keys
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (user_id,)
                )
                
                keys = []
                for row in cur.fetchall():
                    keys.append({
                        'id': str(row[0]),
                        'api_key': row[1],
                        'name': row[2],
                        'created_at': row[3].isoformat()
                    })
                
                return jsonify({'api_keys': keys}), 200
                
        except Exception as e:
            logging.error(f"Database error: {str(e)}")
            return jsonify({'error': 'Failed to retrieve API keys'}), 500
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error in list_api_keys: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@api_customer_bp.route('/get_api_usage', methods=['GET'])
def get_api_usage():
    """
    Get API usage statistics for a specific API key, aggregated by day for a single month.
    
    Required parameters:
    - api_key: The API key to get usage statistics for
    
    Optional parameters:
    - month: Month to get usage for in YYYY-MM format (e.g., '2023-03'). Defaults to current month.
    
    Authentication:
    - Requires a valid Auth0 Bearer token in the Authorization header
    - The API key must belong to the authenticated user
    
    Returns:
    - 200 OK: API usage data
      {
        "api_key": "uuid-string",
        "month": "YYYY-MM",
        "daily_usage": [
          {
            "date": "YYYY-MM-DD",
            "credits_used": float
          }
        ],
        "api_calls": [
          {
            "endpoint": "string",
            "status_code": integer,
            "latency_ms": integer,
            "timestamp": "ISO-8601 datetime string in UTC",
            "credits_used": float
          }
        ],
        "credit_limit": integer,
        "current_month_usage": float,
        "remaining_credits": float
      }
    
    Errors:
    - 400 Bad Request: Missing or invalid parameters
    - 401 Unauthorized: Missing or invalid authentication
    - 404 Not Found: API key not found
    - 500 Internal Server Error: Server-side error
    """
    try:
        # Validate required parameters
        api_key = request.args.get('api_key')
        if not api_key:
            return jsonify({
                'error': 'Missing parameter',
                'message': 'The api_key parameter is required'
            }), 400
            
        # Get month parameter (default to current month)
        month = request.args.get('month')
        if month:
            try:
                # Validate month format
                month_date = datetime.strptime(month, '%Y-%m').replace(tzinfo=timezone.utc)
                start_date = f"{month}-01"
                next_month = datetime.strptime(month, '%Y-%m')
                if next_month.month == 12:
                    end_date = f"{next_month.year + 1}-01-01"
                else:
                    end_date = f"{next_month.year}-{next_month.month + 1:02d}-01"
            except ValueError:
                return jsonify({
                    'error': 'Invalid parameter',
                    'message': 'month must be in YYYY-MM format'
                }), 400
        else:
            # Default to current month in UTC
            today = datetime.now(timezone.utc)
            month_date = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
            start_date = f"{today.year}-{today.month:02d}-01"
            if today.month == 12:
                end_date = f"{today.year + 1}-01-01"
            else:
                end_date = f"{today.year}-{today.month + 1:02d}-01"
            month = f"{today.year}-{today.month:02d}"
        
        # Get and validate auth header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401

        # Process authentication token
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
                token,
                auth0_validator.public_key,
                claims_options={
                    "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                    "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )
            auth0_id = decoded_token['sub']
        except Exception as e:
            logging.error(f"Error verifying token: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401
            
        # Connect to database
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Verify the API key belongs to the authenticated user
                cur.execute(
                    """
                    SELECT api_keys.id 
                    FROM api_keys 
                    JOIN users ON api_keys.user_id = users.id 
                    WHERE api_keys.api_key = %s AND users.auth0_id = %s
                    """,
                    (api_key, auth0_id)
                )
                
                if not cur.fetchone():
                    return jsonify({'error': 'API key not found'}), 404
                
                # Get daily usage for the specified month
                query = """
                SELECT 
                    DATE(created_at AT TIME ZONE 'UTC') AS usage_date,
                    SUM(credits_used) AS total_credits
                FROM 
                    api_calls
                WHERE 
                    api_key = %s
                    AND created_at >= %s
                    AND created_at < %s
                GROUP BY 
                    DATE(created_at AT TIME ZONE 'UTC')
                ORDER BY 
                    usage_date
                """
                
                cur.execute(query, (api_key, start_date, end_date))
                
                # Create a dictionary to store usage by date
                usage_by_date = {}
                for row in cur.fetchall():
                    usage_by_date[row[0].isoformat()] = float(row[1]) if row[1] else 0
                
                # Generate all days in the month
                _, num_days = calendar.monthrange(month_date.year, month_date.month)
                
                # Create the daily usage array with all days of the month
                daily_usage = []
                for day in range(1, num_days + 1):
                    date_str = f"{month_date.year}-{month_date.month:02d}-{day:02d}"
                    daily_usage.append({
                        'date': date_str,
                        'credits_used': usage_by_date.get(date_str, 0)
                    })
                
                # Get individual API calls for the month
                call_query = """
                SELECT 
                    endpoint_name,
                    status_code,
                    response_time_ms,
                    created_at AT TIME ZONE 'UTC' as created_at_utc,
                    credits_used
                FROM 
                    api_calls
                WHERE 
                    api_key = %s
                    AND created_at >= %s
                    AND created_at < %s
                ORDER BY 
                    created_at DESC
                LIMIT 100
                """
                
                cur.execute(call_query, (api_key, start_date, end_date))
                
                # Format the API calls
                api_calls = []
                for row in cur.fetchall():
                    api_calls.append({
                        'endpoint': row[0],
                        'status_code': row[1],
                        'latency_ms': row[2],
                        'timestamp': row[3].isoformat(),
                        'credits_used': float(row[4]) if row[4] else 0
                    })
                
                # Get subscription information
                cur.execute(
                    """
                    SELECT 
                        users.subscription_status,
                        users.product_id
                    FROM 
                        users
                    JOIN 
                        api_keys ON users.id = api_keys.user_id
                    WHERE 
                        api_keys.api_key = %s
                    """,
                    (api_key,)
                )
                
                subscription_info = cur.fetchone()
                subscription_status = subscription_info[0] if subscription_info else None
                subscription_product_id = subscription_info[1] if subscription_info else None
                
                # Calculate credit limits
                credit_limit = 0
                PRO_PLAN_PRODUCT_ID = os.getenv('PRO_PLAN_PRODUCT_ID')
                
                if subscription_status != 'ACTIVE':
                    credit_limit = 500  # Free user
                elif subscription_product_id == PRO_PLAN_PRODUCT_ID:
                    credit_limit = 1500  # Pro user
                
                # Get current month's total usage
                cur.execute(
                    """
                    SELECT 
                        SUM(credits_used)
                    FROM 
                        api_calls
                    WHERE 
                        api_key = %s
                        AND created_at >= DATE_TRUNC('month', CURRENT_DATE AT TIME ZONE 'UTC')
                    """,
                    (api_key,)
                )
                
                current_month_usage = cur.fetchone()[0] or 0
                
                return jsonify({
                    'api_key': api_key,
                    'month': month,
                    'daily_usage': daily_usage,
                    'api_calls': api_calls,
                    'credit_limit': credit_limit,
                    'current_month_usage': float(current_month_usage),
                    'remaining_credits': max(0, credit_limit - float(current_month_usage))
                }), 200
                
        except Exception as e:
            conn.rollback()
            logging.error(f"Database error in get_api_usage: {str(e)}")
            return jsonify({'error': 'Failed to retrieve API usage data'}), 500
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error in get_api_usage: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500