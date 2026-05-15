import smtplib
from email.message import EmailMessage
from signal_system import config


def send_email(subject: str, body: str) -> None:
    """Send an email via Gmail SMTP.

    Args:
        subject: Email subject line
        body: Plain text email body

    Raises:
        smtplib.SMTPException: If SMTP operations fail
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_USERNAME
    msg["To"] = config.ALERT_RECIPIENT_EMAIL
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(config.GMAIL_USERNAME, config.GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
