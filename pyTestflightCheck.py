import requests
from bs4 import BeautifulSoup

# Function to fetch the User-Agent for iPhone Mobile Safari from useragents.me
def get_iphone_user_agent():
    try:
        # Send an HTTP GET request to useragents.me
        response = requests.get("https://www.useragents.me/allagents/iphone", headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()  # Raise an exception for any HTTP errors
        soup = BeautifulSoup(response.text, "html.parser")

        # Find and extract the User-Agent string for iPhone Mobile Safari
        user_agent = soup.find("code").text.strip()
        return user_agent

    except requests.RequestException as e:
        # Handle any request exceptions (e.g., connection errors)
        print(f"Failed to fetch User-Agent: {e}")
        return None

# Function to check TestFlight URL for beta status using a specific User-Agent
def check_testflight_full(url, user_agent):
    headers = {
        'User-Agent': user_agent
    }
    
    try:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Failed to fetch URL: {e}")
        return
    
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # These are placeholder conditions and should be updated based on how Apple marks a 'full' beta
    if soup.find('div', {'class': 'beta-status'}):
        print("Beta is full.")
    elif soup.find('div', {'class': 'beta-steps'}):
        print("Beta is not full.")
    else:
        print("Could not determine beta status.")

if __name__ == "__main__":
    # Get the User-Agent for iPhone Mobile Safari
    iphone_user_agent = get_iphone_user_agent()
    
    if iphone_user_agent:
        print(f"Using User-Agent: {iphone_user_agent}")
        
        # List of TestFlight URLs with names to check
        testflight_urls = [
            {"name": "App 1", "url": "https://testflight.apple.com/join/Trcbh1o3"},
            {"name": "App 2", "url": "https://testflight.apple.com/join/NLskzwi5"},
            # Add more URLs and names as needed
        ]
    
        # Iterate through the list of URLs and names
        for app_info in testflight_urls:
            app_name = app_info["name"]
            app_url = app_info["url"]
            
            # Display the name of the app being checked
            print(f"Checking {app_name}...")
            
            # Call the check_testflight_full function to check the URL's beta status using the iPhone User-Agent
            check_testflight_full(app_url, iphone_user_agent)
            
            # Print a blank line for separation between app checks
            print()
    else:
        print("Failed to fetch iPhone User-Agent.")
