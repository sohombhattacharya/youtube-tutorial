from flask import Blueprint, request, jsonify, g
import logging
import os
import uuid
from authlib.jose import jwt
from services.auth_service import auth0_validator, AUTH0_DOMAIN
from services.database import get_db_connection
from datetime import datetime

api_customer_bp = Blueprint('api_customer', __name__)

@api_customer_bp.route('/create_api_key', methods=['POST'])
def create_api_key():
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
    Get API usage statistics for a specific API key.
    
    Required parameters:
    - api_key: The API key to get usage statistics for
    
    Optional parameters:
    - period: Aggregation period ('day', 'week', or 'month'). Default is 'day'.
    - start_date: Filter results from this date (format: YYYY-MM-DD)
    - end_date: Filter results until this date (format: YYYY-MM-DD)
    
    Authentication:
    - Requires a valid Auth0 Bearer token in the Authorization header
    - The API key must belong to the authenticated user
    
    Returns:
    - Usage data aggregated by the specified period
    - List of individual API calls (limited to 100)
    - Credit limit and usage information
    """
    try:
        # Validate required parameters
        api_key = request.args.get('api_key')
        if not api_key:
            return jsonify({
                'error': 'Missing parameter',
                'message': 'The api_key parameter is required'
            }), 400
            
        # Validate optional parameters
        period = request.args.get('period', 'day').lower()
        if period not in ['day', 'week', 'month']:
            return jsonify({
                'error': 'Invalid parameter',
                'message': 'Period must be one of: day, week, month'
            }), 400
            
        start_date = request.args.get('start_date')
        if start_date:
            try:
                # Validate date format
                datetime.strptime(start_date, '%Y-%m-%d')
            except ValueError:
                return jsonify({
                    'error': 'Invalid parameter',
                    'message': 'start_date must be in YYYY-MM-DD format'
                }), 400
                
        end_date = request.args.get('end_date')
        if end_date:
            try:
                # Validate date format
                datetime.strptime(end_date, '%Y-%m-%d')
            except ValueError:
                return jsonify({
                    'error': 'Invalid parameter',
                    'message': 'end_date must be in YYYY-MM-DD format'
                }), 400
        
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
                
                # Build the SQL query based on the period
                date_trunc_expr = f"DATE_TRUNC('{period}', created_at)"
                
                query = f"""
                SELECT 
                    {date_trunc_expr} AS period_start,
                    SUM(credits_used) AS total_credits
                FROM 
                    api_calls
                WHERE 
                    api_key = %s
                """
                
                params = [api_key]
                
                # Add date range filters if provided
                if start_date:
                    query += " AND created_at >= %s"
                    params.append(start_date)
                
                if end_date:
                    query += " AND created_at <= %s"
                    params.append(end_date)
                
                # Group by the period and order by date
                query += f"""
                GROUP BY 
                    {date_trunc_expr}
                ORDER BY 
                    period_start
                """
                
                cur.execute(query, params)
                
                # Format the results
                usage_data = []
                for row in cur.fetchall():
                    usage_data.append({
                        'period_start': row[0].isoformat(),
                        'credits_used': float(row[1]) if row[1] else 0
                    })
                
                # Get individual API calls
                call_query = """
                SELECT 
                    endpoint,
                    status_code,
                    latency_ms,
                    created_at,
                    credits_used
                FROM 
                    api_calls
                WHERE 
                    api_key = %s
                """
                
                call_params = [api_key]
                
                # Add date range filters if provided
                if start_date:
                    call_query += " AND created_at >= %s"
                    call_params.append(start_date)
                
                if end_date:
                    call_query += " AND created_at <= %s"
                    call_params.append(end_date)
                
                # Order by date
                call_query += " ORDER BY created_at DESC"
                
                # Add limit to prevent too many results
                call_query += " LIMIT 100"
                
                cur.execute(call_query, call_params)
                
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
                        AND created_at >= DATE_TRUNC('month', CURRENT_DATE)
                    """,
                    (api_key,)
                )
                
                current_month_usage = cur.fetchone()[0] or 0
                
                return jsonify({
                    'api_key': api_key,
                    'period': period,
                    'usage_data': usage_data,
                    'api_calls': api_calls,
                    'credit_limit': credit_limit,
                    'current_month_usage': float(current_month_usage),
                    'remaining_credits': max(0, credit_limit - float(current_month_usage))
                }), 200
                
        except Exception as e:
            logging.error(f"Database error in get_api_usage: {str(e)}")
            return jsonify({'error': 'Failed to retrieve API usage data'}), 500
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error in get_api_usage: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500