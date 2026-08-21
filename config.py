#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Standalone configuration for the Agent Inbox library.

This file is a thin, OPTIONAL convenience wrapper around agent_inbox.py.
It loads settings from environment variables (and, for local/manual installs,
from a .env file in this folder), then hands you a ready-to-use AgentInbox.

The AgentInbox class itself takes credentials as constructor arguments and has
NO dependency on this file — you can import agent_inbox.py directly and wire it
up however your own agent harness likes. This config just makes the common
"clone, edit .env, run" path easy for a human installer.

Secrets live in the environment (or .env) ONLY. Never commit them.
"""

import os
from pathlib import Path


# ---- Minimal .env loader (stdlib only, no python-dotenv needed) ----------
# Reads KEY=VALUE lines from ./.env into os.environ, but NEVER overwrites a
# variable that is already set (so a CI/CD secret or shell export always wins).
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


# ---- Settings -------------------------------------------------------------
GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "").strip() or None

# Where the email archive lives. This is the ONE path the end user must decide:
# it can be a dedicated private repo, a folder inside their existing private
# repo, or a folder inside a public repo (NOT recommended — emails are private).
# The library creates record/emails/ (inbox/, sent/, drafts/, outbox/, failed/,
# index.json) underneath whatever path you give it.
EMAIL_ARCHIVE_REPO_PATH = os.environ.get("EMAIL_ARCHIVE_REPO_PATH", "").strip()

AGENT_NAME = os.environ.get("AGENT_NAME", "AI Agent").strip()
TIMEZONE = os.environ.get("TIMEZONE", "America/Los_Angeles").strip()


# ---- Validation -----------------------------------------------------------
def build_inbox():
    """Return a configured AgentInbox, or raise if required values are missing."""
    missing = []
    if not GMAIL_EMAIL:
        missing.append("GMAIL_EMAIL")
    if not GMAIL_APP_PASSWORD:
        missing.append("GMAIL_APP_PASSWORD")
    if not EMAIL_ARCHIVE_REPO_PATH:
        missing.append("EMAIL_ARCHIVE_REPO_PATH")
    if missing:
        raise RuntimeError(
            "Missing required Agent Inbox settings: " + ", ".join(missing) +
            ". Copy .env.example to .env and fill them in (or export them)."
        )

    from agent_inbox import AgentInbox
    return AgentInbox(
        email_address=GMAIL_EMAIL,
        app_password=GMAIL_APP_PASSWORD,
        private_repo_path=EMAIL_ARCHIVE_REPO_PATH,
        operator_email=OPERATOR_EMAIL,
        agent_name=AGENT_NAME,
        timezone=TIMEZONE,
    )


if __name__ == "__main__":
    # Quick smoke test: fetch new mail + retry the outbox.
    inbox = build_inbox()
    inbox.fetch_unread_and_store()
    inbox.retry_outbox()
