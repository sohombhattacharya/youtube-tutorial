from youtube_transcript_api import YouTubeTranscriptApi
import os
import logging
import requests
import google.generativeai as genai
import re
from config import Config

def transcribe_youtube_video(video_id, youtube_url, rotate_proxy=False):
    # Determine if running locally using the environment variable
    is_local = os.getenv('APP_ENV') == 'development'

    # Set proxies only if not running locally
    proxies = None
    if not is_local:
        proxy_port = Config.PROXY_ROTATE_PORT if rotate_proxy else Config.PROXY_PORT
        proxy_url = f"{Config.PROXY_USERNAME}:{Config.PROXY_PASSWORD}@{Config.PROXY_HOST}:{proxy_port}"
        proxies = {
            'http': f"http://{proxy_url}",
            'https': f"https://{proxy_url}"
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

    model = genai.GenerativeModel("gemini-2.5-flash-lite")
    response = model.generate_content(prompt)
    
    # Log the title of the TLDR only if there is a response
    if response:
        title = response.text[:75]  
        logging.info(f"TLDR generated for {youtube_url}, {title}")

        # Replace [sec:XX] with hyperlinks
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', youtube_url)
        if video_id_match:
            video_id = video_id_match.group(1)
            markdown_text = response.text
            
            # Function to replace [sec:XX] with markdown hyperlinks
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
                
                return f'[{display_time}](https://youtu.be/{video_id}?t={seconds})'
            
            markdown_text = re.sub(r'\[sec:(\d+)\]', replace_sec_links, markdown_text)
            return markdown_text
    else:
        return 'No TLDR generated.'
    
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
    
    model = genai.GenerativeModel("gemini-2.5-flash-lite")
    response = model.generate_content(prompt)
    
    # Log the title of the tutorial only if there is a response
    if response:

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