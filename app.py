import logging
import sys
import os
import stripe  # Add this import at the top with other imports

# Configure logging - must be first!
log_level = logging.DEBUG if os.getenv('APP_ENV') == 'development' else logging.INFO

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # Explicitly use stdout
    ],
    force=True  # Force override any existing configuration
)

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
import jwt
from jwt import PyJWKClient  # Add this import
from psycopg2 import pool
import psycopg2.extras
import atexit
import ssl
import certifi

load_dotenv()
app = Flask(__name__)
CORS(app, resources={
    r"/*": {
        "origins": [
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "https://swiftnotes.ai"
        ]
    }
})

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

AUTH0_ALGORITHMS = ['RS256']
jwks_url = f'https://{AUTH0_DOMAIN}/.well-known/jwks.json'
logging.info(f"Auth0 Domain: {AUTH0_DOMAIN}")
logging.info(f"JWKS URL: {jwks_url}")

try:
    # Create JWKS client with SSL context
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    jwks_client = PyJWKClient(jwks_url, ssl_context=ssl_context)
    logging.info("Successfully initialized JWKS client")
except Exception as e:
    logging.error(f"Failed to initialize JWKS client: {type(e).__name__}: {str(e)}")
    raise

def validate_token(token):
    return token == os.getenv("API_KEY")

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
        title = response.text[:75]  
        logging.info(f"{youtube_url}, {title}")  # Log the title

        # Replace [sec:XX] with hyperlinks
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', youtube_url)
        if video_id_match:
            video_id = video_id_match.group(1)
            markdown_text = response.text
            
            # Function to replace [sec:XX] with markdown hyperlinks
            def replace_sec_links(match):
                seconds = int(match.group(1))  # Get the seconds value and convert to int
                
                # Format the display of seconds
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
                
                return f'[{display_time}](https://youtu.be/{video_id}?t={seconds})'  
            
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
            # Verify token and get user's subscription status
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            decoded_token = jwt.decode(
                token,
                signing_key.key,
                algorithms=AUTH0_ALGORITHMS,
                audience=os.getenv('AUTH0_AUDIENCE'),
                issuer=f'https://{AUTH0_DOMAIN}/'
            )
            
            # Get user's subscription status from database
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
                    
                    if note_count >= 7:
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
                    pass

            return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except s3_client.exceptions.NoSuchKey:
            # If the markdown does not exist, generate it
            tutorial = transcribe_youtube_video(video_id, video_url)
            
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
                    pass

            return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/convert_html_to_pdf', methods=['POST'])
def convert_html_to_pdf():
    data = request.json
    html_content = data.get('html')
    youtube_url = data.get('url')  # Get the YouTube URL from the request
    logging.info(f"Received request at /convert_html_to_pdf with video_url: {youtube_url}")
    
    if not html_content:
        return jsonify({'error': 'HTML content is required'}), 400
    
    # Add CSS to increase font size and the generated line with hyperlinks at the top of the HTML content
    html_content = f"<style>body {{ font-size: 150%; }}</style>" + \
                   f"<p>Generated by <a href='https://swiftnotes.ai'>swiftnotes.ai</a></p>\n" + \
                   (f"<p>YouTube Link: <a href='{youtube_url}'>{youtube_url}</a></p>\n" if youtube_url else "") + \
                   html_content
    
    # Create a temporary file to store the PDF
    pdf_path = '/tmp/generated_pdf.pdf'  # Use a temporary path for the PDF
    
    # Convert HTML to PDF using xhtml2pdf
    with open(pdf_path, 'w+b') as pdf_file:
        pisa_status = pisa.CreatePDF(html_content, dest=pdf_file)
    
    if pisa_status.err:
        return jsonify({'error': 'Failed to create PDF'}), 500

    # Return the PDF file directly from the endpoint
    response = send_file(pdf_path, as_attachment=True, download_name='generated_pdf.pdf', mimetype='application/pdf')
    
    # Remove the temporary file after sending the response
    os.remove(pdf_path)
    
    return response

@app.route('/get_tutorial', methods=['POST'])
def get_tutorial():
    # Check for Bearer token
    logging.debug(f"Request headers: {request.headers}")
    auth_header = request.headers.get('Authorization')
    logging.debug(f"Authorization header: {auth_header}")
    
    data = request.json
    video_url = data.get('url')
    visitor_id = data.get('visitor_id')  # This will always be present
    
    subscription_status = 'INACTIVE'  # Default status
    
    # Process Bearer token if present
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        try:
            # Verify token and get user's subscription status
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            decoded_token = jwt.decode(
                token,
                signing_key.key,
                algorithms=AUTH0_ALGORITHMS,
                audience=os.getenv('AUTH0_AUDIENCE'),
                issuer=f'https://{AUTH0_DOMAIN}/'
            )
            
            # Get user's subscription status from database
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
    
    logging.info(f"Received request at /get_tutorial with video_url: {video_url}")
        
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
                    
                    if note_count >= 7:
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
    s3_key = f"notes/{video_id}"
    
    try:
        # Check if the markdown exists in S3
        s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
        tutorial = s3_response['Body'].read().decode('utf-8')

        # Record the view only if user is not ACTIVE
        if subscription_status != 'ACTIVE':
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

        return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except s3_client.exceptions.NoSuchKey:
        return jsonify({'error': 'Tutorial not found'}), 404
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
                'total_free_notes': 7
            }), 200

    except Exception as e:
        logging.error(f"Database error checking visitor notes: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

# Add Stripe configuration after other configurations
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.data.decode("utf-8")
    signature = request.headers.get('Stripe-Signature')

    try:
        # Verify Stripe signature
        event = stripe.Webhook.construct_event(payload, signature, endpoint_secret)
        
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
                    
                elif event.type == 'invoice.payment_failed':
                    invoice = event.data.object
                    attempt_count = invoice.attempt_count
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        # After 3 failed attempts, mark subscription as past_due
                        new_status = 'past_due' if attempt_count >= 3 else 'active'
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
                                subscription_id = NULL,
                                updated_at = NOW()
                            WHERE stripe_customer_id = %s
                        """, (subscription.customer,))
                        
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Subscription cancelled',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"Subscription cancelled for customer {subscription.customer}")
                
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
def get_user():
    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    email = request.args.get('email')

    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'No authentication token provided'}), 401

    token = auth_header.split(' ')[1]
    try:
        # Get the signing key and verify token
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decoded_token = jwt.decode(
            token,
            signing_key.key,
            algorithms=AUTH0_ALGORITHMS,
            audience=os.getenv('AUTH0_AUDIENCE'),
            issuer=f'https://{AUTH0_DOMAIN}/'
        )

        # Extract user info from token
        auth0_id = decoded_token['sub']

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
                
                logging.debug(f"User data: {user['subscription_cancelled_period_ends_at']}")

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

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error decoding token: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Authentication error'}), 401

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
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=AUTH0_ALGORITHMS,
                audience=os.getenv('AUTH0_AUDIENCE'),
                issuer=f'https://{AUTH0_DOMAIN}/'
            )

        try:
            decoded_token = retry_operation(verify_token)
        except jwt.InvalidTokenError as e:
            logging.error(f"Invalid JWT token: {str(e)}")
            return jsonify({'error': 'Invalid authentication token'}), 401
        except Exception as e:
            logging.error(f"Error verifying token: {type(e).__name__}: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401

        auth0_id = decoded_token['sub']

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

if __name__ == "__main__":
    app.run(debug=True)
