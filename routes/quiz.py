
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

quiz_bp = Blueprint('quiz', __name__)

@quiz_bp.route('/generate_quiz', methods=['POST'])
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
        "IMPORTANT: Return ONLY a valid JSON object. Do NOT wrap the response in markdown code blocks, backticks, or any other formatting. "
        "Do NOT include ```json or ``` in your response. Start directly with { and end with }. "
        "Use the following example structure for 5 questions:\n"
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
        "The correctAnswer position should be randomly selected. we should not, for example, have lots of questions with the correct answer in the same position. "
        "Encourage the model to use nuanced language and scenarios related to the NFL Sunday Ticket to create engaging questions. "
        f"Markdown Content:\n{markdown_content}"
    )
    
    model = genai.GenerativeModel("gemini-2.5-flash-lite")
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