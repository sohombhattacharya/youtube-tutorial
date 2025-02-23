from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

def get_youtube_links(search_query):
    start_time = time.time()
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    
    # Existing performance optimizations
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-extensions')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.page_load_strategy = 'eager'
    
    driver = webdriver.Chrome(options=options)
    
    try:
        # Navigate directly to search results instead of homepage
        driver.get(f"https://www.youtube.com/results?search_query={search_query}")
        
        # Reduce scroll iterations and wait time
        for _ in range(2):  # Reduced from 3 to 2
            driver.execute_script("window.scrollTo(0, document.documentElement.scrollHeight);")
            time.sleep(1)  # Reduced from 2 to 1
        
        # Find all video links and titles
        video_elements = WebDriverWait(driver, 5).until(  # Reduced timeout from 10 to 5
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a#video-title"))
        )
        
        # Extract and return the first 25 links and titles
        results = []
        for element in video_elements[:25]:
            href = element.get_attribute('href')
            title = element.get_attribute('title')
            if href and 'watch?v=' in href:
                results.append((href, title))
        
        return results
    
    finally:
        # Close the browser
        driver.quit()

# Example usage
if __name__ == "__main__":
    search_term = input("Enter your YouTube search query: ")
    start_time = time.time()  # Add timer start
    video_info = get_youtube_links(search_term)
    end_time = time.time()  # Add timer end
    
    print(f"\nSearch completed in {end_time - start_time:.2f} seconds")
    print("\nFirst 25 YouTube videos:")
    for i, (link, title) in enumerate(video_info, 1):
        print(f"{i}. {title}\n   {link}\n")
