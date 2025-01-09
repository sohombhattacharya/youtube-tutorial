from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi
import re
import logging

app = Flask(__name__)
CORS(app)

# Configure the Gemini API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Configure logging
logging.basicConfig(level=logging.INFO)

def generate_tutorial(transcript, youtube_url):
    # Create a detailed prompt for the Gemini model
    prompt = (
        "# Tutorial Generation from YouTube Transcript\n\n"
        "## Objective\n"
        "Create a comprehensive and engaging markdown tutorial based on the following YouTube transcript. The tutorial should be structured, informative, and easy to follow, providing readers with a clear understanding of the content discussed in the video.\n\n"
        "## Instructions\n"
        "1. **Introduction**:\n"
        "   - Begin with a brief introduction that summarizes the main topic of the video.\n"
        "   - Explain the significance of the topic and what readers can expect to learn.\n\n"
        "2. **Section Headings**:\n"
        "   - Divide the content into clear sections with descriptive headings.\n"
        "   - Each section should cover a specific aspect of the topic discussed in the transcript.\n\n"
        "3. **Detailed Explanations**:\n"
        "   - Provide in-depth explanations for each point made in the transcript.\n"
        "   - Use bullet points or numbered lists where appropriate to enhance readability.\n\n"
        "4. **Conclusion**:\n"
        "   - Summarize the key takeaways from the tutorial.\n"
        "   - Encourage readers to explore further or apply what they have learned.\n\n"
        "5. **Engagement**:\n"
        "   - Use a conversational tone to engage the reader.\n"
        "   - Pose questions or prompts that encourage readers to think critically about the content.\n\n"
        "## Transcript\n"
        f"{transcript}\n\n"
        "## Output Format\n"
        "The output should be in markdown format, properly formatted with headings, lists, and code blocks as necessary. Ensure that the tutorial is polished and ready for publication.\n\n"
        "Tutorial:"
    )
    
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    
    # Log the title of the tutorial only if there is a response
    if response:
        title = response.text[:50]  # Extract the first 50 characters
        logging.info(f"{youtube_url}, {title}")  # Log the title
        return response.text
    else:
        return 'No tutorial generated.'

def transcribe_youtube_video(video_id, youtube_url):
    # Fetch the transcript for the given video ID
    transcript_data = YouTubeTranscriptApi.get_transcript(video_id, proxies={
        'http': "http://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001",
        'https': "https://spclyk9gey:2Oujegb7i53~YORtoe@gate.smartproxy.com:10001"})
    
    # Combine the transcript entries into a single string
    transcript = " ".join(entry['text'] for entry in transcript_data)
    
    # Generate a readable tutorial from the transcript
    tutorial = generate_tutorial(transcript, youtube_url)
    
    return tutorial

@app.route('/generate_tutorial', methods=['POST'])
def generate_tutorial_endpoint():
    data = request.json
    video_url = data.get('url')
    
    # Log the video URL
    logging.info(f"Received YouTube video URL: {video_url}")
    
    # Extract video ID from the URL
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', video_url)
    if not video_id_match:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    video_id = video_id_match.group(1)  # Get the video ID
    
    try:
        # Generate the tutorial
        tutorial = transcribe_youtube_video(video_id, video_url)
        
        # Return the markdown as plain text
        return tutorial, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
