from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import os
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi
import re
import logging
from dotenv import load_dotenv
import tempfile
from xhtml2pdf import pisa
import boto3
from botocore.exceptions import NoCredentialsError
import time  # Import time for generating unique filenames
import uuid
import json
load_dotenv()
app = Flask(__name__)
CORS(app)

# Configure the Gemini API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Configure logging
logging.basicConfig(level=logging.INFO)

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
    transcript_data = YouTubeTranscriptApi.get_transcript(video_id, proxies=proxies, languages=["en", "es", "fr", "de", "it", "pt", "ru", "zh", "hi"])

    for entry in transcript_data:
        entry.pop('duration', None)
        if 'start' in entry: 
            entry['start'] = int(entry['start'])    
    
    # Generate a readable tutorial from the transcript
    tutorial = generate_tutorial(transcript_data, youtube_url)
    
    return tutorial

@app.route('/generate_tutorial', methods=['POST'])
def generate_tutorial_endpoint():
    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /generate_tutorial with video_url: {video_url}")
        
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
    
    # Define the S3 bucket and key
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")  # Get the bucket name from environment variable
    s3_key = f"notes/{video_id}"  # Unique key for the markdown in S3
    
    try:
        # Check if the markdown already exists in S3
        try:
            s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
            tutorial = s3_response['Body'].read().decode('utf-8')  # Read the markdown content
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
    data = request.json
    video_url = data.get('url')
    logging.info(f"Received request at /get_tutorial with video_url: {video_url}")
        
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
    
    # Define the S3 bucket and key
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")  # Get the bucket name from environment variable
    s3_key = f"notes/{video_id}"  # Unique key for the markdown in S3
    
    try:
        # Check if the markdown exists in S3
        s3_response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
        tutorial = s3_response['Body'].read().decode('utf-8')  # Read the markdown content
        return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except s3_client.exceptions.NoSuchKey:
        # If the markdown does not exist, return 404
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

if __name__ == "__main__":
    app.run(debug=True)
