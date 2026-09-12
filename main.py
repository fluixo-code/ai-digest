import os
import feedparser
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

# News feeds to process
NEWS_FEEDS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.cnbc.com/cnbc/world/",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]

def fetch_news(feeds):
    """Fetch latest news from RSS feeds."""
    articles = []
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:  # Get top 5 from each feed
                articles.append({
                    "title": entry.get("title", "N/A"),
                    "link": entry.get("link", "N/A"),
                    "published": entry.get("published", "N/A"),
                    "summary": entry.get("summary", "N/A"),
                })
        except Exception as e:
            print(f"Error fetching feed {feed_url}: {e}")
    return articles

def summarize_with_gemini(article_text):
    """Summarize article using Gemini API."""
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-pro")
        response = model.generate_content(
            f"Please provide a brief 2-3 sentence summary of this news article:\n\n{article_text}"
        )
        return response.text
    except Exception as e:
        print(f"Error summarizing with Gemini: {e}")
        return "Summary unavailable"

def append_to_google_sheets(data):
    """Append data to Google Sheets."""
    try:
        # Load service account credentials
        credentials = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_JSON,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        service = build('sheets', 'v4', credentials=credentials)
        
        # Prepare the data
        values = []
        for article in data:
            values.append([
                article["title"],
                article["summary"],
                article["link"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ])
        
        # Append to sheet
        body = {'values': values}
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="Sheet1!A1",
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
        print(f"Successfully appended {len(values)} rows to Google Sheets")
    except Exception as e:
        print(f"Error appending to Google Sheets: {e}")

def send_email_digest(articles):
    """Send email digest of summarized articles."""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = EMAIL_TO
        msg['Subject'] = f"AI News Digest - {datetime.now().strftime('%Y-%m-%d')}"
        
        # Create HTML body
        html_body = "<html><body><h1>Today's AI News Digest</h1><hr>"
        for article in articles:
            html_body += f"""
            <h3>{article['title']}</h3>
            <p><strong>Summary:</strong> {article['summary']}</p>
            <p><a href="{article['link']}">Read full article</a></p>
            <hr>
            """
        html_body += "</body></html>"
        
        msg.attach(MIMEText(html_body, 'html'))
        
        # Send email
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"Email digest sent to {EMAIL_TO}")
    except Exception as e:
        print(f"Error sending email: {e}")

def main():
    """Main function to orchestrate the news digest."""
    print("Starting AI News Digest...")
    
    # Fetch news
    articles = fetch_news(NEWS_FEEDS)
    print(f"Fetched {len(articles)} articles")
    
    # Summarize each article
    for article in articles:
        article['summary'] = summarize_with_gemini(
            f"{article['title']}\n{article['summary']}"
        )
    
    # Append to Google Sheets
    append_to_google_sheets(articles)
    
    # Send email digest
    send_email_digest(articles)
    
    print("AI News Digest completed successfully!")

if __name__ == "__main__":
    main()
