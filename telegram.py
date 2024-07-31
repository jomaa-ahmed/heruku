import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import logging
import os
from dotenv import load_dotenv
from flask import Flask, jsonify
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, ConversationHandler

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Get credentials and token from environment variables
EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

if not all([EMAIL_USER, EMAIL_PASS, TELEGRAM_TOKEN]):
    raise ValueError("Please set the EMAIL_USER, EMAIL_PASS, and TELEGRAM_TOKEN environment variables.")

# Initialize Telegram bot
bot = Bot(token=TELEGRAM_TOKEN)

# Conversation states
EMAIL, CHECK_EMAIL = range(2)

def start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('Please enter the password:')
    return EMAIL

def receive_email(update: Update, context: CallbackContext) -> int:
    password = update.message.text
    if password == "123":
        update.message.reply_text("Checking for codes or links...")
        check_for_codes_and_links(update, context)
    else:
        update.message.reply_text("Incorrect password. Please try again.")
        return EMAIL
    return CHECK_EMAIL

def check_for_codes_and_links(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id
    try:
        emails_info = get_emails_info()
        if emails_info:
            for email_info in emails_info:
                send_telegram_message(
                    chat_id,
                    email_info['profile_name'],
                    email_info['link'],
                    email_info['time_ago'],
                    email_info['type']
                )
        else:
            update.message.reply_text("No codes or links available.")
    except Exception as e:
        logging.error(f"An error occurred while checking emails: {e}")
        update.message.reply_text("An error occurred while checking emails.")
    finally:
        update.message.reply_text("Type /start to check again.")
    return ConversationHandler.END

def send_telegram_message(chat_id, profile_name, link, time_ago_str, email_type):
    message = f"👤 <b>Profile:</b> {profile_name}\n📄 <b>Type:</b> {email_type}\n⏰ <b>Time:</b> {time_ago_str}"
    button = InlineKeyboardButton(text="🔗 Click here", url=link)
    keyboard = InlineKeyboardMarkup([[button]])
    try:
        bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Failed to send message via Telegram: {e}")

def get_emails_info():
    try:
        # Connect to the server
        logging.info("Connecting to email server")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        logging.info("Connected to email server")

        # Select the mailbox you want to check
        mail.select("inbox")
        logging.info("Mailbox selected")

        # Search for emails with specific keywords in the subject
        status, messages = mail.search(
            None,
            '(OR (OR (HEADER Subject "temporary access code") (HEADER Subject "temporaire")) (OR (HEADER Subject "Netflix Household") (HEADER Subject "foyer")))'
        )

        if status != 'OK':
            logging.error(f"Error searching emails: {status}")
            return []

        messages = messages[0].split()
        logging.info(f"Found {len(messages)} emails matching the criteria")

        # Fetch all emails and store their dates and ids
        email_data = []
        for num in messages:
            res, msg = mail.fetch(num, "(RFC822)")
            for response in msg:
                if isinstance(response, tuple):
                    msg = email.message_from_bytes(response[1])
                    date = email.utils.parsedate_to_datetime(msg["Date"])
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    email_data.append((num, date, msg))

        # Sort emails from newest to oldest
        email_data.sort(key=lambda x: x[1], reverse=True)
        logging.info("Emails sorted by date")

        final_data = []
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)  # Adjust the time window for 7 days

        for num, date, msg in email_data:
            if date < seven_days_ago:
                logging.info(f"Skipping email from {date}, older than 7 days")
                continue

            subject, encoding = decode_header(msg["Subject"])[0]
            if isinstance(subject, bytes):
                subject = subject.decode(encoding if encoding else "utf-8")

            time_ago = (now - date).total_seconds()
            time_ago_str = (
                f"{int(time_ago // 60)} minutes ago" if time_ago < 3600 else
                f"{int(time_ago // 3600)} hours ago"
            )

            body = None
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if "attachment" not in content_disposition:
                        charset = part.get_content_charset()
                        payload = part.get_payload(decode=True)
                        if payload:
                            part_body = payload.decode(charset if charset else 'utf-8')
                            if content_type == "text/html":
                                body = part_body
                                break  # Prefer HTML part if available
                            elif content_type == "text/plain" and not body:
                                body = part_body  # Fallback to plain text if HTML is not found
            else:
                charset = msg.get_content_charset()
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(charset if charset else 'utf-8')

            if body:
                # Extract the profile name and code link
                profile_name = extract_profile_name(body)
                soup = BeautifulSoup(body, 'html.parser')
                link = soup.find('a', string=["Get Code", "Récupérer le code", "Yes, This Was Me", "Oui, c'était moi"])
                if link and link['href']:
                    email_info = {
                        "profile_name": profile_name if profile_name else "Unknown",
                        "link": link['href'],
                        "time_ago": time_ago_str,
                        "is_recent": True,
                        "type": "Temporary Access" if "Get Code" in link.text or "Récupérer le code" in link.text else "Household Update"
                    }
                    logging.info(f"Processed email: {email_info}")
                    final_data.append(email_info)

        logging.info(f"Processed {len(final_data)} recent emails")
        return final_data

    except imaplib.IMAP4.error as e:
        logging.error(f"IMAP4 error: {e}")
        return []
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return []

def extract_profile_name(body):
    # Use BeautifulSoup to parse the HTML content
    soup = BeautifulSoup(body, 'html.parser')
    
    # Look for patterns that likely contain the profile name in all cases
    requested_by = soup.find(string=lambda text: text and ("Requested by" in text or "Demande effectuée par" in text))
    if requested_by:
        # Extract the name following "Requested by" or "Demande effectuée par"
        start = requested_by.find("by") + len("by ") if "by" in requested_by else requested_by.find("par") + len("par ")
        end = requested_by.find(" ", start)
        if end == -1:
            end = len(requested_by)
        profile_name = requested_by[start:end].strip()
        return profile_name
    
    return None

app = Flask(__name__)

@app.route("/")
def index():
    profile_name_filter = None  # Adjust as needed
    emails_info = get_emails_info()
    return jsonify(emails_info)

def main() -> None:
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            EMAIL: [MessageHandler(Filters.text & ~Filters.command, receive_email)],
            CHECK_EMAIL: [MessageHandler(Filters.text & ~Filters.command, receive_email)]
        },
        fallbacks=[CommandHandler('start', start)],
    )

    dispatcher.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
