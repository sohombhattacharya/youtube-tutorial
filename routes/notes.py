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
import psycopg2
import psycopg2.extras
from services.youtube_service import transcribe_youtube_video, generate_tldr
from services.auth_service import auth0_validator, AUTH0_DOMAIN, AUTH0_AUDIENCE
from services.database import get_db_connection
from authlib.jose.errors import JoseError  # For JWT error handling

notes_bp = Blueprint('notes', __name__)

# Import your note generation functions here
# from services.note_service import generate_tutorial, generate_tldr, etc.

@notes_bp.route('/generate_tutorial', methods=['POST'])
def generate_tutorial_endpoint():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")

    subscription_status = 'INACTIVE'  # Default status
    auth0_id = None
    user_id = None
    
    # Process Bearer token if present
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            # Use auth_service from current_app
            decoded_token = jwt.decode(
                token,
                auth0_validator.public_key,
                claims_options={
                    "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                    "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )

            auth0_id = decoded_token['sub']

            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, subscription_status FROM users WHERE auth0_id = %s",
                    (auth0_id,)
                )
                result = cur.fetchone()
                if result:
                    user_id = result[0]
                    subscription_status = result[1]
                    
        except Exception as e:
            logging.error(f"Error processing token: {type(e).__name__}: {str(e)}")
            # Continue execution with default INACTIVE status

    if auth0_id is None:
        return jsonify({'error': 'Authentication required'}), 401

    # Continue with the rest of the endpoint logic
    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /generate_tutorial with video_url: {video_url}, user_id: {auth0_id}")
        
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)

    # Check note access only if user is not ACTIVE
    if subscription_status != 'ACTIVE':
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Check if user has already viewed this note
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM user_notes WHERE user_id = %s AND youtube_video_url LIKE %s)",
                    (user_id, f'%{video_id}%')
                )
                has_viewed = cur.fetchone()[0]
                
                if not has_viewed:
                    # If they haven't viewed it before, check their total note count
                    cur.execute("SELECT COUNT(*) FROM user_notes WHERE user_id = %s", (user_id,))
                    note_count = cur.fetchone()[0]
                    
                    if note_count >= 3:
                        return jsonify({
                            'error': 'Free note limit reached',
                            'message': 'You have reached the maximum number of free notes. Please subscribe for unlimited access.'
                        }), 403

        except Exception as e:
            logging.error(f"Database error checking user notes: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500

    # Create an S3 client
    s3_client = boto3.client(
        's3',
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )
    
    # Define the S3 bucket and key
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")  # Get the bucket name from environment variable
    s3_key = f"notes/{video_id}"  # Unique key for the markdown in S3
    
    try:
        # Check if the markdown already exists in S3
        try:
            s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
            tutorial = s3_response['Body'].read().decode('utf-8')  # Read the markdown content

            # Record the view only if user is not ACTIVE
            if subscription_status != 'ACTIVE':
                try:
                    # Extract title from the tutorial content (first line or first 50 chars)
                    title = tutorial.split('\n', 1)[0][:50]
                    if not title:
                        title = f"YouTube Video {video_id}"
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO user_notes (user_id, title, youtube_video_url) 
                            VALUES (%s, %s, %s) 
                            ON CONFLICT (user_id, youtube_video_url) DO NOTHING
                            """,
                            (user_id, title, video_url)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error recording user note: {str(e)}")
                    # Continue execution even if operation fails
                    pass

            return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except s3_client.exceptions.NoSuchKey:
            # If the markdown does not exist, generate it
            tutorial = transcribe_youtube_video(video_id, video_url)
            
            # log youtube url and title from tutorial 
            title = tutorial[:75]
            logging.info(f"YouTube URL: {video_url}, Title: {title}")

            # Upload the markdown to S3
            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=tutorial,
                ContentType='text/plain'
            )
            
            # Record the view only if user is not ACTIVE
            if subscription_status != 'ACTIVE':
                try:
                    # Extract title from the tutorial content (first line or first 50 chars)
                    title = tutorial.split('\n', 1)[0][:50]
                    if not title:
                        title = f"YouTube Video {video_id}"
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO user_notes (user_id, title, youtube_video_url) 
                            VALUES (%s, %s, %s) 
                            ON CONFLICT (user_id, youtube_video_url) DO NOTHING
                            """,
                            (user_id, title, video_url)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error recording user note: {str(e)}")
                    # Continue execution even if operation fails
                    pass

            return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@notes_bp.route('/get_tutorial', methods=['POST'])
def get_tutorial():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")
    
    data = request.json
    video_url = data.get('url')
    is_tldr = data.get('tldr', False)  # Flag to determine if we want TLDR
    
    subscription_status = 'INACTIVE'  # Default status
    auth0_id = None
    user_id = None
    
    # Process Bearer token if present
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )

            auth0_id = decoded_token['sub']
            
            # Get user's subscription status from database
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, subscription_status FROM users WHERE auth0_id = %s",
                    (auth0_id,)
                )
                result = cur.fetchone()
                if result:
                    user_id = result[0]
                    subscription_status = result[1]
                    
        except Exception as e:
            logging.error(f"Error processing token: {type(e).__name__}: {str(e)}")
            # Continue execution with default INACTIVE status
    
    if auth0_id is None:
        return jsonify({'error': 'Authentication required'}), 401
    
    logging.info(f"Received request at /get_tutorial with video_url: {video_url}, tldr: {is_tldr}")
        
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)

    # Check note access only if user is not ACTIVE
    if subscription_status != 'ACTIVE':
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Check if user has already viewed this note
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM user_notes WHERE user_id = %s AND youtube_video_url LIKE %s)",
                    (user_id, f'%{video_id}%')
                )
                has_viewed = cur.fetchone()[0]
                
                if not has_viewed:
                    # If they haven't viewed it before, check their total note count
                    cur.execute("SELECT COUNT(*) FROM user_notes WHERE user_id = %s", (user_id,))
                    note_count = cur.fetchone()[0]
                    
                    if note_count >= 3:
                        return jsonify({
                            'error': 'Free note limit reached',
                            'message': 'You have reached the maximum number of free notes. Please subscribe for unlimited access.'
                        }), 403

        except Exception as e:
            logging.error(f"Database error checking user notes: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500

    # Create an S3 client
    s3_client = boto3.client(
        's3',
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )
    
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
    # Use different S3 key based on whether we want TLDR or regular notes
    s3_key = f"tldr/{video_id}" if is_tldr else f"notes/{video_id}"
    
    try:
        # Check if the content exists in S3
        s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
        content = s3_response['Body'].read().decode('utf-8')

        # Record the view only if user is not ACTIVE
        if subscription_status != 'ACTIVE':
            try:
                # Extract title from the content (first line or first 50 chars)
                title = content.split('\n', 1)[0][:50]
                if not title:
                    title = f"YouTube Video {video_id}"
                
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO user_notes (user_id, title, youtube_video_url) 
                        VALUES (%s, %s, %s) 
                        ON CONFLICT (user_id, youtube_video_url) DO NOTHING
                        """,
                        (user_id, title, video_url)
                    )
                conn.commit()
            except Exception as e:
                logging.error(f"Error recording user note: {str(e)}")
                # Continue execution even if this fails

        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except s3_client.exceptions.NoSuchKey:
        return jsonify({'error': 'Content not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@notes_bp.route('/generate_tldr', methods=['POST'])
def generate_tldr_endpoint():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")

    subscription_status = 'INACTIVE'  # Default status
    auth0_id = None
    user_id = None
    
    # Process Bearer token if present
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
                token,
                auth0_validator.public_key,
                claims_options={
                    "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                    "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )

            auth0_id = decoded_token['sub']

            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, subscription_status FROM users WHERE auth0_id = %s",
                    (auth0_id,)
                )
                result = cur.fetchone()
                if result:
                    user_id = result[0]
                    subscription_status = result[1]
                    
        except Exception as e:
            logging.error(f"Error processing token: {type(e).__name__}: {str(e)}")
            # Continue execution with default INACTIVE status

    if auth0_id is None:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /generate_tldr with video_url: {video_url}")
        
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)

    # Check note access only if user is not ACTIVE
    if subscription_status != 'ACTIVE':
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Check if user has already viewed this note
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM user_notes WHERE user_id = %s AND youtube_video_url LIKE %s)",
                    (user_id, f'%{video_id}%')
                )
                has_viewed = cur.fetchone()[0]
                
                if not has_viewed:
                    # If they haven't viewed it before, check their total note count
                    cur.execute("SELECT COUNT(*) FROM user_notes WHERE user_id = %s", (user_id,))
                    note_count = cur.fetchone()[0]
                    
                    if note_count >= 3:
                        return jsonify({
                            'error': 'Free note limit reached',
                            'message': 'You have reached the maximum number of free notes. Please subscribe for unlimited access.'
                        }), 403

        except Exception as e:
            logging.error(f"Database error checking user notes: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500

    s3_client = boto3.client(
        's3',
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )
    
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
    s3_key = f"tldr/{video_id}"  # Different path for TLDRs
    
    try:
        try:
            s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
            tldr = s3_response['Body'].read().decode('utf-8')

            if subscription_status != 'ACTIVE':
                try:
                    # Extract title from the content (first line or first 50 chars)
                    title = tldr.split('\n', 1)[0][:50]
                    if not title:
                        title = f"TLDR: YouTube Video {video_id}"
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO user_notes (user_id, title, youtube_video_url) 
                            VALUES (%s, %s, %s) 
                            ON CONFLICT (user_id, youtube_video_url) DO NOTHING
                            """,
                            (user_id, title, video_url)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error recording user note: {str(e)}")
                    pass

            return tldr, 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except s3_client.exceptions.NoSuchKey:
            # Determine if running locally using the environment variable
            is_local = os.getenv('APP_ENV') == 'development'

            # Set proxies only if not running locally
            proxies = None if is_local else {
                'http': "http://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001",
                'https': "https://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001"
            }
            
            transcript_data = YouTubeTranscriptApi.get_transcript(video_id, proxies=proxies, languages=["en", "es", "fr", "de", "it", "pt", "ru", "zh", "hi", "uk", "cs", "sv"])

            for entry in transcript_data:
                entry.pop('duration', None)
                if 'start' in entry:
                    entry['start'] = int(entry['start'])
            
            tldr = generate_tldr(transcript_data, video_url)
            
            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=tldr,
                ContentType='text/plain'
            )
            
            if subscription_status != 'ACTIVE':
                try:
                    # Extract title from the content (first line or first 50 chars)
                    title = tldr.split('\n', 1)[0][:50]
                    if not title:
                        title = f"TLDR: YouTube Video {video_id}"
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO user_notes (user_id, title, youtube_video_url) 
                            VALUES (%s, %s, %s) 
                            ON CONFLICT (user_id, youtube_video_url) DO NOTHING
                            """,
                            (user_id, title, video_url)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error recording user note: {str(e)}")
                    pass

            return tldr, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@notes_bp.route('/convert_html_to_pdf', methods=['POST'])
def convert_html_to_pdf():
    data = request.json
    html_content = data.get('html')
    youtube_url = data.get('url')  # This will be None if not provided
    get_snippet_zip = data.get('get_snippet_zip', False)
    logging.info(f"Received request at /convert_html_to_pdf with video_url: {youtube_url}")
    
    if not html_content:
        return jsonify({'error': 'HTML content is required'}), 400

    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    subscription_status = 'INACTIVE'  # Default status

    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
                token,
                auth0_validator.public_key,
                claims_options={
                    "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                    "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )
            auth0_id = decoded_token['sub']

            # Get user's subscription status from database
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT subscription_status FROM users WHERE auth0_id = %s",
                    (auth0_id,)
                )
                result = cur.fetchone()
                if result:
                    subscription_status = result[0]

        except Exception as e:
            logging.error(f"Error processing token: {type(e).__name__}: {str(e)}")
            # Continue execution with default INACTIVE status

    # Prepare the HTML content with or without watermark
    updated_html_content = ""
    if get_snippet_zip:
        if subscription_status == 'ACTIVE':
            updated_html_content = f"<style>body {{ font-size: 150%; }}</style>" + \
                                html_content
        else: 
            updated_html_content = f"<style>body {{ font-size: 150%; }}</style>" + \
                        f"<p><i>Generated by swiftnotes.ai</i></p>\n" + \
                        html_content    
    else:
        if subscription_status == 'ACTIVE':
            updated_html_content = f"<style>body {{ font-size: 150%; }}</style>" + \
                                (f"<p>YouTube Link: <a href='{youtube_url}'>{youtube_url}</a></p>\n" if youtube_url else "") + \
                                html_content
        else: 
            updated_html_content = f"<style>body {{ font-size: 150%; }}</style>" + \
                    f"<p><i>Generated by <a href='https://swiftnotes.ai'>swiftnotes.ai</a></i></p>\n" + \
                    (f"<p>YouTube Link: <a href='{youtube_url}'>{youtube_url}</a></p>\n" if youtube_url else "") + \
                    html_content    

    # Create a temporary file to store the PDF
    pdf_path = '/tmp/generated_pdf.pdf'  # Use a temporary path for the PDF
    
    # Convert HTML to PDF using xhtml2pdf
    with open(pdf_path, 'w+b') as pdf_file:
        pisa_status = pisa.CreatePDF(updated_html_content, dest=pdf_file)
    
    if pisa_status.err:
        return jsonify({'error': 'Failed to create PDF'}), 500

    # Return the PDF file directly from the endpoint
    if not get_snippet_zip:
        response = send_file(pdf_path, as_attachment=True, download_name='generated_pdf.pdf', mimetype='application/pdf')
        os.remove(pdf_path)
        return response

    try:
        # Create a temporary zip file
        zip_path = '/tmp/snippets.zip'

        # Extract video ID from the URL
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', youtube_url)
        if not video_id_match:
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        video_id = video_id_match.group(1)

        # Create ZIP file
        with zipfile.ZipFile(zip_path, 'w') as zip_file:
            # Convert PDF pages to images with higher resolution
            pdf_document = fitz.open(pdf_path)
            for page_num in range(len(pdf_document)):
                page = pdf_document[page_num]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x zoom for better quality
                img_bytes = pix.tobytes("png")

                # Add PDF page image to ZIP
                zip_file.writestr(f'page_{page_num + 1}.png', img_bytes)

            # Get YouTube thumbnail
            thumbnail_url = f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
            thumbnail_response = requests.get(thumbnail_url)
            if thumbnail_response.status_code == 200:
                zip_file.writestr('thumbnail.jpg', thumbnail_response.content)

        # Return the ZIP file
        response = send_file(
            zip_path,
            as_attachment=True,
            download_name='snippets.zip',
            mimetype='application/zip'
        )

        # Clean up temporary files
        os.remove(pdf_path)
        os.remove(zip_path)

        return response

    except Exception as e:
        logging.error(f"Error generating snippets: {str(e)}")
        # Clean up temporary files in case of error
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return jsonify({'error': 'Failed to generate snippets'}), 500    

@notes_bp.route('/save_note', methods=['POST'])
def save_note():
    try:
        # Get token from Authorization header and decode it
        token = request.headers.get('Authorization').split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']

        # Get video URL from request
        data = request.json
        youtube_url = data.get('url')
        title = data.get('title')
        if not youtube_url:
            return jsonify({'error': 'YouTube URL is required'}), 400

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
            
            if user['subscription_status'] != 'ACTIVE':
                return jsonify({
                    'error': 'Subscription required',
                    'message': 'An active subscription is required to save notes'
                }), 403

            # Try to insert the note
            try:
                cur.execute("""
                    INSERT INTO user_notes (user_id, title, youtube_video_url)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, youtube_video_url) DO NOTHING
                    RETURNING created_at
                """, (user['id'], title, youtube_url))
                conn.commit()
                
                result = cur.fetchone()
                if result:
                    return jsonify({
                        'message': 'Note saved successfully',
                        'created_at': result['created_at'].isoformat()
                    }), 201
                else:
                    return jsonify({
                        'message': 'Note was already saved',
                    }), 200

            except Exception as e:
                conn.rollback()
                logging.error(f"Database error saving note: {str(e)}")
                return jsonify({'error': 'Failed to save note'}), 500

    except JoseError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in save_note: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@notes_bp.route('/is_saved', methods=['POST'])
def is_saved():
    try:
        # Get token from Authorization header and decode it
        token = request.headers.get('Authorization').split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']

        # Get video URL from request
        data = request.json
        youtube_url = data.get('url')
        if not youtube_url:
            return jsonify({'error': 'YouTube URL is required'}), 400

        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Check user's subscription status and if note is saved
            cur.execute(
                """
                SELECT u.subscription_status, 
                       EXISTS(
                           SELECT 1 
                           FROM user_notes un 
                           WHERE un.user_id = u.id 
                           AND un.youtube_video_url = %s
                       ) as note_saved
                FROM users u 
                WHERE u.auth0_id = %s
                """,
                (youtube_url, auth0_id)
            )
            
            result = cur.fetchone()
            if not result:
                return jsonify({'error': 'User not found'}), 404
            
            subscription_status, is_saved = result
            
            # Only return saved status if user has active subscription
            saved_status = is_saved if subscription_status == 'ACTIVE' else False
            
            return jsonify({
                'saved': saved_status
            }), 200

    except JoseError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in is_saved: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@notes_bp.route('/get_saved_notes', methods=['GET'])
def get_saved_notes():
    try:
        # Get token from Authorization header and decode it
        token = request.headers.get('Authorization').split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": AUTH0_AUDIENCE},
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
            
            if user['subscription_status'] != 'ACTIVE':
                return jsonify({
                    'error': 'Subscription required',
                    'message': 'An active subscription is required to access saved notes'
                }), 403

            # Base query parameters
            query_params = [user['id']]

            # Check if search query is a YouTube URL
            video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', search_query)
            
            # Modify queries based on search parameter
            if video_id_match:
                # If it's a YouTube URL, search by video ID in the youtube_video_url column
                video_id = video_id_match.group(1)
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_notes 
                    WHERE user_id = %s
                    AND youtube_video_url LIKE %s
                """
                notes_query = """
                    SELECT title, youtube_video_url, created_at
                    FROM user_notes 
                    WHERE user_id = %s
                    AND youtube_video_url LIKE %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """
                # Use % wildcards to match any YouTube URL format containing the video ID
                query_params = [user['id'], f'%{video_id}%']
            elif search_query:
                # Regular title search
                search_pattern = f'%{search_query}%'
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_notes 
                    WHERE user_id = %s
                    AND LOWER(title) LIKE LOWER(%s)
                """
                notes_query = """
                    SELECT id, title, youtube_video_url, created_at
                    FROM user_notes 
                    WHERE user_id = %s
                    AND LOWER(title) LIKE LOWER(%s)
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """
                query_params = [user['id'], search_pattern]
            else:
                # No search query
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_notes 
                    WHERE user_id = %s
                """
                notes_query = """
                    SELECT id, title, youtube_video_url, created_at
                    FROM user_notes 
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """

            # Get total count of notes
            cur.execute(count_query, query_params)
            total_notes = cur.fetchone()[0]

            # Add pagination parameters to query
            query_params.extend([per_page, offset])

            # Get paginated notes
            cur.execute(notes_query, query_params)
            
            notes = [{
                'id': note['id'],
                'title': note['title'],
                'url': note['youtube_video_url'],
                'created_at': note['created_at'].isoformat()
            } for note in cur.fetchall()]

            return jsonify({
                'notes': notes,
                'pagination': {
                    'total': total_notes,
                    'page': page,
                    'per_page': per_page,
                    'total_pages': (total_notes + per_page - 1) // per_page
                }
            }), 200

    except JoseError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in get_saved_notes: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
    
@notes_bp.route('/delete_note', methods=['POST'])
def delete_note():
    try:
        # Get token from Authorization header and decode it
        token = request.headers.get('Authorization').split(' ')[1]
        decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,
            claims_options={
                "aud": {"essential": True, "value": AUTH0_AUDIENCE},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )
        auth0_id = decoded_token['sub']

        # Get note ID from request
        data = request.json
        note_id = data.get('id')
        if not note_id:
            return jsonify({'error': 'Note ID is required'}), 400

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
            
            if user['subscription_status'] != 'ACTIVE':
                return jsonify({
                    'error': 'Subscription required',
                    'message': 'An active subscription is required to manage saved notes'
                }), 403

            # Delete the note, ensuring it belongs to the user
            try:
                cur.execute("""
                    DELETE FROM user_notes 
                    WHERE user_id = %s AND id = %s
                    RETURNING id
                """, (user['id'], note_id))
                conn.commit()
                
                deleted_note = cur.fetchone()
                if deleted_note:
                    return jsonify({
                        'message': 'Note deleted successfully'
                    }), 200
                else:
                    return jsonify({
                        'error': 'Note not found'
                    }), 404

            except Exception as e:
                conn.rollback()
                logging.error(f"Database error deleting note: {str(e)}")
                return jsonify({'error': 'Failed to delete note'}), 500

    except JoseError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in delete_note: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500