import os
import json
import base64
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage

# SDK Imports
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai

# ==========================================
# CONFIGURATION
# ==========================================
YOUR_EMAIL = "chris@christopherjking.com" 
CALENDAR_2_ID = "chrisanddanaking@gmail.com" # Update if the ID differs in settings

LAT = 41.7508
LON = -88.1535
TIMEZONE = ZoneInfo("America/Chicago")

def get_google_credentials():
    b64_token = os.environ.get("GOOGLE_TOKEN_B64")
    if not b64_token:
        raise ValueError("GOOGLE_TOKEN_B64 secret is missing.")
    token_json = base64.b64decode(b64_token).decode('utf-8')
    creds_dict = json.loads(token_json)
    return Credentials.from_authorized_user_info(creds_dict)

def get_weather():
    """Fetches weather, converts WMO codes to text, and uses Fahrenheit."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&daily=weathercode,temperature_2m_max,temperature_2m_min&temperature_unit=fahrenheit&timezone=America%2FChicago"
    
    # WMO Weather interpretation codes
    weather_desc = {
        0: "Clear skies ☀️", 1: "Mainly clear 🌤️", 2: "Partly cloudy ⛅", 3: "Overcast ☁️",
        45: "Foggy 🌫️", 48: "Depositing rime fog 🌫️", 
        51: "Light drizzle 🌧️", 53: "Moderate drizzle 🌧️", 55: "Dense drizzle 🌧️",
        61: "Slight rain ☔", 63: "Moderate rain ☔", 65: "Heavy rain 🌧️",
        71: "Slight snow ❄️", 73: "Moderate snow ❄️", 75: "Heavy snow ❄️",
        95: "Thunderstorms ⛈️", 96: "Thunderstorms with slight hail ⛈️", 99: "Thunderstorms with heavy hail ⛈️"
    }

    try:
        response = requests.get(url)
        data = response.json()
        daily = data.get("daily", {})
        
        max_temp = daily.get("temperature_2m_max", [None])[0]
        min_temp = daily.get("temperature_2m_min", [None])[0]
        code = daily.get("weathercode", [0])[0]
        
        forecast = weather_desc.get(code, "Unknown conditions")
        return f"{forecast} — High of {max_temp}°F, Low of {min_temp}°F"
    except Exception as e:
        return f"Could not fetch weather: {e}"

def get_notion_tasks():
    """Fetches 'Do Today' tasks using direct API requests to avoid SDK conflicts."""
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # Note: If your Notion property is a "Select" type instead of "Status", 
    # change "status" on line 66 and 67 to "select"
    payload = {
        "filter": {
            "property": "Status",
            "status": {
                "equals": "Do Today"
            }
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        results = response.json().get("results", [])
        
        tasks = []
        for page in results:
            props = page.get("properties", {})
            # Assumes the title column is literally named "Name"
            title_list = props.get("Name", {}).get("title", [])
            if title_list:
                tasks.append(title_list[0].get("plain_text", ""))
                
        return tasks if tasks else ["No tasks marked 'Do Today'."]
    except Exception as e:
        return [f"Error fetching tasks: {e}"]

def get_calendar_events(creds, now):
    service = build('calendar', 'v3', credentials=creds)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    
    events_list = []
    for cal_id in ['primary', CALENDAR_2_ID]:
        try:
            events_result = service.events().list(
                calendarId=cal_id, timeMin=start_of_day, timeMax=end_of_day,
                singleEvents=True, orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                if 'T' in start:
                    time_obj = datetime.fromisoformat(start)
                    start = time_obj.strftime("%I:%M %p")
                
                # Tag secondary calendar events to tell them apart easily
                prefix = "" if cal_id == 'primary' else "[Shared] "
                events_list.append(f"- {start}: {prefix}{event.get('summary', 'Busy')}")
        except Exception as e:
            events_list.append(f"Error reading calendar {cal_id}: {e}")
            
    return events_list if events_list else ["No events scheduled for today."]

def get_recent_emails(creds):
    """Fetches emails from the last 24 hours and calculates the total count."""
    service = build('gmail', 'v1', credentials=creds)
    try:
        # Added "in:inbox" to ensure we only look at your primary inbox
        query = 'newer_than:1d in:inbox -category:promotions'
        
        # We handle pagination to get an accurate total count of emails
        messages = []
        request = service.users().messages().list(userId='me', q=query)
        while request is not None:
            result = request.execute()
            messages.extend(result.get('messages', []))
            request = service.users().messages().list_next(request, result)
            
        total_count = len(messages)
        
        email_data = []
        for msg in messages[:30]: # Still limit Gemini processing to top 30 to save tokens
            msg_detail = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['Subject', 'From']).execute()
            headers = msg_detail.get('payload', {}).get('headers', [])
            
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
            snippet = msg_detail.get('snippet', '')
            
            email_data.append(f"From: {sender}\nSubject: {subject}\nSnippet: {snippet}\n")
            
        return email_data, total_count
    except Exception as e:
        return [f"Error fetching emails: {e}"], 0

def get_gemini_content(email_data):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    stoic_prompt = (
        "Act as a Stoic philosopher in the style of Ryan Holiday. "
        "Provide a powerful, daily quote from Marcus Aurelius, Seneca, or Epictetus. "
        "Follow it with a 3-sentence modern, practical application for today."
    )
    stoic_response = client.models.generate_content(model='gemini-2.5-flash', contents=stoic_prompt)
    stoic_text = stoic_response.text if stoic_response else "Could not generate quote."
    
    email_text = "\n---\n".join(email_data)
    if not email_text.strip():
        email_summary = "No recent emails to summarize."
    else:
        email_prompt = (
            "Review the following email snippets from the last 24 hours. "
            "Identify any that appear to require a reply or action today. "
            "Ignore newsletters or automated alerts. "
            "Provide a concise, bulleted summary of what needs attention. "
            f"\n\nEmails:\n{email_text}"
        )
        email_response = client.models.generate_content(model='gemini-2.5-flash', contents=email_prompt)
        email_summary = email_response.text if email_response else "Could not generate summary."
        
    return stoic_text, email_summary

def send_email(creds, html_content):
    service = build('gmail', 'v1', credentials=creds)
    message = EmailMessage()
    message.set_content("Please enable HTML to view this message.")
    message.add_alternative(html_content, subtype='html')
    
    message['To'] = YOUR_EMAIL
    message['From'] = YOUR_EMAIL
    message['Subject'] = f"Daily Report: {datetime.now(TIMEZONE).strftime('%A, %B %d')}"
    
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    create_message = {'raw': encoded_message}
    service.users().messages().send(userId='me', body=create_message).execute()

def main():
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%A, %B %d, %Y")
    
    google_creds = get_google_credentials()
    weather = get_weather()
    events = get_calendar_events(google_creds, now)
    tasks = get_notion_tasks()
    
    # We unpack the tuple now
    recent_emails, email_count = get_recent_emails(google_creds)
    stoic_quote, email_summary = get_gemini_content(recent_emails)
    
    html_report = f"""
    <html>
      <body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
        <h2 style="color: #2c3e50;">Good Morning! Here is your daily overview.</h2>
        <p><strong>Date:</strong> {date_str}</p>
        <p><strong>Naperville Weather:</strong> {weather}</p>
        
        <hr style="border: 1px solid #eee;">
        <h3 style="color: #34495e;">🏛️ Daily Stoic Reflection</h3>
        <blockquote style="font-style: italic; background: #f9f9f9; padding: 10px; border-left: 5px solid #ccc;">
          {stoic_quote.replace(chr(10), '<br>')}
        </blockquote>
        
        <hr style="border: 1px solid #eee;">
        <h3 style="color: #34495e;">📅 Today's Schedule</h3>
        <ul>
            {"".join(f"<li>{e}</li>" for e in events)}
        </ul>
        
        <hr style="border: 1px solid #eee;">
        <h3 style="color: #34495e;">✅ Tasks (Do Today)</h3>
        <ul>
            {"".join(f"<li>{t}</li>" for t in tasks)}
        </ul>
        
        <hr style="border: 1px solid #eee;">
        <h3 style="color: #34495e;">📧 Email Triage</h3>
        <p><em>You have received <strong>{email_count}</strong> emails in your inbox over the last 24 hours.</em></p>
        <div>
            {email_summary.replace(chr(10), '<br>')}
        </div>
      </body>
    </html>
    """
    
    send_email(google_creds, html_report)

if __name__ == "__main__":
    main()