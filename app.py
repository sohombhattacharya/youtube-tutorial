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

def generate_tutorial(transcript):
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
        "4. **Examples and Illustrations**:\n"
        "   - Include relevant examples, case studies, or anecdotes to illustrate key points.\n"
        "   - If applicable, provide code snippets or practical applications related to the topic.\n\n"
        "5. **Visual Elements**:\n"
        "   - Suggest where images, diagrams, or charts could be included to support the text.\n"
        "   - Describe any visual elements that would enhance understanding.\n\n"
        "6. **Conclusion**:\n"
        "   - Summarize the key takeaways from the tutorial.\n"
        "   - Encourage readers to explore further or apply what they have learned.\n\n"
        "7. **Engagement**:\n"
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
