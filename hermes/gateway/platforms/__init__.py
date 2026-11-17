"""Messaging platform adapters (#42-#49)."""

from hermes.gateway.platforms.discord import DiscordAdapter
from hermes.gateway.platforms.email import EmailAdapter
from hermes.gateway.platforms.google_chat import GoogleChatAdapter
from hermes.gateway.platforms.signal import SignalAdapter
from hermes.gateway.platforms.slack import SlackAdapter
from hermes.gateway.platforms.teams import TeamsAdapter
from hermes.gateway.platforms.telegram import TelegramAdapter
from hermes.gateway.platforms.webhook import WebhookAdapter
from hermes.gateway.platforms.whatsapp import WhatsAppAdapter

ADAPTERS = {
    "telegram": TelegramAdapter,
    "discord": DiscordAdapter,
    "slack": SlackAdapter,
    "whatsapp": WhatsAppAdapter,
    "signal": SignalAdapter,
    "email": EmailAdapter,
    "teams": TeamsAdapter,
    "google_chat": GoogleChatAdapter,
    "webhook": WebhookAdapter,
}

# Env vars each platform needs (used by `hermes gateway setup` and `hermes doctor`).
PLATFORM_ENV = {
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"],
    "discord": ["DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USERS"],
    "slack": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS"],
    "whatsapp": ["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_ALLOWED_USERS"],
    "signal": ["SIGNAL_API_URL", "SIGNAL_NUMBER", "SIGNAL_ALLOWED_USERS"],
    "email": ["EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST", "EMAIL_ALLOWED_USERS"],
    "teams": ["TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_WEBHOOK_SECRET", "TEAMS_ALLOWED_USERS"],
    "google_chat": ["GOOGLE_CHAT_SECRET", "GOOGLE_CHAT_WEBHOOK_URL", "GOOGLE_CHAT_ALLOWED_USERS"],
    "webhook": ["WEBHOOK_SECRET", "WEBHOOK_CALLBACK_URL"],
}
