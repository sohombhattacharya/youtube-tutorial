from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

def get_youtube_links(search_query):
    # Set up Chrome driver in headless mode
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    driver = webdriver.Chrome(options=options)
    
    try:
        # Navigate to YouTube
        driver.get("https://www.youtube.com")
        
        # Wait for and find the search box
        search_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.NAME, "search_query"))
        )
        
        # Enter search query and submit
        search_box.send_keys(search_query)
        search_box.send_keys(Keys.RETURN)
        
        # Wait for results to load
        time.sleep(3)
        
        # Scroll down to load more videos
        for _ in range(3):
            driver.execute_script("window.scrollTo(0, document.documentElement.scrollHeight);")
            time.sleep(2)
        
        # Find all video links
        video_links = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a#video-title"))
        )
        
        # Extract and return the first 25 links
        results = []
        for link in video_links[:25]:
            href = link.get_attribute('href')
            if href and 'watch?v=' in href:
                results.append(href)
        
        return results
    
    finally:
        # Close the browser
        driver.quit()

# Example usage
if __name__ == "__main__":
    search_term = input("Enter your YouTube search query: ")
    links = get_youtube_links(search_term)
    
    print("\nFirst 25 YouTube video links:")
    for i, link in enumerate(links, 1):
        print(f"{i}. {link}")
