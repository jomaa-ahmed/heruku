import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import logging
from flask import Flask, request
from main import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackContext, Updater, ConversationHandler
from dotenv import load_dotenv
from threading import Thread
import requests
import time

# Load environment variables from .env file
load_dotenv()

# Retrieve environment variables
EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Ensure the environment variables are loaded correctly
if not all([EMAIL_USER, EMAIL_PASS, TELEGRAM_TOKEN]):
    raise ValueError("Missing one or more environment variables: EMAIL_USER, EMAIL_PASS, TELEGRAM_TOKEN")

# Initialize Telegram bot
bot = Bot(token=TELEGRAM_TOKEN)

# Set up Flask
app = Flask(__name__)

@app.route("/")
def home():
    return "Hello, this is the Telegram bot server."

# Conversation states
PROFILE_NAME = range(1)

def start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('👤 من فضلك أدخل اسم الملف الشخصي:')
    return PROFILE_NAME

def receive_profile_name(update: Update, context: CallbackContext) -> int:
    profile_name = update.message.text
    context.user_data['profile_name'] = profile_name
    update.message.reply_text(f"🔍 جاري التحقق من الروابط والأكواد لحساب {profile_name}...")
    emails_info = get_emails_info(profile_name)
    if emails_info:
        for email_info in emails_info:
            send_telegram_message(update.message.chat_id,
                                  email_info['profile_name'],
                                  email_info['link'], email_info['time_ago'],
                                  email_info['type'])
        update.message.reply_text("✅ تم الانتهاء. اكتب /start للبدء من جديد.")
    else:
        update.message.reply_text("❌ لا توجد أكواد أو روابط متاحة.")
    return ConversationHandler.END

def send_telegram_message(chat_id, profile_name, link, time_ago_str, email_type):
    message = f"👤 <b>الملف الشخصي:</b> {profile_name}\n📄 <b>النوع:</b> {email_type}\n⏰ <b>الوقت:</b> {time_ago_str}"
    button = InlineKeyboardButton(text="🔗 اضغط هنا", url=link)
    keyboard = InlineKeyboardMarkup([[button]])
    try:
        bot.send_message(chat_id=chat_id,
                         text=message,
                         parse_mode='HTML',
                         reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Failed to send message via Telegram: {e}")

def get_emails_info(profile_name_filter=None):
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
                f"{int(time_ago // 60)} دقائق مضت" if time_ago < 3600 else
                f"{int(time_ago // 3600)} ساعات مضت"
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
                if profile_name_filter and profile_name_filter.lower() not in profile_name.lower():
                    continue
                soup = BeautifulSoup(body, 'html.parser')
                link = soup.find('a', string=["Get Code", "Récupérer le code", "Yes, This Was Me", "Confirm Update"])
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
        # Extract the name following "by" or "par"
        start = requested_by.find("by") + len("by ") if "by" in requested_by else requested_by.find("par") + len("par ")
        end = requested_by.find(" ", start)
        if end == -1:
            end = len(requested_by)
        profile_name = requested_by[start:end].strip()
        return profile_name

    return "Unknown"

# Function to run the Flask app
def run_flask():
    app.run(host="0.0.0.0", port=8080)

# Function to run the Telegram bot
def run_telegram():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PROFILE_NAME: [
                MessageHandler(Filters.text & ~Filters.command,
                               receive_profile_name)
            ],
        },
        fallbacks=[CommandHandler('start', start)],
    )

    dispatcher.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()

# Start both Flask app and Telegram bot in separate threads
if __name__ == "__main__":
    Thread(target=run_flask).start()
    Thread(target=run_telegram).start()
