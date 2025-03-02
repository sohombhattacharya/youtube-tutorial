from flask import Blueprint, request, jsonify, g, send_file, make_response, current_app
import logging
import re
import os
import boto3
import tempfile
import uuid  # Add this import
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
import urllib.parse
from services.youtube_service import transcribe_youtube_video, generate_tldr
from services.auth_service import auth0_validator, AUTH0_DOMAIN
from services.database import get_db_connection
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from threading import BoundedSemaphore
from concurrent.futures import ThreadPoolExecutor, as_completed

search_bp = Blueprint('search', __name__)

@search_bp.route('/search_youtube', methods=['GET'])
def search_youtube_endpoint():
    try:
        auth_header = request.headers.get('Authorization')
        search_query = request.args.get('query')
        
        if not search_query:
            return jsonify({'error': 'Search query is required'}), 400

        # Require authentication
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401

        # Process authentication token
        token = auth_header.split(' ')[1]
        subscription_status = None
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

            # Check user's subscription status and get user_id
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, subscription_status, product_id 
                    FROM users 
                    WHERE auth0_id = %s
                    """,
                    (auth0_id,)
                )
                result = cur.fetchone()
                if not result:
                    return jsonify({'error': 'User not found'}), 404
                
                user_id = result[0]
                subscription_status = result[1]
                product_id = result[2]
                
                # Get report limits based on subscription tier
                report_limit = 3  # Default for free users
                
                if subscription_status == 'ACTIVE':
                    # Get product IDs from environment variables
                    pro_plan_id = os.getenv('PRO_PLAN_PRODUCT_ID')
                    advanced_plan_id = os.getenv('ADVANCED_PLAN_PRODUCT_ID')
                    growth_plan_id = os.getenv('GROWTH_PLAN_PRODUCT_ID')
                    
                    if product_id == pro_plan_id:
                        report_limit = 10
                    elif product_id == advanced_plan_id:
                        report_limit = 50
                    elif product_id == growth_plan_id:
                        report_limit = 150
                
                # Check report limit for the current month
                cur.execute(
                    """
                    SELECT COUNT(*) 
                    FROM user_reports 
                    WHERE user_id = %s
                    AND created_at >= DATE_TRUNC('month', CURRENT_DATE)
                    """,
                    (user_id,)
                )
                report_count = cur.fetchone()[0]
                
                if report_count >= report_limit:
                    return jsonify({
                        'error': 'Report limit reached',
                        'message': f'You have reached the maximum number of {report_limit} reports for this month.'
                    }), 403

        except Exception as e:
            logging.error(f"Error verifying token: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401

        # Everyone gets fast search
        fast_search_response = fast_search_youtube(search_query)
        
        # Check if fast search was successful
        if fast_search_response and 'error' not in fast_search_response:
            # Get environment and base URL for source links
            is_dev = os.getenv('APP_ENV') == 'development'
            base_url = 'http://localhost:8080' if is_dev else 'https://swiftnotes.ai'
            
            # Save the report to database and S3
            try:
                # Extract title
                title = fast_search_response.get('title', f"Research Report: {search_query[:50]}")
                
                # Save to database
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO user_reports 
                        (user_id, search_query, title, created_at)
                        VALUES (%s, %s, %s, NOW())
                        RETURNING id
                        """,
                        (user_id, search_query, title)
                    )
                    report_id = cur.fetchone()[0]
                    conn.commit()

                # Save to S3
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
                )
                bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
                s3_key = f"reports/{report_id}"
                
                # Add sources section to markdown content
                markdown_content = fast_search_response['content']
                markdown_content += "\n\n## Sources\n"
                for source in fast_search_response['sources']:
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', source['url'])
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        note_url = f"{base_url}/?v={video_id}"
                        markdown_content += f"{source['number']}. [{source['title']}]({note_url})\n"
                    else:
                        markdown_content += f"{source['number']}. [{source['title']}]({source['url']})\n"
                
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=s3_key,
                    Body=markdown_content,
                    ContentType='text/plain'
                )

                return jsonify({
                    'id': str(report_id),
                    'content': markdown_content,
                }), 200
                
            except Exception as e:
                logging.error(f"Error saving fast search report: {str(e)}")
                # Continue with regular search if fast search fails to save
        else: 
            return jsonify({'error': 'Failed to generate report'}), 500

        # Replace with your actual API key
        API_KEY = os.getenv('GOOGLE_API_KEY')
        
        # Log info for request
        logging.info(f"Received request at /search_youtube with query: {search_query} from user {auth0_id}")

        # Search YouTube with 25 results
        videos = search_youtube(search_query, API_KEY, max_results=25)
        
        # Generate tutorials for each video
        all_tutorials = []
        for video in videos:
            try:
                video_url = video['url']
                video_title = video['title']
                
                # Extract video ID from URL
                video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
                if not video_id_match:
                    continue
                video_id = video_id_match.group(1)
                
                # Check if tutorial exists in S3
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
                )
                bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
                s3_key = f"notes/{video_id}"
                
                try:
                    s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
                    tutorial = s3_response['Body'].read().decode('utf-8')
                except s3_client.exceptions.NoSuchKey:
                    # Generate new tutorial if not found
                    tutorial = transcribe_youtube_video(video_id, video_url)

                    s3_client.put_object(
                        Bucket=bucket_name,
                        Key=s3_key,
                        Body=tutorial,
                        ContentType='text/plain'
                    )
                
                all_tutorials.append({
                    'title': video_title,
                    'url': video_url,
                    'content': tutorial
                })
                
            except Exception as e:
                logging.error(f"Error processing video {video_url}: {str(e)}")
                continue

        # Generate comprehensive report using Gemini
        if all_tutorials:
            prompt = (
                "# Analysis Task\n\n"
                f"## User Query\n{search_query}\n\n"
                "## Task Overview\n"
                "1. First, analyze the user's query to understand:\n"
                "   - Is this a specific question seeking direct answers?\n"
                "   - Is this a broad topic requiring synthesis and exploration?\n"
                "   - What are the key aspects or dimensions that need to be addressed?\n"
                "   - What would be most valuable to the user based on their query?\n"
                "   - What deeper implications or connections should be explored?\n\n"
                "2. Then, without mentioning the type of the user's query, and without mentioning that this is an analysis of video transcripts, structure your response appropriately based on the query type. For example:\n"
                "   - For specific questions: Provide comprehensive answers with in-depth analysis and multiple perspectives\n"
                "   - For broad topics: Deliver thorough synthesis with detailed exploration of key themes\n"
                "   - For comparisons: Examine nuanced differences and complex trade-offs\n"
                "   - For how-to queries: Include detailed methodology and consideration of edge cases\n\n"
                "## Content Guidelines\n"
                " - Create a title for the report that is a summary of the report\n"
                "- Structure the response in the most logical way for this specific query\n"
                "- Deeply analyze different perspectives and approaches\n"
                "- Highlight both obvious and subtle connections between sources\n"
                "- Examine any contradictions or disagreements in detail\n"
                "- Draw meaningful conclusions that directly relate to the query\n"
                "- Consider practical implications and real-world applications\n"
                "- Explore edge cases and potential limitations\n"
                "- Identify patterns and trends across sources\n\n"
                "## Citation and Reference Guidelines\n"
                "- Include timestamp links whenever referencing specific content\n"
                "- Add timestamps for:\n"
                "  * Direct quotes or key statements\n"
                "  * Important examples or demonstrations\n"
                "  * Technical explanations or tutorials\n"
                "  * Expert opinions or insights\n"
                "  * Supporting evidence for major claims\n"
                "  * Contrasting viewpoints or approaches\n"
                "- Format timestamps as markdown links to specific moments in the videos\n"
                "- Integrate timestamps naturally into the text to maintain readability\n"
                "- Use multiple timestamps when a point is supported across different sources\n\n"
                "## Formatting Requirements\n"
                "- Use proper markdown headers (# for main title, ## for sections)\n"
                "- Use proper markdown lists (- for bullets, 1. for numbered lists)\n"
                "- Format quotes with > for blockquotes\n"
                "- Use **bold** for emphasis\n"
                "- Ensure all newlines are proper markdown line breaks\n"
                "- Format timestamps as [MM:SS](video-link) or similar\n\n"
                "## Source Materials\n"
                f"{json.dumps([{'title': t['title'], 'content': t['content']} for t in all_tutorials], indent=2)}\n\n"
                "Analyze these materials thoroughly to provide a detailed, well-reasoned response that best serves the user's needs. "
                "Don't summarize - dig deep into the content and explore all relevant aspects and implications. "
                "Support your analysis with specific references and timestamp links throughout the response. Don't mention that this is an analysis of multiple YouTube video transcripts. "
            )
            
            # Add each tutorial's content with its source
            for tutorial in all_tutorials:
                prompt += f"\n### Video: {tutorial['title']}\n{tutorial['content']}\n"
            
            # Generate the report
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(prompt)
            
            if response and response.text:
                # Get environment and base URL
                is_dev = os.getenv('APP_ENV') == 'development'
                base_url = 'http://localhost:8080' if is_dev else 'https://swiftnotes.ai'
                
                # Create a mapping of video IDs to source numbers
                video_id_to_source = {}
                for i, tutorial in enumerate(all_tutorials, 1):
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', tutorial['url'])
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        video_id_to_source[video_id] = i

                # Update timestamp hyperlinks with source numbers
                def add_source_number(match):
                    url = match.group(0)
                    
                    # Only process links that are actual YouTube timestamp links
                    if not ('youtu.be' in url and '?t=' in url):
                        return url
                        
                    video_id_match = re.search(r'youtu\.be/([0-9A-Za-z_-]{11})', url)
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        source_num = video_id_to_source.get(video_id)
                        if source_num:
                            # Extract the display text (time) from the markdown link
                            display_text_match = re.search(r'\[(.*?)\]', url)
                            if not display_text_match:
                                return url
                            display_text = display_text_match.group(1)
                            
                            # Extract the URL part from the markdown link
                            url_match = re.search(r'\((.*?)\)', url)
                            if not url_match:
                                return url
                            url_part = url_match.group(1)
                            
                            # Verify this is a valid timestamp link before formatting
                            if display_text and url_part and 'youtu.be' in url_part and '?t=' in url_part:
                                return f'[({source_num}) {display_text}]({url_part})'
                        return url
                    return url

                # Update the regex pattern to only match YouTube timestamp links
                markdown_content = re.sub(r'\[[^\]]+?\]\(https://youtu\.be/[^)]+\?t=\d+\)', add_source_number, response.text.strip())

                # Add the sources section
                markdown_content += "\n\n## Sources\n"
                for i, tutorial in enumerate(all_tutorials, 1):
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', tutorial['url'])

                    if video_id_match:
                        video_id = video_id_match.group(1)
                        note_url = f"{base_url}/?v={video_id}"
                        markdown_content += f"{i}. [{tutorial['title']}]({note_url})\n"
                    else:
                        markdown_content += f"{i}. [{tutorial['title']}]({tutorial['url']})\n"

                # After generating the markdown content, save the report
                if markdown_content:
                    report_id = None
                    try:
                        # Extract title
                        title = None
                        for line in markdown_content.split('\n'):
                            if line.startswith('# '):
                                title = line.replace('# ', '').strip()
                                break
                            if line.startswith('## '):
                                title = line.replace('## ', '').strip()
                                break
                        
                        if not title:
                            first_line = markdown_content.split('\n')[0].strip()
                            if first_line:
                                title = first_line[:100]
                            else:
                                title = f"Research Report: {search_query[:50]}"
                        
                        if not title or len(title.strip()) == 0:
                            title = f"Research Report: {search_query[:50]}"
                            
                        logging.info(f"Extracted title: {title}")

                        # Save to database
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO user_reports 
                                (user_id, search_query, title, created_at)
                                VALUES (%s, %s, %s, NOW())
                                RETURNING id
                                """,
                                (user_id, search_query, title)
                            )
                            report_id = cur.fetchone()[0]
                            conn.commit()

                        # Save to S3
                        s3_key = f"reports/{report_id}"
                        s3_client.put_object(
                            Bucket=bucket_name,
                            Key=s3_key,
                            Body=markdown_content,
                            ContentType='text/plain'
                        )

                    except Exception as e:
                        logging.error(f"Error saving report: {str(e)}")
                        return jsonify({'error': 'Failed to save report'}), 500

                    return jsonify({
                        'id': str(report_id),
                        'content': markdown_content,
                    }), 200
                else:
                    return jsonify({'error': 'Failed to generate report'}), 500
            
    except Exception as e:
        logging.error(f"Error in search_youtube: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

def search_youtube(query, api_key, max_results=10):
    """
    Search YouTube for videos matching the query and return their URLs and titles.
    
    Args:
        query (str): Search term
        api_key (str): YouTube Data API key
        max_results (int): Maximum number of results to return (default 10)
    """
    try:
        # Create YouTube API client
        youtube = build('youtube', 'v3', developerKey=api_key)
        
        # Call the search.list method
        search_response = youtube.search().list(
            q=query,
            part='id,snippet',  # Added snippet to get video titles
            maxResults=max_results,
            type='video'  # Only search for videos
        ).execute()
        
        # Extract video URLs and titles
        videos = []
        for item in search_response['items']:
            video_id = item['id']['videoId']
            video_url = f'https://www.youtube.com/watch?v={video_id}'
            video_title = item['snippet']['title']
            videos.append({'url': video_url, 'title': video_title})
            
        return videos
        
    except HttpError as e:
        print(f'An HTTP error {e.resp.status} occurred: {e.content}')
        return []

def scrape_youtube_links(search_query, max_retries=1):
    start_time = time.time()
    results = []
    
    # Determine if running locally using the environment variable
    is_local = os.getenv('APP_ENV') == 'development'
    plugin_dir = 'proxy_auth_plugin'

    for attempt in range(max_retries):
        try:
            # Configure Chrome options
            chrome_options = webdriver.ChromeOptions()
            chrome_options.add_argument('--headless=new')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-software-rasterizer')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-infobars')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--ignore-certificate-errors')
            chrome_options.add_argument('--disable-popup-blocking')
            
            # Set up proxy only if not running locally
            if not is_local:
                # SmartProxy credentials
                SMARTPROXY_USER = "spclyk9gey"
                SMARTPROXY_PASS = "2Oujegb7i53~YORtoe"
                SMARTPROXY_ENDPOINT = "gate.smartproxy.com"
                SMARTPROXY_PORT = "7000"  # Using HTTPS port instead of HTTP

                # https://github.com/Smartproxy/Selenium-proxy-authentication

                # Create manifest for Chrome extension
                manifest_json = """
                {
                    "version": "1.0.0",
                    "manifest_version": 2,
                    "name": "Chrome Proxy",
                    "permissions": [
                        "proxy",
                        "tabs",
                        "unlimitedStorage",
                        "storage",
                        "<all_urls>",
                        "webRequest",
                        "webRequestBlocking"
                    ],
                    "background": {
                        "scripts": ["background.js"]
                    },
                    "minimum_chrome_version":"22.0.0"
                }
                """

                background_js = """
                var config = {
                    mode: "fixed_servers",
                    rules: {
                        singleProxy: {
                            scheme: "https",
                            host: "%s",
                            port: parseInt(%s)
                        },
                        bypassList: ["localhost"]
                    }
                };

                chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});

                function callbackFn(details) {
                    return {
                        authCredentials: {
                            username: "%s",
                            password: "%s"
                        }
                    };
                }

                chrome.webRequest.onAuthRequired.addListener(
                    callbackFn,
                    {urls: ["<all_urls>"]},
                    ['blocking']
                );
                """ % (SMARTPROXY_ENDPOINT, SMARTPROXY_PORT, SMARTPROXY_USER, SMARTPROXY_PASS)

                # Create a Chrome extension to handle the proxy
                if not os.path.exists(plugin_dir):
                    os.makedirs(plugin_dir)

                with open(f'{plugin_dir}/manifest.json', 'w') as f:
                    f.write(manifest_json)

                with open(f'{plugin_dir}/background.js', 'w') as f:
                    f.write(background_js)

                chrome_options.add_argument(f'--load-extension={os.path.abspath(plugin_dir)}')
            
            # Set user agent
            chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
            
            # Initialize webdriver with increased timeouts
            driver = webdriver.Chrome(options=chrome_options)
            driver.set_page_load_timeout(60)
            driver.implicitly_wait(20)
            
            try:
                # Navigate to YouTube search
                encoded_query = urllib.parse.quote(search_query)
                url = f"https://www.youtube.com/results?search_query={encoded_query}"
                
                logging.info(f"Attempt {attempt + 1}: Navigating to {url}")
                driver.get(url)
                
                # Wait for video results
                wait = WebDriverWait(driver, 30)
                wait.until(
                    EC.presence_of_element_located((By.TAG_NAME, "ytd-video-renderer"))
                )
                
                # Scroll gradually
                scroll_pause_time = 2
                for _ in range(4):
                    driver.execute_script("window.scrollBy(0, 800);")
                    time.sleep(scroll_pause_time)
                
                # Extract video information
                video_elements = wait.until(
                    EC.presence_of_all_elements_located((By.TAG_NAME, "ytd-video-renderer"))
                )[:25]
                
                for element in video_elements:
                    try:
                        title_element = element.find_element(By.ID, "video-title")
                        href = title_element.get_attribute("href")
                        title = title_element.get_attribute("title")
                        
                        if href and title and 'watch?v=' in href:
                            results.append((href, title))
                            
                    except Exception as e:
                        logging.warning(f"Error extracting video details: {str(e)}")
                        continue
                
                if results:
                    try:
                        s3_client = boto3.client(
                            's3',
                            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
                        )
                        bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
                        s3_key = f"youtube_page_source/{search_query}_{attempt}.html"
                        
                        page_content = driver.page_source
                        s3_client.put_object(
                            Bucket=bucket_name,
                            Key=s3_key,
                            Body=page_content,
                            ContentType='text/html'
                        )
                        logging.info("Page source uploaded to S3")
                    except Exception as e:
                        logging.error(f"Error uploading page source to S3: {str(e)}")
                    
                    break
                
            finally:
                try:
                    driver.quit()
                except Exception as e:
                    logging.warning(f"Error closing driver: {str(e)}")
                
                # Clean up proxy plugin directory if it exists
                if not is_local and os.path.exists(plugin_dir):
                    import shutil
                    shutil.rmtree(plugin_dir)
                
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {type(e).__name__}: {str(e)}")
            if attempt == max_retries - 1:
                logging.error("All attempts failed to scrape YouTube links")
                return [], time.time() - start_time
            time.sleep(2 ** attempt)
        
    end_time = time.time()
    return results, end_time - start_time

def process_video(video):
    """Helper function to process a single video and get its tutorial"""
    try:
        video_url = video[0]  # URL is first element in tuple
        video_title = video[1]  # Title is second element in tuple
        
        # Extract video ID from URL
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
        if not video_id_match:
            return None
        video_id = video_id_match.group(1)
        
        # Check if tutorial exists in S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
        )
        bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
        s3_key = f"notes/{video_id}"
        
        try:
            s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
            tutorial = s3_response['Body'].read().decode('utf-8')
        except s3_client.exceptions.NoSuchKey:
            # Generate new tutorial if not found
            tutorial = transcribe_youtube_video(video_id, video_url, rotate_proxy=True)

            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=tutorial,
                ContentType='text/plain'
            )
        
        return {
            'title': video_title,
            'url': video_url,
            'content': tutorial
        }
        
    except Exception as e:
        logging.error(f"Error processing video {video_url}: {str(e)}")
        return None

@search_bp.route('/deep_research', methods=['GET'])
def deep_research():
    """
    Generate a comprehensive research report based on YouTube content.
    
    Required parameters:
    - search: The search query or research topic
    
    Authentication:
    - Requires a valid API key as Bearer token in the Authorization header
    
    Returns:
    - 200 OK: Successfully generated report
      {
        "title": "string",
        "content": "markdown string",
        "sources": [
          {
            "number": integer,
            "title": "string",
            "url": "string"
          }
        ]
      }
    
    Errors:
    - 400 Bad Request: Missing or invalid search query
    - 401 Unauthorized: Missing or invalid API key
    - 403 Forbidden: Credit limit reached
      {
        "error": "Credit limit reached",
        "message": "This call would exceed your monthly limit of X credits"
      }
    - 500 Internal Server Error: Server-side error
    """
    start_time = time.time()
    api_key = None
    conn = None
    api_call_id = str(uuid.uuid4())  # Generate UUID for both DB and S3
    
    try:
        # Get API key from Authorization header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'API key required'}), 401
        
        api_key = auth_header.split(' ')[1]
        
        # Validate API key against database
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT api_keys.id, api_keys.user_id, users.subscription_status, users.product_id
                FROM api_keys
                JOIN users ON api_keys.user_id = users.id
                WHERE api_keys.api_key = %s
                """,
                (api_key,)
            )
            result = cur.fetchone()
            
            if not result:
                return jsonify({'error': 'Invalid API key'}), 401
            
            subscription_status = result[2]
            subscription_product_id = result[3]
            
            # Check credit limits based on subscription status
            # Get current month's credit usage
            cur.execute(
                """
                SELECT SUM(credits_used) 
                FROM api_calls 
                WHERE api_key = %s 
                AND created_at >= DATE_TRUNC('month', CURRENT_DATE)
                """,
                (api_key,)
            )
            current_usage = cur.fetchone()[0] or 0
            
            # Credits for this call
            credits_for_this_call = 100
            
            # Define credit limits based on subscription
            ADVANCED_PLAN_PRODUCT_ID = os.getenv('ADVANCED_PLAN_PRODUCT_ID')
            GROWTH_PLAN_PRODUCT_ID = os.getenv('GROWTH_PLAN_PRODUCT_ID')
            
            # Set credit limit based on product ID
            credit_limit = 500  # Default for free users and Pro plan
            
            if subscription_status == 'ACTIVE':
                if subscription_product_id == ADVANCED_PLAN_PRODUCT_ID:
                    credit_limit = 5000
                elif subscription_product_id == GROWTH_PLAN_PRODUCT_ID:
                    credit_limit = 15000
            
            # Check if this call would exceed the credit limit
            if current_usage + credits_for_this_call > credit_limit:
                return jsonify({
                    'error': 'Credit limit reached',
                    'message': f'This call would exceed your monthly limit of {credit_limit} credits. Please upgrade for higher volume needs.'
                }), 403

        search_query = request.args.get('search', '').strip()
        if not search_query:
            return jsonify({'error': 'No search query provided'}), 400
        
        timing_info = {}
        timing_info['query'] = search_query
        
        # Time the YouTube scraping
        videos, scrape_time = scrape_youtube_links(search_query)
        timing_info['youtube_scraping'] = f"{scrape_time:.2f} seconds"
        
        # Process all videos in parallel with timeout and connection limiting
        all_tutorials = []
        processing_start = time.time()
        
        # Create a semaphore to limit concurrent connections
        max_concurrent = 25  # Adjust based on your server's capacity
        semaphore = BoundedSemaphore(max_concurrent)
        
        def process_with_semaphore(video):
            with semaphore:
                return process_video(video)
        
        # Process all videos in parallel with timeout
        with ThreadPoolExecutor(max_workers=len(videos)) as executor:
            future_to_video = {
                executor.submit(process_with_semaphore, video): video 
                for video in videos
            }
            
            # Collect results with timeout
            for future in as_completed(future_to_video, timeout=90):
                try:
                    result = future.result(timeout=90)  
                    if result:
                        all_tutorials.append(result)
                except TimeoutError:
                    video = future_to_video[future]
                    logging.warning(f"Timeout processing video: {video[0]}")
                    continue
                except Exception as e:
                    video = future_to_video[future]
                    logging.error(f"Error processing video {video[0]}: {str(e)}")
                    continue
        
        processing_time = time.time() - processing_start
        timing_info['video_processing'] = f"{processing_time:.2f} seconds"

        # Generate comprehensive report using selected LLM
        if all_tutorials:
            llm_start = time.time()
            prompt = (
                "# Analysis Task\n\n"
                f"## User Query\n{search_query}\n\n"
                "## Task Overview\n"
                "1. First, analyze the user's query to understand:\n"
                "   - Is this a specific question seeking direct answers?\n"
                "   - Is this a broad topic requiring synthesis and exploration?\n"
                "   - What are the key aspects or dimensions that need to be addressed?\n"
                "   - What would be most valuable to the user based on their query?\n"
                "   - What deeper implications or connections should be explored?\n\n"
                "2. Then, without mentioning the type of the user's query, and without mentioning that this is an analysis of video transcripts, structure your response appropriately based on the query type. For example:\n"
                "   - For specific questions: Provide comprehensive answers with in-depth analysis and multiple perspectives\n"
                "   - For broad topics: Deliver thorough synthesis with detailed exploration of key themes\n"
                "   - For comparisons: Examine nuanced differences and complex trade-offs\n"
                "   - For how-to queries: Include detailed methodology and consideration of edge cases\n\n"
                "## Content Guidelines\n"
                " - Create a title for the report that is a summary of the report\n"
                "- Structure the response in the most logical way for this specific query\n"
                "- Deeply analyze different perspectives and approaches\n"
                "- Highlight both obvious and subtle connections between sources\n"
                "- Examine any contradictions or disagreements in detail\n"
                "- Draw meaningful conclusions that directly relate to the query\n"
                "- Consider practical implications and real-world applications\n"
                "- Explore edge cases and potential limitations\n"
                "- Identify patterns and trends across sources\n\n"
                "## Citation and Reference Guidelines\n"
                "- Include timestamp links whenever referencing specific content\n"
                "- Add timestamps for:\n"
                "  * Direct quotes or key statements\n"
                "  * Important examples or demonstrations\n"
                "  * Technical explanations or tutorials\n"
                "  * Expert opinions or insights\n"
                "  * Supporting evidence for major claims\n"
                "  * Contrasting viewpoints or approaches\n"
                "- Format timestamps as markdown links to specific moments in the videos\n"
                "- Integrate timestamps naturally into the text to maintain readability\n"
                "- Use multiple timestamps when a point is supported across different sources\n\n"
                "## Formatting Requirements\n"
                "- Use proper markdown headers (# for main title, ## for sections)\n"
                "- Use proper markdown lists (- for bullets, 1. for numbered lists)\n"
                "- Format quotes with > for blockquotes\n"
                "- Use **bold** for emphasis\n"
                "- Ensure all newlines are proper markdown line breaks\n"
                "- Format timestamps as [MM:SS](video-link) or similar\n\n"
                "## Source Materials\n"
                f"{json.dumps([{'title': t['title'], 'content': t['content']} for t in all_tutorials], indent=2)}\n\n"
                "Analyze these materials thoroughly to provide a detailed, well-reasoned response that best serves the user's needs. "
                "Don't summarize - dig deep into the content and explore all relevant aspects and implications. "
                "Support your analysis with specific references and timestamp links throughout the response. Don't mention that this is an analysis of multiple YouTube video transcripts. "
            )
            
            model = genai.GenerativeModel("gemini-2.0-flash-lite")
            response = model.generate_content(prompt)
            response_text = response.text if response else None
            
            llm_time = time.time() - llm_start
            timing_info['llm_generation'] = f"{llm_time:.2f} seconds"
            
            if response_text:
                # Create a mapping of video IDs to source numbers
                video_id_to_source = {}
                for i, tutorial in enumerate(all_tutorials, 1):
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', tutorial['url'])
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        video_id_to_source[video_id] = i

                # Update timestamp hyperlinks with source numbers
                def add_source_number(match):
                    url = match.group(0)
                    
                    # Only process links that are actual YouTube timestamp links
                    if not ('youtu.be' in url and '?t=' in url):
                        return url
                        
                    video_id_match = re.search(r'youtu\.be/([0-9A-Za-z_-]{11})', url)
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        source_num = video_id_to_source.get(video_id)
                        if source_num:
                            # Extract the display text (time) from the markdown link
                            display_text_match = re.search(r'\[(.*?)\]', url)
                            if not display_text_match:
                                return url
                            display_text = display_text_match.group(1)
                            
                            # Extract the URL part from the markdown link
                            url_match = re.search(r'\((.*?)\)', url)
                            if not url_match:
                                return url
                            url_part = url_match.group(1)
                            
                            # Verify this is a valid timestamp link before formatting
                            if display_text and url_part and 'youtu.be' in url_part and '?t=' in url_part:
                                return f'[({source_num}) {display_text}]({url_part})'
                        return url
                    return url

                # Update the regex pattern to only match YouTube timestamp links
                markdown_content = re.sub(r'\[[^\]]+?\]\(https://youtu\.be/[^)]+\?t=\d+\)', add_source_number, response.text.strip())

                # Create sources list instead of appending to markdown
                sources = []
                for i, tutorial in enumerate(all_tutorials, 1):
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', tutorial['url'])
                    source = {
                        'number': i,
                        'title': tutorial['title'],
                        'url': f'https://youtube.com/watch?v={video_id_match.group(1)}' if video_id_match else tutorial['url']
                    }
                    
                    sources.append(source)

                # Calculate total response time
                response_time_ms = int((time.time() - start_time) * 1000)
                
                # Return both markdown content and sources list
                if markdown_content:
                    logging.info(timing_info)

                    title = ''
                    for line in markdown_content.split('\n'):
                        if line.startswith('# '):
                            title = line.replace('# ', '').strip()
                            break

                    # Store just the response JSON in S3
                    try:
                        s3_client = boto3.client(
                            's3',
                            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
                        )
                        bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
                        
                        # Use the UUID as the S3 key
                        s3_key = f"api_responses/{api_call_id}.json"
                        
                        # Prepare the exact JSON that's being returned to the client
                        response_data = {
                            "request": {
                                "search": search_query,
                            },
                            "response": {
                                'title': title,
                                'content': markdown_content,
                                'sources': sources
                            }
                        }
                        
                        # Store the response JSON in S3
                        s3_client.put_object(
                            Bucket=bucket_name,
                            Key=s3_key,
                            Body=json.dumps(response_data),
                            ContentType='application/json'
                        )
                        
                    except Exception as e:
                        logging.error(f"Error storing API response in S3: {str(e)}")
                        # Continue even if S3 storage fails
                    
                    # Log API call to database
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO api_calls 
                                (id, api_key, endpoint_name, status_code, credits_used, request_ip, response_time_ms)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """,
                                (api_call_id, api_key, '/deep_research', 200, credits_for_this_call, request.remote_addr, response_time_ms)
                            )
                            conn.commit()
                    except Exception as e:
                        logging.error(f"Error logging API call: {str(e)}")

                    return jsonify({
                        'title': title,
                        'content': markdown_content,
                        'sources': sources
                    }), 200
                else:
                    # Log failed API call
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO api_calls 
                                (id, api_key, endpoint_name, status_code, credits_used, request_ip, response_time_ms)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """,
                                (api_call_id, api_key, '/deep_research', 500, 0, request.remote_addr, response_time_ms)
                            )
                            conn.commit()
                    except Exception as e:
                        logging.error(f"Error logging API call: {str(e)}")
                        
                    return jsonify({'error': 'Failed to generate report'}), 500
            
    except Exception as e:
        # Calculate response time even for errors
        response_time_ms = int((time.time() - start_time) * 1000)
        
        # Log error API call if we have the API key and connection
        if api_key and conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO api_calls 
                        (id, api_key, endpoint_name, status_code, credits_used, request_ip, response_time_ms)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (api_call_id, api_key, '/deep_research', 500, 0, request.remote_addr, response_time_ms)
                    )
                    conn.commit()
            except Exception as log_error:
                logging.error(f"Error logging API call: {str(log_error)}")
            
        logging.error(f"Error in search_youtube: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        # Close database connection if it exists
        if conn:
            conn.close()

def fast_search_youtube(search_query):
        logging.info(f"Starting fast search for {search_query}")
        timing_info = {}
        timing_info['query'] = search_query
        
        # Time the YouTube scraping
        videos, scrape_time = scrape_youtube_links(search_query)
        logging.info(f"Scraping time: {scrape_time:.2f} seconds")
        timing_info['youtube_scraping'] = f"{scrape_time:.2f} seconds"
        
        # Process all videos in parallel with timeout and connection limiting
        all_tutorials = []
        processing_start = time.time()
        
        # Create a semaphore to limit concurrent connections
        max_concurrent = 25  # Adjust based on your server's capacity
        semaphore = BoundedSemaphore(max_concurrent)
        
        def process_with_semaphore(video):
            with semaphore:
                return process_video(video)
        
        # Process all videos in parallel with timeout
        with ThreadPoolExecutor(max_workers=len(videos)) as executor:
            future_to_video = {
                executor.submit(process_with_semaphore, video): video 
                for video in videos
            }
            
            # Collect results with timeout
            for future in as_completed(future_to_video, timeout=90):
                try:
                    result = future.result(timeout=90)  
                    if result:
                        all_tutorials.append(result)
                except TimeoutError:
                    video = future_to_video[future]
                    logging.warning(f"Timeout processing video: {video[0]}")
                    continue
                except Exception as e:
                    video = future_to_video[future]
                    logging.error(f"Error processing video {video[0]}: {str(e)}")
                    continue
        
        processing_time = time.time() - processing_start
        timing_info['video_processing'] = f"{processing_time:.2f} seconds"

        # Generate comprehensive report using selected LLM
        if all_tutorials:
            llm_start = time.time()
            prompt = (
                "# Analysis Task\n\n"
                f"## User Query\n{search_query}\n\n"
                "## Task Overview\n"
                "1. First, analyze the user's query to understand:\n"
                "   - Is this a specific question seeking direct answers?\n"
                "   - Is this a broad topic requiring synthesis and exploration?\n"
                "   - What are the key aspects or dimensions that need to be addressed?\n"
                "   - What would be most valuable to the user based on their query?\n"
                "   - What deeper implications or connections should be explored?\n\n"
                "2. Then, without mentioning the type of the user's query, and without mentioning that this is an analysis of video transcripts, structure your response appropriately based on the query type. For example:\n"
                "   - For specific questions: Provide comprehensive answers with in-depth analysis and multiple perspectives\n"
                "   - For broad topics: Deliver thorough synthesis with detailed exploration of key themes\n"
                "   - For comparisons: Examine nuanced differences and complex trade-offs\n"
                "   - For how-to queries: Include detailed methodology and consideration of edge cases\n\n"
                "## Content Guidelines\n"
                " - Create a title for the report that is a summary of the report\n"
                "- Structure the response in the most logical way for this specific query\n"
                "- Deeply analyze different perspectives and approaches\n"
                "- Highlight both obvious and subtle connections between sources\n"
                "- Examine any contradictions or disagreements in detail\n"
                "- Draw meaningful conclusions that directly relate to the query\n"
                "- Consider practical implications and real-world applications\n"
                "- Explore edge cases and potential limitations\n"
                "- Identify patterns and trends across sources\n\n"
                "## Citation and Reference Guidelines\n"
                "- Include timestamp links whenever referencing specific content\n"
                "- Add timestamps for:\n"
                "  * Direct quotes or key statements\n"
                "  * Important examples or demonstrations\n"
                "  * Technical explanations or tutorials\n"
                "  * Expert opinions or insights\n"
                "  * Supporting evidence for major claims\n"
                "  * Contrasting viewpoints or approaches\n"
                "- Format timestamps as markdown links to specific moments in the videos\n"
                "- Integrate timestamps naturally into the text to maintain readability\n"
                "- Use multiple timestamps when a point is supported across different sources\n\n"
                "## Formatting Requirements\n"
                "- Use proper markdown headers (# for main title, ## for sections)\n"
                "- Use proper markdown lists (- for bullets, 1. for numbered lists)\n"
                "- Format quotes with > for blockquotes\n"
                "- Use **bold** for emphasis\n"
                "- Ensure all newlines are proper markdown line breaks\n"
                "- Format timestamps as [MM:SS](video-link) or similar\n\n"
                "## Source Materials\n"
                f"{json.dumps([{'title': t['title'], 'content': t['content']} for t in all_tutorials], indent=2)}\n\n"
                "Analyze these materials thoroughly to provide a detailed, well-reasoned response that best serves the user's needs. "
                "Don't summarize - dig deep into the content and explore all relevant aspects and implications. "
                "Support your analysis with specific references and timestamp links throughout the response. Don't mention that this is an analysis of multiple YouTube video transcripts. "
            )
            
            model = genai.GenerativeModel("gemini-2.0-flash-lite")
            response = model.generate_content(prompt)
            response_text = response.text if response else None
            
            llm_time = time.time() - llm_start
            timing_info['llm_generation'] = f"{llm_time:.2f} seconds"
            
            if response_text:
                
                # Create a mapping of video IDs to source numbers
                video_id_to_source = {}
                for i, tutorial in enumerate(all_tutorials, 1):
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', tutorial['url'])
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        video_id_to_source[video_id] = i

                # Update timestamp hyperlinks with source numbers
                def add_source_number(match):
                    url = match.group(0)
                    
                    # Only process links that are actual YouTube timestamp links
                    if not ('youtu.be' in url and '?t=' in url):
                        return url
                        
                    video_id_match = re.search(r'youtu\.be/([0-9A-Za-z_-]{11})', url)
                    if video_id_match:
                        video_id = video_id_match.group(1)
                        source_num = video_id_to_source.get(video_id)
                        if source_num:
                            # Extract the display text (time) from the markdown link
                            display_text_match = re.search(r'\[(.*?)\]', url)
                            if not display_text_match:
                                return url
                            display_text = display_text_match.group(1)
                            
                            # Extract the URL part from the markdown link
                            url_match = re.search(r'\((.*?)\)', url)
                            if not url_match:
                                return url
                            url_part = url_match.group(1)
                            
                            # Verify this is a valid timestamp link before formatting
                            if display_text and url_part and 'youtu.be' in url_part and '?t=' in url_part:
                                return f'[({source_num}) {display_text}]({url_part})'
                        return url
                    return url

                # Update the regex pattern to only match YouTube timestamp links
                markdown_content = re.sub(r'\[[^\]]+?\]\(https://youtu\.be/[^)]+\?t=\d+\)', add_source_number, response.text.strip())

                # Create sources list instead of appending to markdown
                sources = []
                for i, tutorial in enumerate(all_tutorials, 1):
                    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', tutorial['url'])
                    source = {
                        'number': i,
                        'title': tutorial['title'],
                        'url': f'https://youtube.com/watch?v={video_id_match.group(1)}' if video_id_match else tutorial['url']
                    }
                    
                    sources.append(source)

                # Return both markdown content and sources list
                if markdown_content:
                    logging.info(timing_info)

                    title = ''
                    for line in markdown_content.split('\n'):
                        if line.startswith('# '):
                            title = line.replace('# ', '').strip()
                            break

                    return {
                        'title': title,
                        'content': markdown_content,
                        'sources': sources
                    }
                else:
                    return {'error': 'Failed to generate report'}