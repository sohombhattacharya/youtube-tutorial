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

feedback_bp = Blueprint('feedback', __name__)

@feedback_bp.route('/feedback', methods=['POST'])
def get_feedback():
    try:
        # Get request data
        data = request.json
        is_tldr = data.get('isTLDR', False)
        youtube_video_id = data.get('video_id')
        title = data.get('title')
        feedback_text = data.get('feedback')
        was_helpful = data.get('wasHelpful')
        visitor_id = data.get('visitor_id')

        if not youtube_video_id:
            return jsonify({'error': 'YouTube video ID is required'}), 400

        # Initialize auth0_id as None
        auth0_id = None


        helpful = None
        if was_helpful is not None:
            helpful = was_helpful    
        # Check for Bearer token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
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
                logging.error(f"Error processing token: {type(e).__name__}: {str(e)}")
                # Continue execution to check for visitor_id

        # Verify we have either auth0_id or visitor_id
        if not auth0_id and not visitor_id:
            return jsonify({'error': 'Authentication required'}), 401

        # Store feedback in database
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_feedback (
                    auth0_id,
                    visitor_id,
                    youtube_video_id,
                    youtube_video_title,
                    feedback_text,
                    was_helpful,
                    is_tldr
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                auth0_id,
                visitor_id if not auth0_id else None,  # Only use visitor_id if no auth0_id
                youtube_video_id,
                title,
                feedback_text if feedback_text else None,
                helpful,
                is_tldr
            ))
            feedback_id = cur.fetchone()[0]
            conn.commit()

            # Get the YouTube video URL
            youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"

            logging.info(f"Feedback saved for video {youtube_url} - ID: {feedback_id}")
            
            return jsonify({
                'message': 'Feedback saved successfully',
                'feedback_id': feedback_id
            }), 201

    except Exception as e:
        logging.error(f"Error in get_feedback: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@feedback_bp.route('/check_feedback', methods=['POST'])
def check_feedback():
    try:
        # Get request data
        data = request.json
        youtube_video_id = data.get('video_id')
        visitor_id = data.get('visitor_id')
        is_tldr = data.get('isTLDR', False)

        if not youtube_video_id:
            return jsonify({'error': 'YouTube video ID is required'}), 400

        # Initialize auth0_id as None
        auth0_id = None

        # Check for Bearer token
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
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
                logging.error(f"Error processing token: {type(e).__name__}: {str(e)}")
                # Continue execution to check for visitor_id

        # Verify we have either auth0_id or visitor_id
        if not auth0_id and not visitor_id:
            return jsonify({'error': 'Authentication required'}), 401

        # Check for existing feedback in database
        conn = get_db_connection()
        with conn.cursor() as cur:
            if auth0_id:
                cur.execute("""
                    SELECT was_helpful
                    FROM user_feedback
                    WHERE auth0_id = %s
                    AND youtube_video_id = %s
                    AND is_tldr = %s
                    LIMIT 1
                """, (auth0_id, youtube_video_id, is_tldr))
            else:
                cur.execute("""
                    SELECT was_helpful
                    FROM user_feedback
                    WHERE visitor_id = %s
                    AND youtube_video_id = %s
                    AND is_tldr = %s
                    LIMIT 1
                """, (visitor_id, youtube_video_id, is_tldr))

            result = cur.fetchone()
            
            return jsonify({
                'has_feedback': bool(result),
                'was_helpful': result[0] if result else None
            }), 200

    except Exception as e:
        logging.error(f"Error in check_feedback: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
