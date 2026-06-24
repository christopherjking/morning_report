import os
import json
import base64
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage

# SDK Imports
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai

# ==========================================
# CONFIGURATION & TODOs
# ==========================================
# TODO: Update with your actual email address
YOUR_EMAIL = "chris@christopherjking.com" 
# TODO: Update with your second calendar ID (find this in Google Calendar Settings)
CALENDAR_2_ID = "chrisanddanaking@gmail.com@group.calendar.google.com"

# Naperville, IL coordinates
LAT = 41.7508
LON = -88.1535
TIMEZONE = ZoneInfo("America/Chicago")

def get_google_credentials():
    """Decodes the base64 Google token from GitHub Secrets."""
    b64_token = os.environ.get("GOOGLE_TOKEN_B64")
    if not b64_token:
        raise ValueError("GOOGLE_TOKEN_B64 secret is missing.")
    
    token_json = base64.b64decode(b64_token).decode('utf-8')
    creds_dict = json.loads(token_json)
    return Credentials.from_authorized_user_info(creds_dict)

def get_weather():
    """Fetches today's weather forecast for Naperville via Open-Meteo."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&daily=weathercode,temperature_2m_max,temperature_2m_min&timezone=America%2FChicago"
    try:
        response = requests.get(url)
        data = response.json()
        daily = data.get("daily", {})
        max_temp = daily.get("temperature_2m_max", [None])[0]
        min_temp = daily.get("temperature_2m_min", [None])[0]
        return f"High of {max_temp}°C, Low of {min_temp}°C" # Open-Meteo defaults to Celsius, change params to get F if preferred
    except Exception as e:
        return f"Could not fetch weather: {e}"

def get_notion_tasks():
    """Fetches tasks from Notion where Status is 'Do Today'."""
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    notion = NotionClient(auth=notion_token)
    
    tasks = []
    try:
        # Note: If your Status field is a "Select" property instead of a "Status" property, 
        # change "status" to "select" below.
        query = notion.databases.query(
            **{
                "database_id": database_id,
                "filter": {
                    "property": "Status",
                    "status": {
                        "equals": "Do Today"
                    }
                }
            }
        )
        
        for page in query.get("results", []):
            # Assumes the title property is named "Name"
            title_prop = page["properties"].get("Name", {}).get("title", [])
            if title_prop:
                tasks.append(title_prop[0]["plain_text"])
                
        return tasks if tasks else ["No tasks marked 'Do Today'."]
    except Exception as e:
        return [f"Error fetching tasks: {e}"]

def get_calendar_events(creds, now):
    """Fetches today's events from the primary and secondary calendar."""
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
                # Clean up time string if it's a dateTime
                if 'T' in start:
                    time_obj = datetime.fromisoformat(start)
                    start = time_obj.strftime("%I:%M %p")
                events_list.append(f"- {start}: {event.get('summary', 'Busy')}")
        except Exception as e:
            events_list.append(f"Error reading calendar {cal_id}: {e}")
            
    return events_list if events_list else ["No events scheduled for today."]

def get_recent_emails(creds):
    """Fetches the subjects and snippets of emails from the last 24 hours."""
    service = build('gmail', 'v1', credentials=creds)
    try:
        # Query: newer than 1 day, exclude chats and promotional/automated categories if desired
        results = service.users().messages().list(userId='me', q='newer_than:1d -category:promotions').execute()
        messages = results.get('messages', [])
        
        email_data = []
        for msg in messages[:30]: # Limit to top 30 to save Gemini tokens
            msg_detail = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['Subject', 'From']).execute()
            headers = msg_detail.get('payload', {}).get('headers', [])
            
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
            snippet = msg_detail.get('snippet', '')
            
            email_data.append(f"From: {sender}\nSubject: {subject}\nSnippet: {snippet}\n")
            
        return email_data
    except Exception as e:
        return [f"Error fetching emails: {e}"]

def get_gemini_content(email_data):
    """Uses Gemini to generate the Stoic quote and summarize the emails."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    # 1. Stoic Quote
    stoic_prompt = (
        "Act as a Stoic philosopher in the style of Ryan Holiday. "
        "Provide a powerful, daily quote from Marcus Aurelius, Seneca, or Epictetus. "
        "Follow it with a 3-sentence modern, practical application for today."
    )
    stoic_response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=stoic_prompt
    )
    stoic_text = stoic_response.text if stoic_response else "Could not generate quote."
    
    # 2. Email Summary
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
        email_response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=email_prompt
        )
        email_summary = email_response.text if email_response else "Could not generate summary."
        
    return stoic_text, email_summary

def send_email(creds, html_content):
    """Sends the formatted HTML email via Gmail."""
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
    
    print("Loading Google Credentials...")
    google_creds = get_google_credentials()
    
    print("Fetching weather...")
    weather = get_weather()
    
    print("Fetching calendar events...")
    events = get_calendar_events(google_creds, now)
    
    print("Fetching Notion tasks...")
    tasks = get_notion_tasks()
    
    print("Fetching recent emails...")
    recent_emails = get_recent_emails(google_creds)
    
    print("Generating Gemini insights...")
    stoic_quote, email_summary = get_gemini_content(recent_emails)
    
    # Build the HTML Report
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
        <h3 style="color: #34495e;">📧 Email Triage (Last 24 Hours)</h3>
        <div>
            {email_summary.replace(chr(10), '<br>')}
        </div>
      </body>
    </html>
    """
    
    print("Sending report to inbox...")
    send_email(google_creds, html_report)
    print("Done!")

if __name__ == "__main__":
    main()