#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================================
#  SECRETS — you must set these two as SECRETS (never write them in this file)
# =========================================================================
#  1. GMAIL_APP_PASSWORD — a Gmail "App Password" (16 characters) for the
#     agent's Gmail account (NOT the account's normal password).
#     To create one: turn on 2-Step Verification, enable IMAP, then
#     Google Account → Security → App passwords → create one for "Mail".
#
#  2. OPERATOR_EMAIL — the operator's email address that receives the optional
#     daily digest. (Optional — only needed if you use digests.)
#
#  Where to set them:
#    - GitHub Actions: repo Settings → Secrets and variables → Actions
#    - Local: export them as environment variables before running your script,
#      e.g.  export GMAIL_APP_PASSWORD="your-16-char-password"
# =========================================================================

import os

# =========================================================================
#  agent_email_config.py — edit these, that's all you have to touch
# =========================================================================
GMAIL_EMAIL            = "your-agent@gmail.com"      # the Gmail address your agent checks
EMAIL_ARCHIVE_REPO_PATH = "/path/to/archive"         # where emails get saved
AGENT_NAME             = "My Agent"
TIMEZONE              = "America/Los_Angeles"

# =========================================================================
#  SECRETS — read from the environment. Do NOT edit these lines.
# =========================================================================
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL") or None


# =========================================================================
#  You do NOT need to change anything below this line.
# =========================================================================
def build_inbox():
    """Return a ready-to-use AgentInbox built from the settings above."""
    if not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD is not set. Set it as a SECRET "
            "(environment variable) — see the instructions at the top of this file."
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
    # Quick test: fetch new mail, then retry anything stuck in the outbox.
    inbox = build_inbox()
    inbox.fetch_unread_and_store()
    inbox.retry_outbox()
