import requests
from bs4 import BeautifulSoup

# Function to check TestFlight URL for beta status
def check_testflight_full(url):
    # Define user-agent headers to mimic a web browser
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 EdgiOS/116.1938.72 Mobile/15E148 Safari/605.1.15'
    }
    
    try:
        # Send an HTTP GET request to the TestFlight URL
        r = requests.get(url, headers=headers)
        r.raise_for_status()  # Raise an exception for any HTTP errors
    except requests.RequestException as e:
        # Handle any request exceptions (e.g., connection errors)
        print(f"Failed to fetch URL: {e}")
        return
    
    # Parse the HTML content of the page using BeautifulSoup
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # These are placeholder conditions and should be updated based on how Apple marks a 'full' beta
    if soup.find('div', {'class': 'beta-status'}):
        print("Beta is full.")
    elif soup.find('div', {'class': 'beta-steps'}):
        print("Beta is not full.")
    else:
        print("Could not determine beta status.")

if __name__ == "__main__":
    # List of TestFlight URLs with names to check
    testflight_urls = [
        {"name": "App 1", "url": "https://testflight.apple.com/join/your-testflight-url"},
        {"name": "App 2", "url": "https://testflight.apple.com/join/your-testflight-url"},
        # Add more URLs and names as needed
    ]
    
    # Iterate through the list of URLs and names
    for app_info in testflight_urls:
        app_name = app_info["name"]
        app_url = app_info["url"]
        
        # Display the name of the app being checked
        print(f"Checking status of {app_name}...")
        
        # Call the check_testflight_full function to check the URL's beta status
        check_testflight_full(app_url)
        
        # Print a blank line for separation between app checks
        print()
