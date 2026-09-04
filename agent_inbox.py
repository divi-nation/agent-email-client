#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent Inbox – A reusable email module for AI agents.

This module provides a simple, robust interface for:
- Fetching unread emails from Gmail (IMAP)
- Storing them as Markdown files with YAML frontmatter
- Indexing them in a searchable JSON index
- Sending emails via Gmail SMTP with Outbox retry system
- Saving drafts
- Full‑text search across email bodies
- Thread retrieval (walk In-Reply-To/References)
- Labels/tags + list/enumerate helpers
- CC / BCC and multiple-recipient support
- Address validation (warn, not block) for hallucinated/typo'd addresses
- HTML-only email body fallback
- Robust error handling for SMTP errors (address not found, auth, network, etc.)
- Managing an email archive in a private Git repository

Dependencies:
    - Python 3.9+ (standard library only: imaplib, smtplib, email, json, os, re, time, datetime)
    - No pip installs required.

Environment Variables (optional if you pass credentials directly):
    GMAIL_EMAIL        – your Gmail address (e.g., "agent@example.com")
    GMAIL_APP_PASSWORD – an App Password generated from Gmail (not your regular password)
    OPERATOR_EMAIL     – email address to receive session digests and error notifications

License: MIT
"""

import os
import json
import re
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, parsedate_to_datetime, make_msgid
from email.header import decode_header, make_header
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import socket


# Domains that are placeholders or examples. An address at one of these is
# almost always invented rather than real, so sending to it is refused and the
# agent is told which address was refused and why.
RESERVED_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "localhost", "test.com", "invalid", "domain.invalid",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


# =====================================================================
# AGENT-FACING INSTRUCTIONS
# =====================================================================
# This text teaches an AI agent what email capabilities it has. Import it and
# append it to the agent's prompt so the agent "knows" it can use these methods.
# It is a plain string — importing it has no side effects (nothing prints).
# For anyone using this file as a standalone library: append this to your own
# prompt so your agent knows it can send and search mail. Step 5 of the README.
# An engine that describes these tools itself will not need it.
# Setting a letter aside is common enough to be worth one agreed name. A label
# an agent invents is one it has to remember inventing; a label the library
# knows about can be put back in front of it every session without being asked.
REVIEW_LATER = "review later"

AGENT_TOOL_INSTRUCTIONS = """You have an email inbox (via the `inbox` object). Available methods:
- inbox.fetch_unread_and_store() # pull new mail from Gmail
- inbox.search_emails("query") # search all mail (headers + body)
- inbox.get_thread("<message-id>") # whole conversation, oldest first
- inbox.send_email(to, subject, body) # send; returns (ok, error)
- inbox.save_draft(to, subject, body) # save a draft
- inbox.list_emails(status="unread") # list messages (summaries)
- inbox.mark_email_read("msg_001") # mark one dealt with (replying does this for you)
- inbox.add_label("msg_001", "label") # tag an email
- inbox.remove_label("msg_001", "label") # remove a tag
- inbox.set_aside("msg_001") # come back to this letter another day
- inbox.pick_up("msg_001") # take it back up: off the pile, into your unread
- inbox.list_labels() # every label in use, and how many carry it
- inbox.list_drafts() # list drafts
- inbox.list_outbox() # list messages awaiting retry
- inbox.list_by_label("label") # list emails with a label
- inbox.retry_outbox() # retry failed sends

Answering a letter is not the only honest response, and deciding not to answer
is not the only alternative. You may set one down and come back to it:
`inbox.set_aside("msg_001")`. It is put back in front of you, with who wrote
and when, until you answer it or `inbox.pick_up` it. Nothing expires and
nothing nags beyond that line.
"""


def _extract_email_address(value):
    """Pull a bare address out of 'Name <addr>' or a raw addr."""
    if not value:
        return ""
    v = str(value).strip()
    if "<" in v and ">" in v:
        v = v[v.rfind("<") + 1:v.rfind(">")].strip()
    return v


def validate_email_address(value):
    """Return (is_valid, reason). Warn-level check: syntax + reserved/example domains.
    Deliberately does NOT do SMTP delivery checks (the outbox retry already handles
    real delivery failures)."""
    addr = _extract_email_address(value)
    if not addr or "@" not in addr:
        return False, f"'{value}' is not a valid email address"
    if not _EMAIL_RE.match(addr):
        return False, f"'{addr}' fails email syntax — check for typos"
    domain = addr.rsplit("@", 1)[1].lower()
    if domain in RESERVED_EMAIL_DOMAINS:
        return False, (f"'{addr}' uses the reserved/example domain '{domain}' — "
                       f"this is almost certainly not a real address. Verify against "
                       f"search results or your directory before sending.")
    return True, ""


def _decode_header(value):
    """Return a header as text, whatever encoding it arrived in.

    A header carrying anything but plain ASCII — an accent, an em dash, an
    emoji — is transmitted encoded, as `=?UTF-8?Q?...?=`. Stored that way it is
    unreadable, and it does not match a search for the words it contains."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        # A malformed header is worth keeping as it came rather than losing.
        return str(value).strip()


# An attachment small enough, and text enough, to keep beside the letter. The
# bytes of anything else are left in the mail account: both repositories are
# cloned at the start of every session and git keeps a file for good, so storing
# one PDF is a cost on every run from then on, to hold a second copy of something
# the mail account already holds reliably.
ATTACHMENT_TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".yaml", ".yml",
                            ".log", ".ini", ".toml")
MAX_STORED_ATTACHMENT_BYTES = 32000
# One message cannot fill the index with its own attachment list.
MAX_ATTACHMENTS_LISTED = 10


def _is_attachment(part):
    """Is this part a file someone attached, rather than the letter itself?

    A part with no filename is the message. One marked `inline` is usually a
    signature logo or a tracking pixel, and listing those would bury the
    attachments that were meant on every letter sent from an office."""
    if part.get_content_maintype() == "multipart":
        return False
    return bool(part.get_filename()) and part.get_content_disposition() != "inline"


def _readable_as_text(filename, content_type):
    """Can this be kept as words, rather than as bytes nothing here can read?"""
    if str(content_type or "").startswith("text/"):
        return True
    return str(filename or "").lower().endswith(ATTACHMENT_TEXT_SUFFIXES)


def _body_parts(msg, _root=True):
    """The parts of a message that are the letter, not something attached to it.

    `email`'s own walk() descends into an attached message and hands back its
    text as though it belonged here — so a forwarded email supplies the body and
    whatever the sender wrote above it is thrown away. This stops at anything
    attached instead of going through it."""
    if not _root and (_is_attachment(msg)
                      or msg.get_content_maintype() == "message"):
        return
    yield msg
    if msg.is_multipart():
        payload = msg.get_payload()
        if isinstance(payload, list):
            for part in payload:
                yield from _body_parts(part, _root=False)


def _reference_ids(value):
    """The Message-IDs listed in a References header, oldest first."""
    return [m for m in str(value or "").split() if m.startswith("<")]


def _parent_of(entry, by_msgid):
    """The letter this one is answering, as far as the archive can tell.

    In-Reply-To names the direct parent. When that one is not held — it predates
    the agent, or was sent from somewhere else — References lists the rest of the
    conversation, and the nearest of those that IS held keeps this in the same
    thread instead of starting a second one."""
    direct = entry.get("in_reply_to")
    if direct and direct in by_msgid:
        return direct
    for mid in reversed(_reference_ids(entry.get("references"))):
        if mid in by_msgid:
            return mid
    return None


def _human_size(n):
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def describe_attachments(attachments):
    """The lines shown to the agent for what came with a letter.

    Says of each one whether it can be read, and where. Nothing is put in front
    of the agent unasked: a file that was kept says where it is, and the agent
    reads it if it wants it."""
    if not attachments:
        return ""
    lines = ["Attachments:"]
    for a in attachments[:MAX_ATTACHMENTS_LISTED]:
        size = _human_size(a.get("size"))
        if a.get("saved"):
            lines.append(f"  {a.get('name')} ({size}) — saved at {a['saved']}, "
                         f"`read_file` it if you want it")
        else:
            lines.append(f"  {a.get('name')} ({size}, {a.get('type')}) — kept in "
                         f"your mail account; it cannot be read here")
    return "\n".join(lines)


# How much of one letter is put in front of an agent without being asked for.
# The whole letter is always saved; this is the prompt, not the record.
MAX_EMAIL_BODY_CHARS = 12000


def labels_line(labels):
    """The one line saying how a letter is filed, or "" when it is not.

    Shown with the letter itself. A label the agent set and is never shown
    again is worse than no label: it feels like something was done."""
    kept = [str(l).strip() for l in (labels or []) if str(l).strip()]
    if not kept:
        return ""
    return "Filed as: " + ", ".join(kept)


def trim_for_prompt(body, file_rel, limit=MAX_EMAIL_BODY_CHARS):
    """Shorten a letter for the prompt, saying where the rest of it is.

    A newsletter can carry a hundred thousand characters, and five of them would
    spend a session's whole budget before the agent had done anything."""
    body = body or ""
    if len(body) <= limit:
        return body
    kept = body[:limit].rstrip()
    return (f"{kept}\n\n[The letter goes on for {len(body) - len(kept)} more "
            f"characters. The whole of it is at record/emails/{file_rel} — "
            f"`read_file` it if you want the rest.]")


def _decode_part(part):
    """Return an email part's text. Empty string if there is nothing to read."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        return ""
    return payload.decode("utf-8", errors="ignore")


def _html_to_text(html):
    """Minimal stdlib HTML→text: strip tags, unescape common entities, collapse blanks."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class AgentInbox:
    """
    A handler for AI agents to manage emails with a private Git repository.
    """

    def __init__(self, email_address, app_password, private_repo_path, 
                 operator_email=None, agent_name="AI Agent", timezone="America/Los_Angeles"):
        """
        Initialize the AgentInbox.

        Args:
            email_address (str): Gmail address to use.
            app_password (str): Gmail App Password (not regular password).
            private_repo_path (str): Absolute path to the root of the private repository.
            operator_email (str, optional): Email address for digests and error reports.
            agent_name (str, optional): Name of the agent (used in digests). Default: "AI Agent".
            timezone (str): IANA timezone name (default: "America/Los_Angeles").
        """
        self.email_address = email_address
        self.app_password = app_password
        self.private_repo_path = Path(private_repo_path)
        self.operator_email = operator_email
        self.agent_name = agent_name
        self.timezone = ZoneInfo(timezone)
        # Mail fetched with mark_seen=False waits here to be marked read on the
        # server once the caller has safely saved it. See mark_pending_seen().
        self._pending_seen_uids = []
        self._ensure_directories()

    def _ensure_directories(self):
        """Create required subdirectories inside the private repo."""
        (self.private_repo_path / "record" / "emails" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.private_repo_path / "record" / "emails" / "sent").mkdir(parents=True, exist_ok=True)
        (self.private_repo_path / "record" / "emails" / "drafts").mkdir(parents=True, exist_ok=True)
        (self.private_repo_path / "record" / "emails" / "outbox").mkdir(parents=True, exist_ok=True)
        (self.private_repo_path / "record" / "emails" / "failed").mkdir(parents=True, exist_ok=True)

    # ---------- Index Management ----------
    def _index_path(self):
        return self.private_repo_path / "record" / "emails" / "index.json"

    def load_index(self):
        """Load the email index from index.json."""
        index_path = self._index_path()
        if index_path.exists():
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return []
        return []

    def save_index(self, index):
        """Save the email index to index.json."""
        with open(self._index_path(), "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)

    def _generate_email_id(self, existing_ids):
        """Generate a unique message ID for new emails."""
        nums = []
        for eid in existing_ids:
            # An entry with a missing or unexpected id must not stop new mail
            # from being saved, so anything unreadable is skipped.
            if not isinstance(eid, str) or not eid.startswith("msg_"):
                continue
            try:
                nums.append(int(eid.split("_")[1]))
            except (IndexError, ValueError):
                continue
        if nums:
            return f"msg_{max(nums) + 1:03d}"
        return "msg_001"

    def reference_chain(self, in_reply_to):
        """The References header for a reply: the conversation so far, then the
        letter being answered. Empty when this is not a reply."""
        if not in_reply_to:
            return None
        parent = next((e for e in self.load_index()
                       if e.get("message_id") == in_reply_to), None)
        earlier = _reference_ids(parent.get("references")) if parent else []
        return " ".join(earlier + [in_reply_to])

    def new_message_id(self):
        """A Message-ID for a letter about to be sent.

        Built from the sending address's own domain. The default would use this
        machine's hostname, which says where the agent runs to everyone it
        writes to."""
        domain = (self.email_address or "").rsplit("@", 1)[-1] or "localhost"
        return make_msgid(domain=domain)

    def _sanitize_filename(self, text):
        """Clean up a string for safe use in a filename."""
        return re.sub(r'[^a-zA-Z0-9_.-]', '_', text)[:80]

    def _store_attachments(self, msg, date_local):
        """Note what was attached, and keep the small text ones beside the letter.

        Returns a list of {name, type, size} — with `saved` naming a path for
        anything kept. The bytes of a PDF or an image are not stored; see the
        note beside MAX_STORED_ATTACHMENT_BYTES."""
        out = []
        if not msg.is_multipart():
            return out
        stamp = date_local.strftime("%Y-%m-%d_%H-%M-%S")
        for part in msg.walk():
            if len(out) >= MAX_ATTACHMENTS_LISTED:
                break
            if not _is_attachment(part):
                continue
            name = _decode_header(part.get_filename()) or "attachment"
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            size = len(payload)
            if not size:
                # An attached email decodes to nothing, because its payload is
                # a message rather than bytes. Reporting "0 B" would read as an
                # empty file rather than one this cannot open.
                try:
                    size = len(part.as_bytes())
                except Exception:
                    size = 0
            record = {"name": name, "type": ctype, "size": size}
            if (_readable_as_text(name, ctype) and payload
                    and len(payload) <= MAX_STORED_ATTACHMENT_BYTES):
                safe = self._sanitize_filename(name) or "attachment"
                path = (self.private_repo_path / "record" / "emails"
                        / "attachments" / f"{stamp}_{safe}")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(payload.decode("utf-8", errors="ignore"),
                                    encoding="utf-8")
                    # Relative to the repository root, which is how the agent
                    # asks for a file to be read.
                    record["saved"] = f"record/emails/attachments/{stamp}_{safe}"
                except Exception as e:
                    print(f"⚠️ Could not save attachment {name}: {e}")
            out.append(record)
        return out

    def _save_email_file(self, folder, filename, content, frontmatter):
        """Write an email file with YAML frontmatter."""
        lines = ["---"]
        for key, value in frontmatter.items():
            if value is not None:
                if isinstance(value, str):
                    value = value.replace('"', '\\"')
                    lines.append(f'{key}: "{value}"')
                else:
                    # JSON, not repr: JSON is valid YAML, and a list of objects
                    # holding a filename with a quote in it has to survive being
                    # written down.
                    lines.append(f'{key}: {json.dumps(value)}')
        lines.append("---")
        lines.append("")
        lines.append(content)

        file_path = self.private_repo_path / "record" / "emails" / folder / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return f"{folder}/{filename}"

    # ---------- Fetch Incoming Emails ----------
    def fetch_unread_and_store(self, mark_seen=True):
        """Fetch unread emails from Gmail, save to inbox/, and update index.

        mark_seen=True marks each email as read on the server as it is saved.

        mark_seen=False leaves the mail unread on the server and remembers it
        instead; call mark_pending_seen() once the saved mail is safely stored.
        Use this when the mail is being saved somewhere that might not survive,
        such as a copy of a repository that has not been committed yet. Mail that
        is marked read but then lost cannot be fetched again, because only unread
        mail is collected.
        """
        if not self.email_address or not self.app_password:
            print("⚠️ Email credentials missing. Skipping fetch.")
            return

        print("📬 Fetching unread emails from Gmail...")
        index = self.load_index()
        existing_ids = [entry.get("id") for entry in index]
        seen_message_ids = {entry.get("message_id") for entry in index
                            if entry.get("message_id")}
        new_count = 0

        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self.email_address, self.app_password)
            mail.select("inbox")

            # UIDs rather than sequence numbers: a UID keeps pointing at the same
            # message on a later connection, which is what mark_pending_seen needs.
            status, data = mail.uid("SEARCH", None, "UNSEEN")
            if status != "OK":
                print("⚠️ No unread emails found.")
                mail.close()
                mail.logout()
                return

            email_uids = data[0].split()
            print(f"📬 Found {len(email_uids)} unread email(s).")

            for uid in email_uids:
                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)

                def remember_seen():
                    """Mark this email read now, or note it for later."""
                    if mark_seen:
                        mail.uid("STORE", uid, "+FLAGS", "\\Seen")
                    else:
                        self._pending_seen_uids.append(uid_str)

                # BODY.PEEK[] rather than RFC822: asking for RFC822 marks the
                # message read on the server as a side effect of reading it,
                # which would defeat mark_seen=False and lose any mail whose
                # session then failed. PEEK is the same bytes without the flag.
                status, msg_data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Skip anything already saved. Mail with no Message-ID cannot be
                # compared this way, so it is always treated as new rather than
                # being mistaken for a copy of the last one that also had none.
                msg_id = msg.get("Message-ID", "").strip()
                if msg_id and msg_id in seen_message_ids:
                    print(f"⏭️ Skipping duplicate email: {msg_id}")
                    # Already saved, so stop it coming back on every fetch.
                    remember_seen()
                    continue

                # Parse headers
                subject = _decode_header(msg.get("Subject")) or "(no subject)"
                from_addr = _decode_header(msg.get("From")) or "unknown"
                date_str = msg.get("Date", "")
                try:
                    date_dt = parsedate_to_datetime(date_str)
                    if date_dt:
                        date_local = date_dt.astimezone(self.timezone)
                    else:
                        date_local = datetime.now(self.timezone)
                except Exception:
                    date_local = datetime.now(self.timezone)

                # Extract body: prefer text/plain, fall back to text/html (strip tags)
                body = ""
                if msg.is_multipart():
                    text_part = None
                    html_part = None
                    # An attached file is not the letter. Without this a
                    # forwarded message supplies the body and whatever the
                    # sender wrote above it is thrown away.
                    for part in _body_parts(msg):
                        if part.get_content_type() == "text/plain":
                            text_part = part
                            break
                    if text_part is None:
                        for part in _body_parts(msg):
                            if part.get_content_type() == "text/html":
                                html_part = part
                                break
                    if text_part:
                        body = _decode_part(text_part)
                    elif html_part:
                        body = _html_to_text(_decode_part(html_part))
                else:
                    payload = _decode_part(msg)
                    if msg.get_content_type() == "text/html":
                        body = _html_to_text(payload)
                    else:
                        body = payload

                attachments = self._store_attachments(msg, date_local)

                # Build frontmatter
                frontmatter = {
                    "direction": "incoming",
                    "status": "unread",
                    "date": date_local.isoformat(),
                    "from": from_addr,
                    "subject": subject,
                    "message_id": msg_id,
                    "in_reply_to": msg.get("In-Reply-To", "").strip() or None,
                    "references": msg.get("References", "").strip() or None,
                    "labels": [],
                    "attachments": attachments,
                }

                new_id = self._generate_email_id(existing_ids)
                frontmatter["id"] = new_id
                existing_ids.append(new_id)

                date_str_filename = date_local.strftime("%Y-%m-%d_%H-%M-%S")
                sender_slug = self._sanitize_filename(from_addr.split('<')[0].strip() if '<' in from_addr else from_addr)
                subject_slug = self._sanitize_filename(subject[:30])
                filename = f"{date_str_filename}_{sender_slug}_{subject_slug}.md"

                file_rel = self._save_email_file("inbox", filename, body, frontmatter)
                index.append({
                    "id": new_id,
                    "file": file_rel,
                    "direction": "incoming",
                    "status": "unread",
                    "date": date_local.isoformat(),
                    "from": from_addr,
                    "subject": subject,
                    "message_id": msg_id,
                    "in_reply_to": frontmatter["in_reply_to"],
                    "references": frontmatter["references"],
                    "labels": [],
                    "attachments": attachments,
                })
                if msg_id:
                    seen_message_ids.add(msg_id)

                # The index is saved for every email, before that email is marked
                # read, so mail can never be marked read without a record of it.
                # Saving once at the end would lose every email collected so far
                # if anything went wrong partway through.
                self.save_index(index)
                new_count += 1
                print(f"✅ Saved email from {from_addr}: {subject}")

                remember_seen()

            mail.close()
            mail.logout()

            if new_count > 0:
                print(f"📬 Fetched and stored {new_count} new email(s).")
                if not mark_seen:
                    print(f"📬 {len(self._pending_seen_uids)} email(s) left unread on the "
                          f"server until the record is saved.")
            else:
                print("📬 No new emails to fetch.")

        except Exception as e:
            print(f"❌ Failed to fetch emails: {e}")

    def mark_pending_seen(self):
        """Mark mail fetched with mark_seen=False as read on the server.

        Call this once the fetched mail has been saved somewhere permanent. If it
        is never called, the mail stays unread and the next fetch collects it
        again, which is the safe outcome: the same email arriving twice is a much
        smaller problem than an email disappearing.
        """
        if not self._pending_seen_uids:
            return 0
        if not self.email_address or not self.app_password:
            return 0

        marked = 0
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self.email_address, self.app_password)
            mail.select("inbox")
            for uid in self._pending_seen_uids:
                status, _ = mail.uid("STORE", uid, "+FLAGS", "\\Seen")
                if status == "OK":
                    marked += 1
            mail.close()
            mail.logout()
            self._pending_seen_uids = []
            print(f"📬 Marked {marked} email(s) as read on the server.")
        except Exception as e:
            # The mail stays unread and is collected again next time.
            print(f"⚠️ Could not mark mail as read on the server: {e}")
        return marked

    def send_operator_alert(self, subject, body):
        """Email the operator about how the agent is running.

        Used for budget notices. Like the session digest, this goes straight to
        the operator and is NOT saved in the email record, because it is a notice
        from the software rather than part of the agent's correspondence.
        """
        if not self.operator_email:
            print("⚠️ No operator email set. Skipping alert.")
            return False
        # The subject says who is speaking. These notices are written plainly,
        # which reads like a person unless the envelope says otherwise.
        if not subject.lower().startswith("[automated]"):
            subject = f"[automated] {subject}"
        body = (f"This is an automated notice from the {self.agent_name} engine. "
                f"Nobody wrote it.\n\n{body}"
                f"\n\n---\nSent by the {self.agent_name} engine "
                f"(not saved in the email record).\n")
        success, error = self._send_raw_email(self.operator_email, subject, body)
        if success:
            print(f"📧 Operator alert sent: {subject}")
        else:
            print(f"❌ Operator alert failed to send: {error}")
        return success

    # ---------- Full-Text Email Search ----------
    def search_emails(self, query):
        """
        Search all emails (headers + bodies) for a query string.
        Returns a list of matching entries with snippets.
        """
        if not query:
            return []
        index = self.load_index()
        results = []
        for entry in index:
            # Search headers
            searchable = f"{entry.get('from', '')} {entry.get('to', '')} {entry.get('subject', '')} {entry.get('message_id', '')}"
            if query.lower() in searchable.lower():
                results.append(entry)
                continue
            # Search body
            file_path = self.private_repo_path / "record" / "emails" / entry.get("file", "")
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                # Extract body (after frontmatter)
                parts = content.split("---\n", 2)
                if len(parts) >= 3:
                    body = parts[2].strip()
                    if query.lower() in body.lower():
                        # Add a snippet
                        snippet = self._get_snippet(body, query)
                        entry_snippet = entry.copy()
                        entry_snippet["snippet"] = snippet
                        results.append(entry_snippet)
        return results

    def _get_snippet(self, text, query, context_chars=100):
        """Return a snippet of text around the first occurrence of query."""
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - context_chars)
            end = min(len(text), match.end() + context_chars)
            snippet = text[start:end]
            snippet = pattern.sub(f"**{query}**", snippet)
            return f"...{snippet}..."
        return None

    # ---------- Thread Retrieval ----------
    def get_thread(self, message_id):
        """
        Return the full conversation for a message_id, chronological, including
        bodies. Walks In-Reply-To up to the root and down to all replies. Accepts
        either a raw Message-ID (e.g. '<CAB...@mail.gmail.com>') or a local id
        (e.g. 'msg_042').
        """
        index = self.load_index()
        by_msgid = {e.get("message_id"): e for e in index if e.get("message_id")}
        by_local = {e.get("id"): e for e in index if e.get("id")}

        if message_id in by_local and message_id not in by_msgid:
            message_id = by_local[message_id].get("message_id") or message_id

        if message_id not in by_msgid:
            return []

        # Walk up to the root via in_reply_to
        chain = []
        seen = set()
        cur = message_id
        while cur and cur in by_msgid and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = _parent_of(by_msgid[cur], by_msgid)
        root_ids = list(reversed(chain))

        # Walk down to all descendants (replies)
        thread_ids = list(root_ids)
        queue = list(root_ids)
        while queue:
            parent_id = queue.pop(0)
            children = [e.get("message_id") for e in index
                        if e.get("message_id")
                        and _parent_of(e, by_msgid) == parent_id
                        and e.get("message_id") not in thread_ids]
            thread_ids.extend(children)
            queue.extend(children)

        result = []
        for mid in thread_ids:
            entry = by_msgid.get(mid)
            if not entry:
                continue
            body = ""
            file_path = self.private_repo_path / "record" / "emails" / entry.get("file", "")
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts = content.split("---\n", 2)
                if len(parts) >= 3:
                    body = parts[2].strip()
            result.append({
                "id": entry.get("id"),
                # Which side of the conversation this is. Until sent mail
                # carried a Message-ID nothing outgoing could appear in a
                # thread, so there was only ever one answer.
                "direction": entry.get("direction", ""),
                "from": entry.get("from", ""),
                "to": entry.get("to", ""),
                "subject": entry.get("subject", ""),
                "date": entry.get("date", entry.get("date_created", "")),
                "message_id": entry.get("message_id", ""),
                "in_reply_to": entry.get("in_reply_to"),
                "references": entry.get("references"),
                "body": body,
            })
        result.sort(key=lambda e: e.get("date", ""))
        return result

    # ---------- Labels / Tags ----------
    def add_label(self, email_id, label):
        """Add a label to an email."""
        index = self.load_index()
        for entry in index:
            if entry.get("id") == email_id:
                if "labels" not in entry:
                    entry["labels"] = []
                if label not in entry["labels"]:
                    entry["labels"].append(label)
                    self.save_index(index)
                    return True
                return False
        return False

    def remove_label(self, email_id, label):
        """Remove a label from an email."""
        index = self.load_index()
        for entry in index:
            if entry.get("id") == email_id:
                if "labels" in entry and label in entry["labels"]:
                    entry["labels"].remove(label)
                    self.save_index(index)
                    return True
                return False
        return False

    # ---------- List / Enumerate ----------
    def list_emails(self, status=None, label=None):
        """Return index entries filtered by status and/or label (summaries, no bodies)."""
        index = self.load_index()
        out = []
        for entry in index:
            if status is not None and entry.get("status") != status:
                continue
            if label is not None and label not in entry.get("labels", []):
                continue
            out.append({
                "id": entry.get("id"),
                "status": entry.get("status"),
                "direction": entry.get("direction"),
                "from": entry.get("from", ""),
                "to": entry.get("to", ""),
                "subject": entry.get("subject", ""),
                "date": entry.get("date") or entry.get("date_created", ""),
                "labels": entry.get("labels", []),
                "message_id": entry.get("message_id", ""),
            })
        return out

    def list_drafts(self):
        """Return all saved drafts (not yet sent)."""
        return self.list_emails(status="draft")

    def list_outbox(self):
        """Return emails queued in the outbox awaiting retry.

        Outbox entries are files in record/emails/outbox/ (not index.json records —
        _save_to_outbox writes the file directly), so read the folder here.
        """
        outbox_path = self.private_repo_path / "record" / "emails" / "outbox"
        if not outbox_path.exists():
            return []
        out = []
        for file_path in sorted(outbox_path.glob("*.md")):
            try:
                content = file_path.read_text(encoding="utf-8")
                parts = content.split("---\n", 2)
                if len(parts) < 3:
                    continue
                frontmatter = {}
                for line in parts[1].strip().split("\n"):
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        frontmatter[key.strip()] = value.strip().strip('"')
                out.append({
                    "id": frontmatter.get("id"),
                    "status": "outbox",
                    "direction": "outgoing",
                    "to": frontmatter.get("to", ""),
                    "subject": frontmatter.get("subject", ""),
                    "date": frontmatter.get("date_created", ""),
                    "retry_count": int(frontmatter.get("retry_count", 0)),
                    "last_error": frontmatter.get("last_error", ""),
                    "labels": [],
                })
            except Exception:
                continue
        return out

    def count_outbox(self):
        """Number of emails currently queued in the outbox (folder file count)."""
        outbox_path = self.private_repo_path / "record" / "emails" / "outbox"
        return len(list(outbox_path.glob("*.md"))) if outbox_path.exists() else 0

    def list_by_label(self, label):
        """Return all emails carrying a given label."""
        return self.list_emails(label=label)

    def mail_for_prompt(self, limit=5, body_limit=MAX_EMAIL_BODY_CHARS):
        """The whole mail situation, for putting in front of an agent.

        The unread letters and the ones set aside, in one call, because the
        agent is told that a letter it sets down will come back — and that
        promise is only true if whoever wired this library up remembered the
        second half. One line to add is a promise that keeps itself; two lines
        is a promise with a way to fail silently."""
        parts = [self.unread_for_prompt(limit=limit, body_limit=body_limit)]
        waiting = self.set_aside_summary()
        if waiting:
            parts.append(waiting)
        return "\n\n".join(parts)

    def unread_for_prompt(self, limit=5, body_limit=MAX_EMAIL_BODY_CHARS):
        """The unread mail, written out for putting in front of an agent.

        Whole letters, with what came attached and how each one is filed. The
        library builds this rather than leaving it to every caller, because the
        parts that are easy to leave out — the labels, the note that more are
        waiting — are the parts that make deferring a letter work at all."""
        unread = [e for e in self.load_index()
                  if e.get("direction") == "incoming" and e.get("status") == "unread"]
        if not unread:
            return "No unread email."
        out = [f"You have {len(unread)} unread email(s). Here is each in full:\n"]
        for i, e in enumerate(unread[:limit], 1):
            out.append(f"--- Email #{i} (ID: {e.get('id')}) ---")
            out.append(f"From: {e.get('from')}")
            out.append(f"Subject: {e.get('subject')}")
            out.append(f"Date: {e.get('date')}")
            if e.get("message_id"):
                out.append(f"Message-ID: {e.get('message_id')}")
            if e.get("in_reply_to"):
                out.append(f"In-Reply-To: {e.get('in_reply_to')}")
            filed = labels_line(e.get("labels"))
            if filed:
                out.append(filed)
            attached = describe_attachments(e.get("attachments"))
            if attached:
                out.append(attached)
            out.append("Body:")
            out.append(trim_for_prompt(self._body_of(e), e.get("file", ""),
                                       body_limit))
            out.append("")
        if len(unread) > limit:
            out.append(f"... and {len(unread) - limit} more unread.")
        return "\n".join(out)

    def _body_of(self, entry):
        """The text of a stored letter, without its frontmatter."""
        path = self.private_repo_path / "record" / "emails" / (entry.get("file") or "")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return "[Could not read email body]"
        parts = content.split("---\n", 2)
        return parts[2].strip() if len(parts) >= 3 else content.strip()

    def list_labels(self):
        """Every label in use, and how many letters carry it.

        Without this a label can only be found by guessing the exact words it
        was written with — which means a label put on a letter last week is
        unreachable by an agent that does not remember writing it."""
        counts = {}
        for entry in self.load_index():
            for label in entry.get("labels") or []:
                label = str(label).strip()
                if label:
                    counts[label] = counts.get(label, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def set_aside(self, email_id, label=REVIEW_LATER):
        """Keep a letter to come back to another day.

        Answering is not the only honest response to a letter, and neither is
        deciding not to answer. Setting one down is a third thing, and it only
        works if it is picked back up — which is what set_aside_summary is for."""
        if self.add_label(email_id, label):
            return f"Set aside: {email_id} is filed as '{label}'."
        return (f"{email_id} was already set aside, or there is no letter with "
                f"that id.")

    def pick_up(self, email_id, label=REVIEW_LATER):
        """Take a letter back up: off the set-aside pile and into the unread list.

        It goes back to unread rather than simply losing its label. A letter set
        aside has usually been marked read as well, and taking the label off on
        its own would drop it out of both lists at once — gone from the waiting
        list and not in the unread one, reachable only by searching for a letter
        the agent would have to remember existed. "Pick up" is the opposite of
        dismissing something, and it should put the letter back in your hands."""
        if not self.remove_label(email_id, label):
            return f"{email_id} was not set aside."
        self.mark_email_unread(email_id)
        return (f"Picked up: {email_id} is off the set-aside pile and back among "
                f"your unread letters.")

    def set_aside_summary(self, label=REVIEW_LATER, limit=10):
        """What is waiting to be come back to. Put this in every prompt.

        The point of the whole mechanism. A letter set aside and never shown
        again has not been deferred, it has been lost, and the agent will not
        know the difference — it will remember deciding to come back to
        something and have no way to find it."""
        waiting = self.list_by_label(label)
        if not waiting:
            return ""
        lines = [f"You set {len(waiting)} letter"
                 f"{'s' if len(waiting) != 1 else ''} aside to come back to:"]
        for e in waiting[:limit]:
            who = e.get("from") or e.get("to") or "someone"
            when = str(e.get("date") or "")[:10]
            lines.append(f"  [{e.get('id')}] {who} — \"{e.get('subject')}\""
                         f"{f' ({when})' if when else ''}")
        if len(waiting) > limit:
            lines.append(f"  ... and {len(waiting) - limit} more.")
        lines.append("Answer one, or `pick_up` it to stop setting it aside, or "
                     "leave it here another day — but it will keep asking.")
        return "\n".join(lines)

    # ---------- Outbox Retry System ----------
    def retry_outbox(self):
        """
        Retry all emails in the outbox folder.
        Moves to sent/ on success, increments retry_count on failure.
        After 3 failures, moves to failed/.
        """
        outbox_path = self.private_repo_path / "record" / "emails" / "outbox"
        if not outbox_path.exists():
            return

        outbox_files = list(outbox_path.glob("*.md"))
        if not outbox_files:
            return

        print(f"📤 Retrying {len(outbox_files)} email(s) in Outbox...")
        # The index is not loaded here. _archive_sent() reads and writes it for
        # each email that goes out, so holding a copy and saving it at the end
        # would overwrite those entries and lose the record of what was sent.
        moved_count = 0
        failed_count = 0

        for file_path in outbox_files:
            try:
                content = file_path.read_text(encoding="utf-8")
                parts = content.split("---\n", 2)
                if len(parts) < 3:
                    print(f"⚠️ Invalid outbox file format: {file_path.name}")
                    continue

                frontmatter_lines = parts[1].strip().split("\n")
                frontmatter = {}
                for line in frontmatter_lines:
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        key = key.strip()
                        value = value.strip().strip('"')
                        frontmatter[key] = value

                body = parts[2].strip()
                to = frontmatter.get("to")
                subject = frontmatter.get("subject")
                in_reply_to = frontmatter.get("in_reply_to")
                cc = frontmatter.get("cc")
                bcc = frontmatter.get("bcc")
                retry_count = int(frontmatter.get("retry_count", 0))

                if not to or not subject:
                    print(f"⚠️ Missing fields in outbox file: {file_path.name}")
                    continue

                print(f"📤 Retrying email to {to}...")
                message_id = self.new_message_id()
                success, error = self._send_raw_email(
                    to, subject, body, in_reply_to, cc, bcc, message_id,
                    self.reference_chain(in_reply_to))

                if success:
                    now_local = datetime.now(self.timezone)
                    self._archive_sent(to, subject, body, in_reply_to, cc, bcc,
                                       now_local, message_id)
                    self._mark_answered(in_reply_to)
                    file_path.unlink()
                    moved_count += 1
                    print(f"✅ Retry succeeded for {to}")
                else:
                    retry_count += 1
                    if retry_count >= 3:
                        failed_path = self.private_repo_path / "record" / "emails" / "failed" / file_path.name
                        file_path.rename(failed_path)
                        failed_count += 1
                        print(f"❌ Email to {to} failed after 3 attempts. Moved to failed/. Error: {error}")
                    else:
                        new_frontmatter = frontmatter.copy()
                        new_frontmatter["retry_count"] = retry_count
                        new_frontmatter["last_retry"] = datetime.now(self.timezone).isoformat()
                        new_frontmatter["last_error"] = error
                        lines = ["---"]
                        for key, value in new_frontmatter.items():
                            if value is not None:
                                if isinstance(value, str):
                                    value = value.replace('"', '\\"')
                                    lines.append(f'{key}: "{value}"')
                                else:
                                    lines.append(f'{key}: {value}')
                        lines.append("---")
                        lines.append("")
                        lines.append(body)
                        file_path.write_text("\n".join(lines), encoding="utf-8")
                        print(f"🔄 Retry failed for {to}. Retry count: {retry_count}. Error: {error}")

            except Exception as e:
                print(f"❌ Error processing outbox file {file_path.name}: {e}")
                failed_count += 1

        if moved_count > 0 or failed_count > 0:
            print(f"📤 Outbox retry complete. Sent: {moved_count}, Failed: {failed_count}")

    # ---------- Send Outgoing Emails ----------
    def _send_raw_email(self, to, subject, body, in_reply_to=None, cc=None,
                        bcc=None, message_id=None, references=None):
        """Internal: send an email via SMTP. Returns (success, error_message)."""
        if not self.email_address or not self.app_password:
            return False, "Email credentials missing"

        try:
            msg = MIMEMultipart()
            msg["From"] = self.email_address
            msg["To"] = to
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            # Without a Message-ID of its own, a sent letter cannot be referred
            # to: a reply to it names an id nothing has, so the reply arrives
            # with no parent and the conversation reads as one-sided. The
            # caller passes one in so it can be saved with the copy.
            msg["Message-ID"] = message_id or self.new_message_id()
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                # The whole conversation, not just the letter being answered.
                # Some mail programs thread on References alone, and one naming
                # a single message puts a long exchange in two pieces.
                msg["References"] = references or in_reply_to
            if cc:
                msg["Cc"] = cc
            if bcc:
                msg["Bcc"] = bcc
            msg.attach(MIMEText(body, "plain"))

            # Build recipient list: split 'to' AND cc/bcc on commas so a
            # comma-separated list of recipients works everywhere.
            recipients = [addr.strip() for addr in str(to).split(',') if addr.strip()]
            if cc:
                recipients.extend([addr.strip() for addr in cc.split(',') if addr.strip()])
            if bcc:
                recipients.extend([addr.strip() for addr in bcc.split(',') if addr.strip()])

            server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            server.login(self.email_address, self.app_password)
            server.send_message(msg, to_addrs=recipients)
            server.quit()
            return True, None

        except smtplib.SMTPRecipientsRefused as e:
            return False, f"Recipient refused (address not found?): {e}"
        except smtplib.SMTPAuthenticationError as e:
            return False, f"Authentication failed: {e}"
        except smtplib.SMTPDataError as e:
            return False, f"Data error: {e}"
        except smtplib.SMTPServerDisconnected as e:
            return False, f"Server disconnected: {e}"
        except socket.timeout:
            return False, "Connection timeout"
        except Exception as e:
            return False, f"SMTP error: {e}"

    def send_email(self, to, subject, body, in_reply_to=None, cc=None, bcc=None):
        """
        Send an email via Gmail SMTP.
        Warn (do NOT silently block) on an invalid recipient address, and return
        (success, error) so the engine can surface a SPECIFIC message to the agent.
        On success, archive a copy in sent/. On SMTP failure, save to outbox/ for retry.
        """
        if not self.email_address or not self.app_password:
            return False, "Email credentials missing"

        valid, reason = validate_email_address(to)
        if not valid:
            print(f"⚠️ Email validation warning: {reason}")
            return False, reason

        now_local = datetime.now(self.timezone)
        print(f"📤 Sending email to {to}...")

        message_id = self.new_message_id()
        success, error = self._send_raw_email(
            to, subject, body, in_reply_to, cc, bcc, message_id,
            self.reference_chain(in_reply_to))

        if success:
            try:
                self._archive_sent(to, subject, body, in_reply_to, cc, bcc,
                                   now_local, message_id)
            except Exception as e:
                # The letter has gone. Reporting a failure here would be untrue,
                # and sending it again to get a copy would send it twice — so
                # the only thing left is to say the record is missing.
                print(f"⚠️ Email sent to {to}, but its copy could not be saved: {e}")
                print("   The recipient has it; your record of it does not.")
                return True, None
            closed = self._mark_answered(in_reply_to)
            if closed:
                print(f"📌 Answered — {closed}")
            print(f"✅ Email sent successfully to {to}")
            return True, None
        else:
            self._save_to_outbox(to, subject, body, in_reply_to, cc, bcc, now_local, error)
            print(f"📤 Email queued in outbox for retry: {to}. Error: {error}")
            return False, error

    def _mark_answered(self, in_reply_to):
        """Close out the letter a reply was written to.

        Answering something is the clearest possible statement that it has been
        dealt with, so it should not also have to be said. The letter is marked
        read and taken off the set-aside pile — otherwise a letter that has been
        answered goes on appearing as waiting, and the one list meant to be
        trusted fills up with work already done.

        Never raises: the reply has gone, and losing the send over the
        bookkeeping afterwards would be the wrong trade."""
        if not in_reply_to:
            return ""
        try:
            index = self.load_index()
            for entry in index:
                if (entry.get("message_id") != in_reply_to
                        or entry.get("direction") != "incoming"):
                    continue
                notes = []
                if entry.get("status") != "read":
                    entry["status"] = "read"
                    notes.append("marked read")
                if REVIEW_LATER in (entry.get("labels") or []):
                    entry["labels"].remove(REVIEW_LATER)
                    notes.append("off the set-aside pile")
                if notes:
                    self.save_index(index)
                    return f"{entry.get('id')}: {' and '.join(notes)}."
                return ""
        except Exception as e:
            print(f"⚠️ Replied, but could not close out the original: {e}")
        return ""

    def _save_to_outbox(self, to, subject, body, in_reply_to, cc, bcc, now_local, error):
        """Save an email to the outbox folder for retry."""
        index = self.load_index()
        existing_ids = [entry.get("id") for entry in index]

        date_str = now_local.strftime("%Y-%m-%d_%H-%M-%S")
        recipient_slug = self._sanitize_filename(to.split('<')[0].strip() if '<' in to else to)
        subject_slug = self._sanitize_filename(subject[:30])
        filename = f"{date_str}_{recipient_slug}_{subject_slug}.md"

        frontmatter = {
            "direction": "outgoing",
            "status": "outbox",
            "date_created": now_local.isoformat(),
            "to": to,
            "subject": subject,
            "in_reply_to": in_reply_to,
            "cc": cc,
            "bcc": bcc,
            "retry_count": 0,
            "last_retry": "",
            "last_error": error,
        }
        new_id = self._generate_email_id(existing_ids)
        frontmatter["id"] = new_id

        self._save_email_file("outbox", filename, body, frontmatter)
        print(f"📤 Email saved to outbox for retry: {filename}")

    def _archive_sent(self, to, subject, body, in_reply_to, cc, bcc, now_local,
                      message_id=""):
        """Save a sent email to the sent/ folder and update index."""
        index = self.load_index()
        existing_ids = [entry.get("id") for entry in index]

        date_str = now_local.strftime("%Y-%m-%d_%H-%M-%S")
        recipient_slug = self._sanitize_filename(to.split('<')[0].strip() if '<' in to else to)
        subject_slug = self._sanitize_filename(subject[:30])
        filename = f"{date_str}_{recipient_slug}_{subject_slug}.md"

        frontmatter = {
            "direction": "outgoing",
            "status": "sent",
            "date": now_local.isoformat(),
            "to": to,
            "subject": subject,
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "cc": cc,
            "bcc": bcc,
            "labels": [],
        }
        new_id = self._generate_email_id(existing_ids)
        frontmatter["id"] = new_id

        file_rel = self._save_email_file("sent", filename, body, frontmatter)
        index.append({
            "id": new_id,
            "file": file_rel,
            "direction": "outgoing",
            "status": "sent",
            "date": now_local.isoformat(),
            "to": to,
            "subject": subject,
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "cc": cc,
            "bcc": bcc,
            "labels": [],
        })
        self.save_index(index)
        print(f"📁 Archived sent email in {file_rel}")

    # ---------- Drafts ----------
    def save_draft(self, to, subject, body, in_reply_to=None, cc=None, bcc=None):
        """Save a draft email to the drafts/ folder and update the index."""
        if not to or not subject or body is None:
            print("❌ Missing required fields for draft.")
            return None

        now_local = datetime.now(self.timezone)
        index = self.load_index()
        existing_ids = [entry.get("id") for entry in index]

        date_str = now_local.strftime("%Y-%m-%d_%H-%M-%S")
        recipient_slug = self._sanitize_filename(to.split('<')[0].strip() if '<' in to else to)
        subject_slug = self._sanitize_filename(subject[:30])
        filename = f"{date_str}_{recipient_slug}_{subject_slug}.md"

        frontmatter = {
            "direction": "outgoing",
            "status": "draft",
            "date_created": now_local.isoformat(),
            "updated_at": now_local.isoformat(),
            "to": to,
            "subject": subject,
            "in_reply_to": in_reply_to,
            "cc": cc,
            "bcc": bcc,
            "labels": [],
        }
        new_id = self._generate_email_id(existing_ids)
        frontmatter["id"] = new_id

        file_rel = self._save_email_file("drafts", filename, body, frontmatter)
        index.append({
            "id": new_id,
            "file": file_rel,
            "direction": "outgoing",
            "status": "draft",
            "date_created": now_local.isoformat(),
            "updated_at": now_local.isoformat(),
            "to": to,
            "subject": subject,
            "in_reply_to": in_reply_to,
            "cc": cc,
            "bcc": bcc,
            "labels": [],
        })
        self.save_index(index)
        print(f"📝 Draft saved to {file_rel}")
        return file_rel

    # ---------- Mark as Read ----------
    def mark_email_read(self, email_id):
        """Mark an incoming email as read in the index."""
        index = self.load_index()
        for entry in index:
            if entry.get("id") == email_id and entry.get("direction") == "incoming":
                entry["status"] = "read"
                self.save_index(index)
                print(f"📌 Marked email {email_id} as read.")
                return True
        print(f"⚠️ Email {email_id} not found or not incoming.")
        return False

    def mark_email_unread(self, email_id):
        """Put an incoming email back into the unread list."""
        index = self.load_index()
        for entry in index:
            if entry.get("id") == email_id and entry.get("direction") == "incoming":
                entry["status"] = "unread"
                self.save_index(index)
                return True
        return False

    # ---------- Session Digest ----------
    def send_session_digest(self, session_type, session_num,
                            budget_spent, budget_remaining,
                            incoming_count=0, sent_count=0,
                            outbox_count=0, errors=None,
                            incoming_emails=None, sent_emails=None,
                            files_edited=0, journal_written=False, journal_entry="",
                            account_balance=None, monthly_limit=None,
                            scripts_run=None, tasks_completed=0, tasks_added=0,
                            journal_forced=False):
        """
        Send a session digest email to the operator.

        IMPORTANT: digests are ENGINE telemetry, not the agent's correspondence.
        They are sent directly via SMTP and are NOT archived in record/emails/ and
        NOT added to index.json (so they never pollute the agent's email record).
        """
        if not self.operator_email:
            print("⚠️ No operator email set. Skipping digest.")
            return False

        now_local = datetime.now(self.timezone)
        subject = f"[{self.agent_name}] Session {session_num} – {session_type} ({now_local.strftime('%Y-%m-%d %H:%M')})"

        limit_note = f" of ${monthly_limit:.2f}" if monthly_limit else ""
        # The account balance is what the provider holds; the monthly figures are
        # what this agent has been allowed. They are different numbers, so they
        # are shown on different lines.
        balance_line = (f"- Account balance at the provider: ${account_balance:.2f}\n"
                        if account_balance is not None else "")
        scripts_note = ", ".join(scripts_run) if scripts_run else "none"
        # Three outcomes, not two. A forced entry means there IS a file, written
        # by the engine because the agent wrote nothing — reporting that as "no
        # journal entry" reads as a gap in the record that is not there.
        if journal_forced:
            journal_note = "⚠️ Forced — the engine wrote a stub; the agent wrote nothing"
        elif journal_written:
            journal_note = "✅ Yes"
        else:
            journal_note = "❌ No"

        body = f"""{self.agent_name} Session Digest
=======================

Session: {session_type} (Session #{session_num})
Date/Time: {now_local.strftime('%Y-%m-%d %H:%M %Z')}

📊 Summary:
- Incoming emails received: {incoming_count}
- Emails sent successfully: {sent_count}
- Emails in Outbox (pending retry): {outbox_count}
- Spent this month: ${budget_spent:.2f}{limit_note}
- Remaining this month: ${budget_remaining:.2f}
{balance_line}- Files written: {files_edited}
- Scripts run: {scripts_note}
- Tasks completed: {tasks_completed}
- Tasks added: {tasks_added}
- Journal entry written: {journal_note}

"""

        if journal_written and journal_entry:
            preview = journal_entry[:300] + "..." if len(journal_entry) > 300 else journal_entry
            body += f"""
📝 Journal Entry:
{preview}
"""

        if incoming_emails:
            body += "\n📬 Incoming Emails this session:\n"
            for email in incoming_emails[:10]:
                body += f"  - From: {email.get('from', 'Unknown')} | Subject: {email.get('subject', 'No subject')} | Date: {email.get('date', 'Unknown')}\n"
            if len(incoming_emails) > 10:
                body += f"  ... and {len(incoming_emails) - 10} more.\n"

        if sent_emails:
            body += "\n📤 Sent Emails this session:\n"
            for email in sent_emails[:10]:
                body += f"  - To: {email.get('to', 'Unknown')} | Subject: {email.get('subject', 'No subject')} | Date: {email.get('date', 'Unknown')}\n"
            if len(sent_emails) > 10:
                body += f"  ... and {len(sent_emails) - 10} more.\n"

        if errors:
            body += f"""
⚠️ Errors:
{errors}
"""
        else:
            body += "\n✅ No errors reported this session.\n"

        body += f"""
---
Sent by {self.agent_name}, autonomous AI agent.
(Engine-generated session digest — sent outside the agent's correspondence archive.)
"""

        print(f"📧 Sending session digest to {self.operator_email}...")
        success, error = self._send_raw_email(self.operator_email, subject, body)
        if success:
            print(f"✅ Session digest sent to {self.operator_email} (not archived in the email record).")
            return True
        else:
            print(f"❌ Session digest failed to send: {error}")
            return False


# =====================================================================
# EXAMPLE USAGE (if run as a standalone script)
# =====================================================================
if __name__ == "__main__":
    import os
    email = os.environ.get("GMAIL_EMAIL", "your-email@gmail.com")
    password = os.environ.get("GMAIL_APP_PASSWORD", "your-app-password")
    operator = os.environ.get("OPERATOR_EMAIL", "operator@example.com")
    repo_path = os.environ.get("PRIVATE_REPO_PATH", "./private-brain")

    if email and password:
        inbox = AgentInbox(email, password, repo_path, operator_email=operator, agent_name="AI Agent")
        inbox.fetch_unread_and_store()
        inbox.retry_outbox()
    else:
        print("Set GMAIL_EMAIL and GMAIL_APP_PASSWORD environment variables to test.")
