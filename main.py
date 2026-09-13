import os
import sys
import time
import socket
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser

import feedparser
import gspread
import markdown
from google import genai
from google.oauth2.service_account import Credentials


FEEDS = [
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("Hugging Face",   "https://huggingface.co/blog/feed.xml"),
    ("VentureBeat AI", "https://venturebeat.com/ai/feed/"),
    ("TechCrunch AI",  "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("OpenAI News",    "https://openai.com/news/rss.xml"),
    ("Anthropic",      "https://www.anthropic.com/rss.xml"),
    ("arXiv cs.AI",    "https://export.arxiv.org/rss/cs.AI"),
    ("Ollama Blog",    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_ollama.xml"),
]

GEMINI_MODEL     = "gemini-2.5-flash"
USER_AGENT       = "Mozilla/5.0 (compatible; AIDigestBot/1.0; +https://github.com/you/repo)"
SHEET_NAME       = "AI Digest"
ITEMS_SHEET      = "Items"
DIGESTS_SHEET    = "Digests"
ARXIV_LIMIT      = 30
CHUNK_MAX_CHARS  = 240_000
CHUNK_DELAY_SECS = 6
MAX_RETRIES      = 3
FEED_TIMEOUT_S   = 30

GOOGLE_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def strip_html(raw: str) -> str:
    s = HTMLStripper()
    s.feed(raw or "")
    return " ".join(s.text).strip()


def get_entry_body(entry) -> str:
    raw = entry.get("summary") or ""
    if not raw and entry.get("content"):
        try:
            raw = entry["content"][0].get("value", "")
        except (IndexError, AttributeError, TypeError):
            raw = ""
    return strip_html(raw)


def fetch_items() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    items: list[dict] = []
    arxiv_count = 0

    for name, url in FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)

            if feed.bozo and not feed.entries:
                print(f"[WARN] Skipping '{name}': {feed.bozo_exception}")
                continue

            for entry in feed.entries:
                if name == "arXiv cs.AI":
                    if arxiv_count >= ARXIV_LIMIT:
                        break
                    arxiv_count += 1

                parsed = (
                    entry.get("published_parsed")
                    or entry.get("updated_parsed")
                )
                if not parsed:
                    continue

                published = datetime(*parsed[:6], tzinfo=timezone.utc)
                if published < cutoff:
                    continue

                items.append({
                    "date":    published.strftime("%Y-%m-%d"),
                    "source":  name,
                    "title":   entry.get("title", "Untitled").strip(),
                    "link":    entry.get("link", ""),
                    "summary": get_entry_body(entry),
                })

        except Exception as exc:
            print(f"[ERROR] Feed '{name}' failed unexpectedly: {exc}")
            continue

    return items


def chunk_items(items: list[dict], max_chars: int = CHUNK_MAX_CHARS):
    chunk: list[dict] = []
    size = 0
    for item in items:
        text = f"{item['title']} {item['link']} {item['summary']}"
        if size + len(text) > max_chars and chunk:
            yield chunk
            chunk, size = [], 0
        chunk.append(item)
        size += len(text)
    if chunk:
        yield chunk


def gemini_with_retry(client: genai.Client, prompt: str):
    delay = 5
    for attempt in range(MAX_RETRIES):
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            print(
                f"[WARN] Gemini attempt {attempt + 1}/{MAX_RETRIES} failed: {exc}. "
                f"Retrying in {delay}s."
            )
            time.sleep(delay)
            delay *= 2


def summarise(items: list[dict]) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    chunks = list(chunk_items(items))
    summaries: list[str] = []

    for i, chunk in enumerate(chunks):
        payload = "\n\n".join(
            f"Title: {item['title']}\n"
            f"Link: {item['link']}\n"
            f"Summary: {item['summary']}"
            for item in chunk
        )

        prompt = (
            "You are an AI news curator for a solo founder building AI agents "
            "on cheap, low-resource hardware in Nigeria.\n"
            "Summarize these AI news items into a concise, practical digest.\n"
            "Rules:\n"
            "- Group items under exactly 3 sections:\n"
            "  Must-Know | Tools & Releases | Research Worth Reading\n"
            "- Embed each article link as an inline markdown link after the title\n"
            "- Prioritise: local AI, edge models, open-source releases, "
            "free tools, cheap inference, LangGraph, multi-agent systems, on-device AI\n"
            "- Drop pure marketing fluff with no technical substance\n"
            "- Max 800 words total\n\n"
            f"{payload}"
        )

        try:
            resp = gemini_with_retry(client, prompt)
            summaries.append(resp.text)
        except Exception as exc:
            print(f"[ERROR] Chunk {i + 1}/{len(chunks)} permanently failed: {exc}")

        if i < len(chunks) - 1:
            time.sleep(CHUNK_DELAY_SECS)

    return "\n\n---\n\n".join(summaries)


def get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet,
    title: str,
    headers: list[str],
) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=title, rows=1000, cols=len(headers)
        )
        ws.append_row(headers)
        return ws

    if not any(ws.row_values(1)):
        ws.append_row(headers)

    return ws


def save_to_sheet(items: list[dict], digest: str) -> None:
    creds = Credentials.from_service_account_file(
        "credentials.json", scopes=GOOGLE_SCOPES
    )
    client = gspread.authorize(creds)
    sh = client.open(SHEET_NAME)

    items_ws = get_or_create_worksheet(
        sh, ITEMS_SHEET, ["Date", "Source", "Title", "Link"]
    )

    existing = set(items_ws.col_values(4))
    new_rows = [
        [item["date"], item["source"], item["title"], item["link"]]
        for item in items
        if item["link"] and item["link"] not in existing
    ]
    if new_rows:
        items_ws.append_rows(new_rows)

    digest_ws = get_or_create_worksheet(
        sh, DIGESTS_SHEET, ["Date", "Items Processed", "Digest"]
    )
    digest_ws.append_row([
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        len(items),
        digest,
    ])


def send_email(digest: str) -> None:
    sender    = os.environ["GMAIL_USER"]
    password  = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    html_digest = markdown.markdown(digest)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"AI Digest - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(digest, "plain", "utf-8"))
    msg.attach(MIMEText(html_digest, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
        print("[INFO] Email sent successfully.")
    except Exception as exc:
        print(f"::error::Email delivery failed: {exc}")
        sys.exit(1)


def main() -> None:
    print(f"[START] AI Digest - {datetime.now(timezone.utc).isoformat()}")

    socket.setdefaulttimeout(FEED_TIMEOUT_S)

    items = fetch_items()
    if not items:
        print("::error::No items fetched from any feed")
        sys.exit(1)
    print(f"[INFO] Fetched {len(items)} items across {len(FEEDS)} feeds.")

    digest = summarise(items)
    if not digest.strip():
        print("::error::Gemini returned an empty digest")
        sys.exit(1)
    print("[INFO] Digest generated.")

    save_to_sheet(items, digest)
    print("[INFO] Saved to Google Sheets.")

    send_email(digest)
    print(f"[DONE] {len(items)} items processed.")


if __name__ == "__main__":
    main()
