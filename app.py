import logging
import sys
import os
import stripe  # Add this import at the top with other imports
import base64

# Configure logging - must be first!
log_level = logging.DEBUG if os.getenv('APP_ENV') == 'development' else logging.INFO

# Custom filter to exclude OPTIONS and POST requests
class HTTPFilter(logging.Filter):
    def filter(self, record):
        # Check if this is a Werkzeug access log
        if 'werkzeug' in record.name.lower():
            return False  # Filter out all Werkzeug logs
        return True

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

from flask import Flask, request, jsonify, send_file, abort, g
from flask_cors import CORS
import os
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi
import re
from dotenv import load_dotenv
import tempfile
from xhtml2pdf import pisa
import boto3
from botocore.exceptions import NoCredentialsError
import time  # Import time for generating unique filenames
import uuid
import json
from authlib.oauth2.rfc7523 import JWTBearerTokenValidator
from authlib.jose.rfc7517.jwk import JsonWebKey
from psycopg2 import pool
import psycopg2.extras
import atexit
import ssl
import certifi
import requests
from authlib.integrations.flask_oauth2 import ResourceProtector
from authlib.jose import jwt  # Add this import at the top
from PIL import Image
import fitz  # PyMuPDF
import io
import zipfile



load_dotenv()
app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "https://swiftnotes.ai",
    "https://deploy-preview-1--swiftnotesai.netlify.app"
], supports_credentials=True)

# Initialize the connection pool
try:
    app.db_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,  # Adjust based on your needs
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

# Add teardown context to return connections to pool
@app.teardown_appcontext
def close_db_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        app.db_pool.putconn(db)
        g._database = None

# Helper function to get connection from pool
def get_db_connection():
    if not hasattr(g, '_database'):
        g._database = app.db_pool.getconn()
    return g._database




# Configure the Gemini API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Add these Auth0 configuration settings after app initialization
AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
if not AUTH0_DOMAIN:
    logging.error("AUTH0_DOMAIN environment variable is not set!")
    raise ValueError("AUTH0_DOMAIN must be configured")

# Replace JWKS client setup with Auth0 validator
class Auth0JWTBearerTokenValidator(JWTBearerTokenValidator):
    def __init__(self, domain, audience):
        logging.info(f"Initializing Auth0JWTBearerTokenValidator with domain: {domain} and audience: {audience}")
        issuer = f'https://{domain}/'
        jsonurl = requests.get(f'{issuer}.well-known/jwks.json')
        public_key = JsonWebKey.import_key_set(jsonurl.json())
        super().__init__(public_key, issuer=issuer, audience=audience)
        logging.info(f"Auth0JWTBearerTokenValidator initialized with domain: {domain} and audience: {audience}")
        self.claims_options = {
            "exp": {"essential": True},
            "aud": {"essential": True, "value": audience},
            "iss": {"essential": True, "value": issuer},
            "sub": {"essential": True}
        }

# Initialize the Auth0 validator
auth0_validator = Auth0JWTBearerTokenValidator(
    AUTH0_DOMAIN,
    os.getenv('AUTH0_AUDIENCE')
)

# Create ResourceProtector for route protection
require_auth = ResourceProtector()
require_auth.register_token_validator(auth0_validator)

def generate_tutorial(transcript_data, youtube_url):
    # Create a detailed prompt for the Gemini model
    prompt = (
        "# Write up Generation from YouTube Transcript\n\n"
        "## Objective\n"
        "Create a detailed, comprehensive, and engaging write up based on a provided YouTube transcript."
        "The transcript can be of various lengths. Do not ignore any information in the transcript. For example, if the transcript is longer than 2 hours, then you should continue to write the write up with the same level of detail."
        "The YouTube transcript is split into a list of dictionaries, each containing text and start time."
        "For example: {'text': 'Hello, my name is John', 'start': 100}. This means that the text 'Hello, my name is John' starts at 100 seconds into the video.\n\n"
        "The write up should be structured, informative, and easy to follow, providing readers with a clear understanding of the content discussed in the video.\n\n"
        "## Instructions\n"
        "1. **Introduction**:\n"
        "   - Begin with a brief introduction that summarizes the main topic of the video.\n"
        "   - Explain the significance of the topic and what readers can expect to learn.\n\n"
        "2. **Section Headings**:\n"
        "   - Divide the content into clear sections with descriptive headings.\n"
        "   - Each section should cover a specific aspect of the topic discussed in the transcript.\n\n"
        "   - Each section should also point out the start time of the section in the transcript. Include the start time in the section heading end as an integer in a specific format. For example: '[sec:100]'\n\n"        
        "3. **Detailed Explanations**:\n"
        "   - Provide in-depth explanations for each point made in the transcript.\n"
        "   - ALWAYS use bullet points or numbered lists to represent and separate the points. \n\n"
        "   - At the end of each point, include the start time of the point in the transcript as an integer in a specific format. For example: '[sec:100]'. NEVER include a time range. NEVER include multiple times.\n\n"        
        "4. **Conclusion**:\n"
        "   - Summarize the key takeaways from the transcript.\n"
        "   - Encourage readers to explore further or apply what they have learned.\n\n"
        "5. **Engagement**:\n"
        "   - Use a conversational tone to engage the reader.\n"
        "   - Pose questions or prompts that encourage readers to think critically about the content.\n\n"
        "Additional note: If the section heading is the title of the markdown write up then DO NOT include a start time in the section heading. For example, DO NOT do this: # Amazing AI Tools That Will Blow Your Mind [sec:0]. Instead do this: # Amazing AI Tools That Will Blow Your Mind.\n\n"        
        "## Transcript\n"
        f"{transcript_data}\n\n"
        "## Output Format\n"
        "The output should be in markdown format, properly formatted with headings, lists, and code blocks as necessary. Ensure that the write up is polished and ready for publication.\n\n"
        "Transcript:"
    )
    
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    
    # Log the title of the tutorial only if there is a response
    if response:
        # Replace [sec:XX] with just the formatted time display
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', youtube_url)
        if video_id_match:
            video_id = video_id_match.group(1)
            markdown_text = response.text
            
            # Function to replace [sec:XX] with just the time display
            def replace_sec_links(match):
                seconds = int(match.group(1))  # Get the seconds value and convert to int
                
                # Format the display of seconds
                if seconds >= 3600:  # Check if seconds is greater than or equal to an hour
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    remaining_seconds = seconds % 60
                    display_time = f"{hours}hr{minutes}m{remaining_seconds}s"
                elif seconds >= 60:  # Check if seconds is greater than or equal to 60
                    minutes = seconds // 60
                    remaining_seconds = seconds % 60
                    display_time = f"{minutes}m{remaining_seconds}s"
                else:
                    display_time = f"{seconds}s"
                
                return f'({display_time})'  # Just return the formatted time in parentheses
            
            # Use regex to find and replace all occurrences of [sec:XX]
            markdown_text = re.sub(r'\[sec:(\d+)\]', replace_sec_links, markdown_text)
            return markdown_text
    else:
        return 'No tutorial generated.'

def transcribe_youtube_video(video_id, youtube_url):
    # Determine if running locally using the environment variable
    is_local = os.getenv('APP_ENV') == 'development'

    # Set proxies only if not running locally
    proxies = None if is_local else {
        'http': "http://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001",
        'https': "https://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001"
    }
    
    # Fetch the transcript for the given video ID
    transcript_data = YouTubeTranscriptApi.get_transcript(video_id, proxies=proxies, languages=["en", "es", "fr", "de", "it", "pt", "ru", "zh", "hi", "uk", "cs", "sv"])

    for entry in transcript_data:
        entry.pop('duration', None)
        if 'start' in entry: 
            entry['start'] = int(entry['start'])    
    
    # Generate a readable tutorial from the transcript
    tutorial = generate_tutorial(transcript_data, youtube_url)
    
    return tutorial

@app.route('/generate_tutorial', methods=['POST'])
def generate_tutorial_endpoint():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")
    visitor_id = request.json.get('visitor_id')

    subscription_status = 'INACTIVE'  # Default status
    
    # Process Bearer token if present
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            decoded_token = jwt.decode(
            token,
            auth0_validator.public_key,  # Use the public key from your validator
            claims_options={
                "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
                "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
                }
            )

            auth0_id = decoded_token['sub']

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

    # Continue with the rest of the endpoint logic
    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /generate_tutorial with video_url: {video_url}")
        
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)

    # Check note access only if user is not ACTIVE
    if subscription_status != 'ACTIVE' and visitor_id:
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Check if visitor has already viewed this note
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM visitor_notes WHERE visitor_id = %s AND youtube_video_id = %s)",
                    (visitor_id, video_id)
                )
                has_viewed = cur.fetchone()[0]
                
                if not has_viewed:
                    # If they haven't viewed it before, check their total note count
                    cur.execute("SELECT COUNT(*) FROM visitor_notes WHERE visitor_id = %s", (visitor_id,))
                    note_count = cur.fetchone()[0]
                    
                    if note_count >= 3:
                        return jsonify({
                            'error': 'Free note limit reached',
                            'message': 'You have reached the maximum number of free notes. Please sign up for unlimited access.'
                        }), 403

        except Exception as e:
            logging.error(f"Database error checking visitor notes: {str(e)}")
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

            # Record the view only if user is not ACTIVE and visitor_id is provided
            if subscription_status != 'ACTIVE' and visitor_id:
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO visitor_notes (visitor_id, youtube_video_id) 
                            VALUES (%s, %s) 
                            ON CONFLICT (visitor_id, youtube_video_id) DO NOTHING
                            """,
                            (visitor_id, video_id)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error handling visitor note: {str(e)}")
                    # Continue execution even if operation fails

            return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except s3_client.exceptions.NoSuchKey:
            # If the markdown does not exist, generate it
            tutorial = transcribe_youtube_video(video_id, video_url)
            

            # log youtube url, visitor id, and title from tutorial 
            title = tutorial[:75]
            logging.info(f"YouTube URL: {video_url}, Visitor ID: {visitor_id}, Title: {title}")

            # Upload the markdown to S3
            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=tutorial,
                ContentType='text/plain'
            )
            
            # Record the view only if user is not ACTIVE and visitor_id is provided
            if subscription_status != 'ACTIVE' and visitor_id:
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO visitor_notes (visitor_id, youtube_video_id) 
                            VALUES (%s, %s) 
                            ON CONFLICT (visitor_id, youtube_video_id) DO NOTHING
                            """,
                            (visitor_id, video_id)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error handling visitor note: {str(e)}")
                    # Continue execution even if operation fails

            return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/convert_html_to_pdf', methods=['POST'])
def convert_html_to_pdf():
    data = request.json
    html_content = data.get('html')
    youtube_url = data.get('url')
    get_snippet_zip = data.get('get_snippet_zip', False)
    logging.info(f"Received request at /convert_html_to_pdf with video_url: {youtube_url}, get_snippet_zip: {get_snippet_zip}")
    
    if not html_content:
        return jsonify({'error': 'HTML content is required'}), 400
    
    # If generating snippets, remove all timestamp references and hyperlinks
    if get_snippet_zip:
        # Remove timestamp hyperlinks first (any <a> tag containing a timestamp pattern)
        html_content = re.sub(r'\s*<a[^>]*>\s*\d+(?:hr)?\d*m\d*s(?:\d*s)?(?=[\s<]|$)\s*</a>\s*', '', html_content)
        # Then remove any remaining timestamp text, including from titles/headings
        html_content = re.sub(r'\s*\(\d+(?:hr)?\d*m\d*s(?:\d*s)?\)\s*', '', html_content)  # Remove timestamps in parentheses
        html_content = re.sub(r'\s*\d+(?:hr)?\d*m\d*s(?:\d*s)?(?=[\s<]|$)\s*', '', html_content)  # Remove any other timestamps
        html_content = re.sub(r'\s*0s\s*', '', html_content)  # Remove bare 0s

        # Clean up any potential double spaces left behind
        html_content = re.sub(r'\s+', ' ', html_content)
        logging.debug(f"HTML content after timestamp removal: {html_content[:200]}...")  # Log for verification
    
    # Add CSS and header after timestamp removal
    html_content = f"""<style>
        body {{ font-size: 150%; }}
        a {{ color: black; text-decoration: none; }}
    </style>
    <div style="margin-top: 40px;">""" + html_content + "</div>"

    # Create a temporary HTML file first
    html_path = '/tmp/temp.html'
    with open(html_path, 'w', encoding='utf-8') as html_file:
        html_file.write(html_content)
    
    # Create a temporary file to store the PDF
    pdf_path = '/tmp/generated_pdf.pdf'
    
    # Convert HTML to PDF using xhtml2pdf
    with open(html_path, 'rb') as html_file:
        with open(pdf_path, 'w+b') as pdf_file:
            pisa_status = pisa.CreatePDF(html_file, dest=pdf_file)
    
    if pisa_status.err:
        return jsonify({'error': 'Failed to create PDF'}), 500

    # Clean up HTML file
    os.remove(html_path)

    if not get_snippet_zip:
        # Return the PDF file directly if not requesting snippets
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

@app.route('/get_tutorial', methods=['POST'])
def get_tutorial():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")
    
    data = request.json
    video_url = data.get('url')
    visitor_id = data.get('visitor_id')  # This will always be present
    is_tldr = data.get('tldr', False)  # New flag to determine if we want TLDR
    
    subscription_status = 'INACTIVE'  # Default status
    
    # Process Bearer token if present
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
    
    logging.info(f"Received request at /get_tutorial with video_url: {video_url}, tldr: {is_tldr}")
        
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)

    # Check note access only if user is not ACTIVE
    if subscription_status != 'ACTIVE' and visitor_id:
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Check if visitor has already viewed this note
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM visitor_notes WHERE visitor_id = %s AND youtube_video_id = %s)",
                    (visitor_id, video_id)
                )
                has_viewed = cur.fetchone()[0]
                
                if not has_viewed:
                    # If they haven't viewed it before, check their total note count
                    cur.execute("SELECT COUNT(*) FROM visitor_notes WHERE visitor_id = %s", (visitor_id,))
                    note_count = cur.fetchone()[0]
                    
                    if note_count >= 3:
                        return jsonify({
                            'error': 'Free note limit reached',
                            'message': 'You have reached the maximum number of free notes. Please sign up for unlimited access.'
                        }), 403

        except Exception as e:
            logging.error(f"Database error checking visitor notes: {str(e)}")
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
        if subscription_status != 'ACTIVE' and visitor_id:
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO visitor_notes (visitor_id, youtube_video_id) 
                        VALUES (%s, %s) 
                        ON CONFLICT (visitor_id, youtube_video_id) DO NOTHING
                        """,
                        (visitor_id, video_id)
                    )
                conn.commit()
            except Exception as e:
                logging.error(f"Error recording visitor note: {str(e)}")
                # Continue execution even if this fails

        return content, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except s3_client.exceptions.NoSuchKey:
        return jsonify({'error': 'Content not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/generate_quiz', methods=['POST'])
def generate_quiz():
    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /generate_quiz with video_url: {video_url}")
    
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)  # Get the video ID
    
    # Create an S3 client
    s3_client = boto3.client(
        's3',
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )
    
    # Define the S3 bucket and keys
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")  # Get the bucket name from environment variable
    quiz_s3_key = f"quiz/{video_id}.json"  # Unique key for the quiz in S3
    markdown_s3_key = f"notes/{video_id}"  # Key for the markdown content in S3
    
    try:
        # Check if the quiz already exists in S3
        s3_response = s3_client.get_object(Bucket=bucket_name, Key=quiz_s3_key)
        existing_quiz = s3_response['Body'].read().decode('utf-8')  # Read the existing quiz content
        return jsonify({'quiz': json.loads(existing_quiz)}), 200  # Return the existing quiz
    except s3_client.exceptions.NoSuchKey:
        # If the quiz does not exist, proceed to get the markdown content
        try:
            s3_response = s3_client.get_object(Bucket=bucket_name, Key=markdown_s3_key)
            markdown_content = s3_response['Body'].read().decode('utf-8')  # Read the markdown content
        except s3_client.exceptions.NoSuchKey:
            return jsonify({'error': 'Markdown tutorial not found'}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    # Generate quiz using Gemini
    prompt = (
        "Create a 10-question quiz based on the following markdown content. "
        "The questions should progressively increase in difficulty from easy to hard. "
        "Return the quiz in a valid JSON object with the following example structure for 5 questions. The JSON object should NOT be wrapped in code blocks ``` ```. \n"
        "{\n"
        '  "quiz": {\n'
        '    "title": "Understanding NFL Sunday Ticket",\n'
        '    "description": "A quiz to test your knowledge about the NFL Sunday Ticket and its features.",\n'
        '    "questions": [\n'
        '      {\n'
        '        "question": "Which of the following best describes the primary benefit of NFL Sunday Ticket?",\n'
        '        "options": ["Access to all NFL games regardless of location", "Exclusive behind-the-scenes content", "Discounted merchandise for subscribers", "Access to NFL Network programming"],\n'
        '        "correctAnswer": "Access to all NFL games regardless of location",\n'
        '        "explanation": "NFL Sunday Ticket allows subscribers to watch every out-of-market NFL game live, which is its primary benefit." \n'
        '      },\n'
        '      {\n'
        '        "question": "Which platforms can you use to stream NFL Sunday Ticket?",\n'
        '        "options": ["Only on TV", "Mobile devices and computers", "Only on gaming consoles", "Smart TVs only"],\n'
        '        "correctAnswer": "Mobile devices and computers",\n'
        '        "explanation": "NFL Sunday Ticket can be streamed on various platforms, including mobile devices and computers." \n'
        '      },\n'
        '      {\n'
        '        "question": "What is the typical cost range for the NFL Sunday Ticket subscription for the 2023 season?",\n'
        '        "options": ["$99 to $199", "$199 to $299", "$299 to $399", "$399 to $499"],\n'
        '        "correctAnswer": "$299 to $399",\n'
        '        "explanation": "The cost for the NFL Sunday Ticket subscription typically ranges from $299 to $399 for the season." \n'
        '      },\n'
        '      {\n'
        '        "question": "Which feature allows you to watch multiple games at once on NFL Sunday Ticket?",\n'
        '        "options": ["Game Mix", "Multi-View", "Red Zone Channel", "Picture-in-Picture"],\n'
        '        "correctAnswer": "Multi-View",\n'
        '        "explanation": "The Multi-View feature allows subscribers to watch multiple games simultaneously." \n'
        '      },\n'
        '      {\n'
        '        "question": "During the playoffs, what unique advantage does NFL Sunday Ticket provide to its subscribers?",\n'
        '        "options": ["Access to all playoff games live", "Exclusive interviews with players", "Enhanced graphics and analytics", "Discounted merchandise"],\n'
        '        "correctAnswer": "Access to all playoff games live",\n'
        '        "explanation": "NFL Sunday Ticket provides access to all playoff games live, which is a significant advantage during the playoffs." \n'
        '      }\n'
        '    ]\n'
        '  }\n'
        "}\n"
        "Ensure the questions are challenging and require critical thinking. "
        "The options should be plausible and similar in nature to make it difficult to identify the correct answer. "
        "The correctAnswer should be one of the options, and provide an explanation for each correct answer. "
        "Encourage the model to use nuanced language and scenarios related to the NFL Sunday Ticket to create engaging questions. "
        f"Markdown Content:\n{markdown_content}"
    )
    
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    
    if response and response.text:
        try:
            quiz_data = json.loads(response.text)  # Parse the response text to JSON
            
            # Upload the quiz JSON to S3
            s3_client.put_object(
                Bucket=bucket_name,
                Key=quiz_s3_key,  # Use the defined key for the quiz
                Body=json.dumps(quiz_data),  # Convert the quiz data to JSON string
                ContentType='application/json'
            )
            
            return jsonify({'quiz': quiz_data}), 200  # Return the JSON response
        except json.JSONDecodeError as e:
            logging.error(f"JSON decode error: {str(e)} - Response: {response.text}")  # Log the error and response
            return jsonify({'error': 'Failed to parse quiz as JSON'}), 500
        except Exception as e:
            logging.error(f"Error uploading quiz to S3: {str(e)}")  # Log any S3 upload errors
            return jsonify({'error': 'Failed to upload quiz to S3'}), 500
    else:
        return jsonify({'error': 'Failed to generate quiz'}), 500

@app.route('/get_visitor_notes', methods=['POST'])
def get_visitor_notes():
    data = request.json
    visitor_id = data.get('visitor_id')
    
    if not visitor_id:
        return jsonify({'error': 'Visitor ID is required'}), 400
        
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Count existing notes for this visitor
            cur.execute("SELECT COUNT(*) FROM visitor_notes WHERE visitor_id = %s", (visitor_id,))
            used_notes = cur.fetchone()[0]

            
            return jsonify({
                'used_notes': used_notes,
                'total_free_notes': 3
            }), 200

    except Exception as e:
        logging.error(f"Database error checking visitor notes: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

# Add Stripe configuration after other configurations
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
stripe_endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.data.decode("utf-8")
    signature = request.headers.get('Stripe-Signature')
    webhook_log_id = None  
    
    try:
        # Verify Stripe signature
        event = stripe.Webhook.construct_event(payload, signature, stripe_endpoint_secret)
        
        # Extract customer ID from the event
        customer_id = event.data.object.customer

        # Log the webhook event with customer ID
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO webhook_logs 
                (stripe_event_id, event_type, event_data, stripe_customer_id, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                RETURNING id
            """, (
                event.id,
                event.type,
                json.dumps(event.data.object),
                customer_id
            ))
            webhook_log_id = cur.fetchone()[0]
        conn.commit()
        
        # Process the event with retries
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                if event.type == 'customer.subscription.created':
                    subscription = event.data.object
                    email = stripe.Customer.retrieve(subscription.customer).email
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE users 
                            SET subscription_status = 'ACTIVE',
                                subscription_id = %s,
                                stripe_customer_id = %s,
                                updated_at = NOW()
                            WHERE email = %s
                        """, (subscription.id, subscription.customer, email))
                        
                        # Update webhook log with processing status
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Subscription activated',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"New subscription created for customer {subscription.customer}")
                    
                elif event.type == 'invoice.paid':
                    invoice = event.data.object
                    subscription = stripe.Subscription.retrieve(invoice.subscription)
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Payment confirmed and subscription extended',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"Payment confirmed for customer {invoice.customer}")
                    
                elif event.type == 'customer.subscription.updated':
                    subscription = event.data.object
                    
                    if subscription.cancel_at_period_end == False:
                        # Handle subscription renewal (existing code)
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE users 
                                SET subscription_status = 'ACTIVE',
                                    subscription_cancelled_at = NULL,
                                    subscription_cancelled_period_ends_at = NULL,
                                    updated_at = NOW()
                                WHERE stripe_customer_id = %s
                                  AND subscription_cancelled_at IS NOT NULL
                            """, (subscription.customer,))
                            
                            if cur.rowcount > 0:
                                cur.execute("""
                                    UPDATE webhook_logs 
                                    SET processing_status = 'success',
                                        processing_details = 'Subscription renewed',
                                        processed_at = NOW()
                                    WHERE id = %s
                                """, (webhook_log_id,))
                                logging.info(f"Subscription renewed for customer {subscription.customer}")
                    
                    elif subscription.cancel_at_period_end == True:
                        # Handle subscription cancellation
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE users 
                                SET subscription_cancelled_at = NOW(),
                                    subscription_cancelled_period_ends_at = to_timestamp(%s),
                                    updated_at = NOW()
                                WHERE stripe_customer_id = %s
                            """, (subscription.current_period_end, subscription.customer))
                            
                            cur.execute("""
                                UPDATE webhook_logs 
                                SET processing_status = 'success',
                                    processing_details = 'Subscription cancelled (will end at period end)',
                                    processed_at = NOW()
                                WHERE id = %s
                            """, (webhook_log_id,))
                            logging.info(f"Subscription cancelled (pending end of period) for customer {subscription.customer}")
                    
                    conn.commit()

                elif event.type == 'invoice.payment_failed':
                    invoice = event.data.object
                    attempt_count = invoice.attempt_count
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        # After 3 failed attempts, mark subscription as past_due
                        new_status = 'INACTIVE' if attempt_count >= 3 else 'ACTIVE'
                        cur.execute("""
                            UPDATE users 
                            SET subscription_status = %s,
                                updated_at = NOW()
                            WHERE stripe_customer_id = %s
                        """, (new_status, invoice.customer))
                        
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = %s,
                                processed_at = NOW()
                            WHERE id = %s
                        """, (f"Payment failed (attempt {attempt_count})", webhook_log_id))
                    conn.commit()
                    
                    # TODO: Send email notification about failed payment
                    logging.error(f"Payment failed for customer {invoice.customer} (attempt {attempt_count})")
                    
                elif event.type == 'customer.subscription.deleted':
                    subscription = event.data.object
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE users 
                            SET subscription_status = 'INACTIVE',
                            WHERE stripe_customer_id = %s
                        """, (subscription.customer,))
                        
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Subscription cancelled and terminated',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"Subscription terminated for customer {subscription.customer}")
                
                # If we get here, processing succeeded
                break
                
            except Exception as e:
                retry_count += 1
                error_msg = str(e)
                
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = %s,
                                processing_details = %s,
                                processed_at = NOW()
                            WHERE id = %s
                        """, (
                            'error' if retry_count == max_retries else 'retrying',
                            f"Error: {error_msg} (attempt {retry_count}/{max_retries})",
                            webhook_log_id
                        ))
                    conn.commit()
                except Exception as log_error:
                    logging.error(f"Failed to update webhook log: {str(log_error)}")
                
                if retry_count == max_retries:
                    logging.error(f"Failed to process webhook after {max_retries} attempts: {error_msg}")
                    return jsonify({'error': 'Processing failed'}), 500
                    
                time.sleep(2 ** retry_count)
                continue
        
        return jsonify({'message': 'Webhook processed'}), 200
        
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        error_msg = str(e)
        logging.error(f"Webhook verification failed: {error_msg}")
        
        # Log verification failure
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO webhook_logs 
                    (event_type, processing_status, processing_details, created_at, processed_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                """, (
                    'verification_failed',
                    'error',
                    f"Verification error: {error_msg}"
                ))
            conn.commit()
        except Exception as log_error:
            logging.error(f"Failed to log webhook verification error: {str(log_error)}")
            
        return jsonify({'error': error_msg}), 400

@app.route('/get_user', methods=['GET'])
@require_auth(None)
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

@app.route('/cancel_subscription', methods=['POST'])
def cancel_subscription():
    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'No authentication token provided'}), 401

    token = auth_header.split(' ')[1]
    max_retries = 3
    base_delay = 1  # Base delay in seconds

    # Function to handle retries with exponential backoff
    def retry_operation(operation, *args, **kwargs):
        for attempt in range(max_retries):
            try:
                return operation(*args, **kwargs)
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    raise  # Re-raise the last exception
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.warning(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {delay} seconds...")
                time.sleep(delay)

    try:
        # Verify token and get user info
        def verify_token():
            claims = auth0_validator.validate_token(token, scopes=None, request=None)
            auth0_id = claims['sub']
            return auth0_id

        try:
            auth0_id = retry_operation(verify_token)
        except jwt.InvalidTokenError as e:
            logging.error(f"Invalid JWT token: {str(e)}")
            return jsonify({'error': 'Invalid authentication token'}), 401
        except Exception as e:
            logging.error(f"Error verifying token: {type(e).__name__}: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401

        # Get user's subscription info from database
        def get_user_subscription():
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT subscription_id, stripe_customer_id
                    FROM users 
                    WHERE auth0_id = %s
                """, (auth0_id,))
                return cur.fetchone()

        try:
            user = retry_operation(get_user_subscription)
            if not user or not user['subscription_id']:
                return jsonify({'error': 'No active subscription found'}), 404

        except Exception as e:
            logging.error(f"Database error getting user subscription: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500

        # Cancel the subscription with Stripe
        def cancel_stripe_subscription():
            return stripe.Subscription.modify(
                user['subscription_id'],
                cancel_at_period_end=True
            )

        try:
            subscription = retry_operation(cancel_stripe_subscription)
        except stripe.error.StripeError as e:
            logging.error(f"Stripe error: {str(e)}")
            return jsonify({'error': 'Failed to cancel subscription'}), 500

        # Update database with cancellation info
        def update_user_cancellation():
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users 
                    SET subscription_cancelled_at = NOW(),
                        subscription_cancelled_period_ends_at = to_timestamp(%s)
                    WHERE auth0_id = %s
                """, (subscription.current_period_end, auth0_id))
                conn.commit()

        try:
            retry_operation(update_user_cancellation)
        except Exception as e:
            logging.error(f"Database error updating cancellation info: {str(e)}")
            # Note: Subscription is already cancelled in Stripe at this point
            return jsonify({'error': 'Subscription cancelled but failed to update database'}), 500

        return jsonify({
            'message': 'Subscription will be canceled at the end of the billing period',
            'current_period_end': subscription.current_period_end,
        }), 200

    except Exception as e:
        logging.error(f"Unexpected error in cancel_subscription: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/manage_sub', methods=['POST'])
def manage_subscription():
    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'No authentication token provided'}), 401

    token = auth_header.split(' ')[1]
    try:
        decoded_token = jwt.decode(
        token,
        auth0_validator.public_key,  # Use the public key from your validator
        claims_options={
            "aud": {"essential": True, "value": os.getenv('AUTH0_AUDIENCE')},
            "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )

        auth0_id = decoded_token['sub']

        # Get user's Stripe customer ID from database
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT stripe_customer_id
                FROM users 
                WHERE auth0_id = %s
            """, (auth0_id,))
            user = cur.fetchone()

            if not user or not user['stripe_customer_id']:
                return jsonify({'error': 'No Stripe customer found'}), 404
            
            try:
                # Create Stripe billing portal session
                session = stripe.billing_portal.Session.create(
                    customer=user['stripe_customer_id'],
                )
                return jsonify({'url': session.url}), 200

            except stripe.error.StripeError as e:
                logging.error(f"Stripe error creating portal session: {str(e)}")
                return jsonify({'error': 'Failed to create management session'}), 500

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in manage_subscription: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/save_note', methods=['POST'])
@require_auth(None)  # Use the existing auth decorator
def save_note():
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

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in save_note: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/is_saved', methods=['POST'])
@require_auth(None)  # Use the existing auth decorator
def is_saved():
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

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in is_saved: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/get_saved_notes', methods=['GET'])
@require_auth(None)
def get_saved_notes():
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
            
            if user['subscription_status'] != 'ACTIVE':
                return jsonify({
                    'error': 'Subscription required',
                    'message': 'An active subscription is required to access saved notes'
                }), 403

            # Base query parameters
            query_params = [user['id']]

            # Modify queries based on search parameter
            if search_query:
                search_pattern = f'%{search_query}%'
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_notes 
                    WHERE user_id = %s
                    AND LOWER(title) LIKE LOWER(%s)
                """
                notes_query = """
                    SELECT title, youtube_video_url, created_at
                    FROM user_notes 
                    WHERE user_id = %s
                    AND LOWER(title) LIKE LOWER(%s)
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """
                query_params = [user['id'], search_pattern]
            else:
                count_query = """
                    SELECT COUNT(*) 
                    FROM user_notes 
                    WHERE user_id = %s
                """
                notes_query = """
                    SELECT title, youtube_video_url, created_at
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

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in get_saved_notes: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

def generate_tldr(transcript_data, youtube_url):
    # Create a detailed prompt for the Gemini model
    prompt = (
        "# TLDR Generation from YouTube Transcript\n\n"
        "## Objective\n"
        "Create a highly informative, concise, clear TLDR (Too Long; Didn't Read) summary based on a provided YouTube transcript. "
        "The transcript can be of various lengths. Please do not ignore any information in the transcript. For example, if the transcript is longer than 1 hour, then you should gather information from the entire transcript and write up a TLDR of the entire video."
        "The YouTube transcript is split into a list of dictionaries, each containing text and start time."
        "For example: {'text': 'Hello, my name is John', 'start': 100}. This means that the text 'Hello, my name is John' starts at 100 seconds into the video.\n\n"        
        "The summary should be brief but capture all important points from the entire transcript. Do not ignore any information in the transcript. Each bullet point should be unique from the other bullet points. For example, do not have bullet points that are close in time to each other. \n\n"
        "## Instructions\n"
        "1. **Format**:\n"
        "   - Start with a title of what the video is about.\n"
        "   - Then follow up with a one-sentence overview of what the video is about.\n"
        "   - Follow with at least 3-5 key bullet points that capture the main takeaways. If you find that the video is too short, then you can reduce the number of bullet points to 3. If you find that the video has more than 5 key takeways include them all.\n"
        "   - Each bullet point should be clear and concise (1-2 sentences max). \n\n"
        "   - Each bullet point should start with the topic in bold. \n\n"
        "   - Each bullet point must be unique and should not overlap with the other bullets, even if the topics are discussed at different parts of the video. For example, if a topic is discussed repeatedly, only one bullet point should mention the overall point about the topic, and not each of the times it was mentioned in the video. Aim to use information from the whole video.\n\n"
        "   - Pay special attention to information that seems to be emphasized by the speaker or returned to multiple times.\n\n"
        "   - Interpret the meaning and significance of each point. Don't just summarize what was said, but also what impact it has on the overall message.\n\n"
        "   - At the end include a 1-2 sentence conclusion that summarizes the main takeaways and what the video is about.\n\n"
        "2. **Time Stamps**:\n"
        "   - Each bullet point should also point out the start time of the section in the transcript. Include the start time in the section heading end as an integer in a specific format. For example: '[sec:100]'\n\n"        
        "3. **Length**:\n"
        "   - The entire TLDR should be no more than 200 words.\n"
        f"## Transcript\n{transcript_data}\n\n"
        "## Output Format\n"
        "The output should be in markdown format with a brief overview followed by bullet points.\n\n"
        "Transcript:"
    )

    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    
    # Log the title of the TLDR only if there is a response
    if response:
        title = response.text[:75]  
        logging.info(f"TLDR generated for {youtube_url}, {title}")

        # Replace [sec:XX] with just the formatted time display
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', youtube_url)
        if video_id_match:
            video_id = video_id_match.group(1)
            markdown_text = response.text
            
            # Function to replace [sec:XX] with just the time display
            def replace_sec_links(match):
                seconds = int(match.group(1))
                
                if seconds >= 3600:
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    remaining_seconds = seconds % 60
                    display_time = f"{hours}hr{minutes}m{remaining_seconds}s"
                elif seconds >= 60:
                    minutes = seconds // 60
                    remaining_seconds = seconds % 60
                    display_time = f"{minutes}m{remaining_seconds}s"
                else:
                    display_time = f"{seconds}s"
                
                return f'({display_time})'  # Just return the formatted time in parentheses
            
            markdown_text = re.sub(r'\[sec:(\d+)\]', replace_sec_links, markdown_text)
            return markdown_text
    else:
        return 'No TLDR generated.'

@app.route('/generate_tldr', methods=['POST'])
def generate_tldr_endpoint():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")
    visitor_id = request.json.get('visitor_id')

    subscription_status = 'INACTIVE'  # Default status
    
    # Process Bearer token if present
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

    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /generate_tldr with video_url: {video_url}")
        
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)

    # Check note access only if user is not ACTIVE
    if subscription_status != 'ACTIVE' and visitor_id:
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM visitor_notes WHERE visitor_id = %s AND youtube_video_id = %s)",
                    (visitor_id, video_id)
                )
                has_viewed = cur.fetchone()[0]
                
                if not has_viewed:
                    cur.execute("SELECT COUNT(*) FROM visitor_notes WHERE visitor_id = %s", (visitor_id,))
                    note_count = cur.fetchone()[0]
                    
                    if note_count >= 3:
                        return jsonify({
                            'error': 'Free note limit reached',
                            'message': 'You have reached the maximum number of free notes. Please sign up for unlimited access.'
                        }), 403

        except Exception as e:
            logging.error(f"Database error checking visitor notes: {str(e)}")
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

            if subscription_status != 'ACTIVE' and visitor_id:
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO visitor_notes (visitor_id, youtube_video_id) 
                            VALUES (%s, %s) 
                            ON CONFLICT (visitor_id, youtube_video_id) DO NOTHING
                            """,
                            (visitor_id, video_id)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error handling visitor note: {str(e)}")
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
            
            if subscription_status != 'ACTIVE' and visitor_id:
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO visitor_notes (visitor_id, youtube_video_id) 
                            VALUES (%s, %s) 
                            ON CONFLICT (visitor_id, youtube_video_id) DO NOTHING
                            """,
                            (visitor_id, video_id)
                        )
                    conn.commit()
                except Exception as e:
                    logging.error(f"Error handling visitor note: {str(e)}")
                    pass

            return tldr, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
