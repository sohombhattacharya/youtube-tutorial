from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi
import re

app = Flask(__name__)
CORS(app)

# Configure the Gemini API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = Flask(__name__)

def generate_tutorial(transcript):
    # Create a detailed prompt for the Gemini model
    prompt = (
        "Based on the following transcript, create a detailed tutorial in markdown format. "
        "Break down the content into sections with headings, "
        "and provide clear explanations and examples where applicable.\n\n"
        f"Transcript:\n{transcript}\n\n"
        "Tutorial:"
    )
    
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    
    # Extract the tutorial from the response
    return response.text if response else 'No tutorial generated.'

def transcribe_youtube_video(video_id):
    # Fetch the transcript for the given video ID
    transcript_data = YouTubeTranscriptApi.get_transcript(video_id, proxies={
        'http': "http://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001",
        'https': "https://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001"})
    
    # Combine the transcript entries into a single string
    transcript = " ".join(entry['text'] for entry in transcript_data)
    
    # Generate a readable tutorial from the transcript
    tutorial = generate_tutorial(transcript)
    
    return tutorial

@app.route('/generate_tutorial', methods=['POST'])
def generate_tutorial_endpoint():
    data = request.json
    video_url = data.get('url')
    
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)  # Get the video ID
    
    try:
        # Generate the tutorial
        tutorial = transcribe_youtube_video(video_id)
        
        # Return the markdown as plain text
        return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
