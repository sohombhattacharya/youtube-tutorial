from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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

def main():
    # Replace with your actual API key
    API_KEY = 'AIzaSyD3-yL9Fs2CmtNgxsL-r3i-7KEfmkttJPs'
    
    # Get search query from user
    search_query = input('Enter your search query: ')
    
    # Search YouTube with 25 results
    videos = search_youtube(search_query, API_KEY, max_results=25)
    
    # Print results
    if videos:
        print('\nFound videos:')
        for i, video in enumerate(videos, 1):
            print(f"{i}. {video['title']}\n   {video['url']}\n")
    else:
        print('No videos found or an error occurred.')

if __name__ == '__main__':
    main()
