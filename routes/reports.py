from flask import Blueprint, request, jsonify, g, send_file, make_response, current_app
import logging
import re
import os
import boto3
import tempfile
from xhtml2pdf import pisa
import fitz  # PyMuPDF
import io
import zipfile
import requests
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai
from authlib.jose import jwt
import json
import time
import psycopg2.extras
from services.youtube_service import transcribe_youtube_video, generate_tldr
from services.auth_service import auth0_validator, AUTH0_DOMAIN
from services.database import get_db_connection

reports_bp = Blueprint('reports', __name__)

@reports_bp.route('/get_reports', methods=['GET'])
def get_reports():
    try:
        # Get token from Authorization header and decode it
        token = request.headers.get('Authorization').split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']

        # Get pagination parameters and search query from query string
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        search_query = request.args.get('search', '').strip()
        offset = (page - 1) * per_page

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Check user's subscription status and get user_id
            cur.execute("""
                SELECT id, subscription_status 
                FROM users 
                WHERE auth0_id = %s
            """, (auth0_id,))
            
            user = cur.fetchone()
            if not user:
                return jsonify({'error': 'User not found'}), 404
            
            # Base query parameters
            query_params = [user['id']]

            # Modify queries based on search parameter
            if search_query:
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_reports 
                    WHERE user_id = %s
                    AND (
                        LOWER(title) LIKE LOWER(%s)
                        OR LOWER(search_query) LIKE LOWER(%s)
                    )
                """
                reports_query = """
                    SELECT id, title, search_query, created_at
                    FROM user_reports 
                    WHERE user_id = %s
                    AND (
                        LOWER(title) LIKE LOWER(%s)
                        OR LOWER(search_query) LIKE LOWER(%s)
                    )
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """
                search_pattern = f'%{search_query}%'
                query_params.extend([search_pattern, search_pattern])
            else:
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_reports 
                    WHERE user_id = %s
                """
                reports_query = """
                    SELECT id, title, search_query, created_at
                    FROM user_reports 
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """

            # Get total count of reports
            cur.execute(count_query, query_params)
            total_reports = cur.fetchone()[0]

            # Add pagination parameters to query
            query_params.extend([per_page, offset])

            # Get paginated reports
            cur.execute(reports_query, query_params)
            
            # Create S3 client for fetching report content
            s3_client = boto3.client(
                's3',
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
            )
            bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")

            reports = []
            for report in cur.fetchall():
                try:
                    # Get report content from S3
                    s3_key = f"reports/{report['id']}"
                    s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
                    content = s3_response['Body'].read().decode('utf-8')
                    
                    reports.append({
                        'id': report['id'],
                        'title': report['title'],
                        'search_query': report['search_query'],
                        'content': content,
                        'created_at': report['created_at'].isoformat()
                    })
                except Exception as e:
                    logging.error(f"Error fetching report content from S3: {str(e)}")
                    # Skip this report if we can't fetch its content
                    continue

            return jsonify({
                'reports': reports,
                'pagination': {
                    'total': total_reports,
                    'page': page,
                    'per_page': per_page,
                    'total_pages': (total_reports + per_page - 1) // per_page
                }
            }), 200

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in get_reports: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@reports_bp.route('/get_report/<string:report_id>', methods=['GET'])
def get_report_by_id(report_id):
    try:
        # Get token from Authorization header and decode it
        token = request.headers.get('Authorization').split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Check user's subscription status and verify report ownership
            cur.execute("""
                SELECT u.subscription_status, r.id, r.title, r.search_query, r.created_at
                FROM users u
                LEFT JOIN user_reports r ON r.user_id = u.id
                WHERE u.auth0_id = %s AND r.id = %s
            """, (auth0_id, report_id))
            
            result = cur.fetchone()
            if not result:
                return jsonify({'error': 'Report not found'}), 404

            try:
                # Get report content from S3
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
                )
                bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
                s3_key = f"reports/{report_id}"
                
                s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
                content = s3_response['Body'].read().decode('utf-8')
                
                return jsonify({
                    'id': result['id'],
                    'title': result['title'],
                    'search_query': result['search_query'],
                    'content': content,
                    'created_at': result['created_at'].isoformat()
                }), 200

            except Exception as e:
                logging.error(f"Error fetching report content from S3: {str(e)}")
                return jsonify({'error': 'Failed to retrieve report content'}), 500

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in get_report_by_id: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@reports_bp.route('/get_free_reports_count', methods=['GET'])
def get_free_reports_count():
    try:
        # Get token from Authorization header and decode it
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401
            
        token = auth_header.split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']
        
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Check user's subscription status
            cur.execute("""
                SELECT id, subscription_status 
                FROM users 
                WHERE auth0_id = %s
            """, (auth0_id,))
            
            user = cur.fetchone()
            if not user:
                return jsonify({'error': 'User not found'}), 404
                
            # If user has active subscription, they have unlimited reports
            if user['subscription_status'] == 'ACTIVE':
                return jsonify({
                    'used_reports': 0,
                    'total_free_reports': 'unlimited'
                }), 200
                
            # Count existing reports for this user
            cur.execute("SELECT COUNT(*) FROM user_reports WHERE user_id = %s", (user['id'],))
            used_reports = cur.fetchone()[0]
            
            return jsonify({
                'used_reports': used_reports,
                'total_free_reports': 2
            }), 200

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in get_free_reports_count: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@reports_bp.route('/get_public_report/<string:public_id>', methods=['GET'])
def get_public_report(public_id):
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # First check the public_shared_reports table
            cur.execute("""
                SELECT user_report_id
                FROM public_shared_reports 
                WHERE id = %s
            """, (public_id,))
            
            shared_report = cur.fetchone()
            if not shared_report:
                return jsonify({'error': 'Public report not found'}), 404

            # Initialize variables
            search_query = None
            user_report_id = shared_report['user_report_id']
            
            # Get the report data
            cur.execute("""
                SELECT id, search_query
                FROM user_reports 
                WHERE id = %s
            """, (user_report_id,))
            report = cur.fetchone()
            if not report:
                return jsonify({'error': 'Report data not found'}), 404
                
            search_query = report['search_query']

            try:
                # Get report content from S3
                s3_key = f"reports/{user_report_id}"
                
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
                )
                bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
                
                s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
                content = s3_response['Body'].read().decode('utf-8')
                
                return jsonify({
                    'search_query': search_query,
                    'content': content,
                }), 200

            except Exception as e:
                logging.error(f"Error fetching report content from S3: {str(e)}")
                return jsonify({'error': 'Failed to retrieve report content'}), 500

    except Exception as e:
        logging.error(f"Error in get_public_report: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@reports_bp.route('/create_public_report', methods=['POST'])
def create_public_report():
    try:
        # Get token from Authorization header and decode it
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401
            
        token = auth_header.split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']
        
        data = request.json
        report_id = data.get('report_id')
        
        if not report_id:
            return jsonify({'error': 'Report ID is required'}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verify report ownership (but don't check subscription status)
            cur.execute("""
                SELECT r.id 
                FROM user_reports r
                JOIN users u ON r.user_id = u.id
                WHERE r.id = %s AND u.auth0_id = %s
            """, (report_id, auth0_id))
            
            result = cur.fetchone()
            if not result:
                return jsonify({'error': 'Report not found or not owned by user'}), 404
            
            # Check for existing public share
            cur.execute("""
                SELECT id
                FROM public_shared_reports
                WHERE user_report_id = %s
            """, (report_id,))
            
            existing_share = cur.fetchone()
            if existing_share:
                return jsonify({
                    'public_id': existing_share['id']
                }), 200

            # Create new public share entry if none exists
            try:
                cur.execute("""
                    INSERT INTO public_shared_reports 
                    (user_report_id, created_at)
                    VALUES (%s, NOW())
                    RETURNING id
                """, (report_id,))
                conn.commit()
                
                public_id = cur.fetchone()['id']
                return jsonify({
                    'public_id': public_id
                }), 201

            except Exception as e:
                conn.rollback()
                logging.error(f"Database error creating public share: {str(e)}")
                return jsonify({'error': 'Failed to create public share'}), 500

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in create_public_report: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500    