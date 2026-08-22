# Agent Inbox

A tiny email helper for AI agents. It lets your agent **check, search, send,
and keep a record of email** — without you writing any email code.

It works with Gmail (over IMAP/SMTP), needs no extra installs (Python's built-in
libraries only), and saves everything as plain files your agent can read.

## What it does for your agent

- Fetches new Gmail into a local `inbox/`
- Keeps a copy of everything sent in `sent/`
- Saves `drafts/` to finish and send later
- Retries failed sends from an `outbox/` (gives up after 3 tries → `failed/`)
- Full-text search over every email
- Threads (whole conversation, oldest first)
- Labels + read/unread tracking

## Quick start (no coding experience needed)

**1. Copy two files next to your agent's main script.**

Copy these two files into the same folder as your main agent script:

- `agent_inbox.py`
- `agent_email_config.py`

**2. Open `agent_email_config.py` and edit four values** — your agent's Gmail
address, where to save mail, the agent's name, and timezone. The file tells you
exactly what each one is.

**3. Set two secrets** — `GMAIL_APP_PASSWORD` (required) and `OPERATOR_EMAIL`
(optional). The top of `agent_email_config.py` explains exactly what each one
needs to be and where to set it.

**4. Add these two lines to your main agent script:**

```python
from agent_email_config import build_inbox, AGENT_TOOL_INSTRUCTIONS
inbox = build_inbox()
```

**5. Tell your agent about its email tools.** Find the place in your script
where the prompt text is built (the text that gets sent to the model) and add
the ready-made instructions to it. It will look like one of these:

```python
prompt += AGENT_TOOL_INSTRUCTIONS
# or
prompt = prompt + AGENT_TOOL_INSTRUCTIONS
# or, inside an f-string:
prompt = f"... {AGENT_TOOL_INSTRUCTIONS}"
```

> `AGENT_TOOL_INSTRUCTIONS` is a block of text that ships with the library and
> describes the email methods to your agent. Appending it to the prompt is what
> makes the agent "know" it can check, search, and send email.

**Try it first:** run `python agent_email_config.py` — it fetches new mail and
retries the outbox, so you can see it working before wiring it in.

## What it outputs

Under your `EMAIL_ARCHIVE_REPO_PATH`, it creates:

```
<your path>/
└── record/
    └── emails/
        ├── index.json    # searchable list of every message
        ├── inbox/        # fetched mail (.md files)
        ├── sent/         # copies of mail you sent
        ├── drafts/       # drafts not yet sent
        ├── outbox/       # failed sends awaiting retry
        └── failed/       # sends that failed 3 times
```

Each email is a Markdown file with a small header (from, to, subject, date)
followed by the body, so a human can read it too.

## What each file is for

| File | Purpose |
|---|---|
| `agent_inbox.py` | The library itself (you don't edit this). |
| `agent_email_config.py` | Your settings — the only file you edit. |
| `README.md` | This file. |

## Things to know

- **Fetching consumes.** Fetched mail is marked read in Gmail, so each message
  is pulled once. (Nothing is deleted — it just isn't re-fetched.)
- **Retries aren't automatic.** A failed send lands in `outbox/`; call
  `inbox.retry_outbox()` to try again. After 3 failures it moves to `failed/`.
- **Sending to a made-up address** (like `x@example.com`) is rejected with a
  clear reason instead of being retried forever.
- **Only Gmail is built in.** For another email provider, change the two server
  names in `agent_inbox.py` (`imap.gmail.com`, `smtp.gmail.com`).

## More methods

```python
inbox.list_emails(status="unread")      # summaries (no bodies)
inbox.get_thread("<message-id>")        # whole conversation, oldest first
inbox.save_draft(to, subject, body)     # save a draft
inbox.mark_email_read("msg_001")        # mark an email read
inbox.add_label("msg_001", "follow-up") # tag an email
inbox.list_drafts()                     # list drafts
inbox.list_outbox()                     # list messages awaiting retry
inbox.retry_outbox()                    # retry failed sends
```

## License

MIT — see the header of `agent_inbox.py`.
