# Agent Inbox

A small, dependency-free email archive library for AI agents.

## The problem this solves

Agents that can send and receive email usually only have a *live* connection:
they can check "is there new mail?", but they have no local inbox to **search**,
no record of **sent** mail, and nowhere to keep **drafts** between sessions.
They lose threads, re-read old mail, and can't answer "what did I already tell
this person?"

Agent Inbox gives an agent a plain, file-based archive it owns:

- fetch new mail from a Gmail account (IMAP) into a local `inbox/`
- send mail (SMTP) and archive a copy in `sent/`
- save `drafts/` to finish and send later
- an `outbox/` with automatic retry (moves to `failed/` after 3 attempts)
- a searchable `index.json` over the whole archive
- full-text search, thread reconstruction, labels, and read/unread tracking

It is **Gmail over IMAP/SMTP**, written with the Python standard library only
(no `pip install`), and stores everything as Markdown files + JSON so the agent
(or a human) can read it directly.

## What's in this folder

| File | What it does |
|---|---|
| `agent_inbox.py` | The library. `AgentInbox` class: fetch, send, search, threads, drafts, labels, outbox retry, digest. Also runnable directly (`python agent_inbox.py`) as a fetch + retry smoke test. |
| `config.py` | Optional convenience wrapper. Loads `.env`, validates required values, and returns a ready `AgentInbox` via `build_inbox()`. |
| `.env.example` | Template for your settings. Copy to `.env` and edit. |
| `.gitignore` | Keeps `.env` and Python caches out of git. |

## What it outputs

On first run, the library creates this structure under the path you set as
`EMAIL_ARCHIVE_REPO_PATH`:

```
<your archive path>/
└── record/
    └── emails/
        ├── index.json      # searchable index of every message
        ├── inbox/          # fetched incoming mail (.md files)
        ├── sent/           # copies of mail you sent
        ├── drafts/         # drafts, not yet sent
        ├── outbox/         # failed sends awaiting retry
        └── failed/         # sends that failed 3 times
```

Each email is a Markdown file with YAML frontmatter (from, to, subject, date,
message_id, in_reply_to, labels, status) followed by the body. `index.json`
holds the same metadata plus the relative file path, so the archive is fully
searchable without opening every file.

On the console, it prints progress lines with emoji markers (📬 fetch, 📤 send,
✅ success, ⚠️ warnings, ❌ errors) — so a harness can also parse logs.

## Starting from zero

This repo ships **no** archive and **no** inbox. A brand-new agent starts
empty: the first `fetch_unread_and_store()` creates the folders and an empty
`index.json`, then fills the inbox with whatever is unread in the Gmail account
at that moment. There is no migration, no seed data, nothing to delete.

## Setup (for the human installing this)

### 1. Gmail prerequisites (one time)

For the Gmail account the agent will use:

1. Turn on **2-Step Verification**.
2. Enable **IMAP** (Gmail → Settings → See all settings → Forwarding and
   POP/IMAP → Enable IMAP).
3. Create an **App Password**: Google Account → Security → App passwords →
   generate one for **Mail**. This 16-character password is what goes in
   `.env` — *not* the account's normal password.

> Tip: give the agent its own dedicated Gmail address rather than sharing a
> human's inbox.

### 2. Install

```bash
git clone <this-repo-url> agent-email-client
cd agent-email-client
cp .env.example .env
# edit .env: GMAIL_EMAIL, GMAIL_APP_PASSWORD, EMAIL_ARCHIVE_REPO_PATH
```

### 3. Point at an archive location

`EMAIL_ARCHIVE_REPO_PATH` is the only "where" decision. Pick ONE:

- **Dedicated private repo** (recommended) — clone it locally, point the path
  at it, and commit the `record/emails/` tree so it's backed up and versioned.
- **A folder in your existing private repo** — same idea, isolated by path.
- **A folder in a public repo** — works, but **not recommended**: every email
  body becomes publicly readable. Email privacy should live in a private repo.

The library creates `record/emails/` under whatever path you choose.

### 4. Smoke test

```bash
python agent_inbox.py     # fetch new mail + retry the outbox
# or, via the config wrapper:
python config.py
```

## Wiring it into your agent (library usage)

```python
from agent_inbox import AgentInbox

inbox = AgentInbox(
    email_address="agent-name@gmail.com",
    app_password="<app password>",
    private_repo_path="/path/to/archive",       # archive lives here/record/emails/
    operator_email="you@example.com",            # optional, for digests
    agent_name="My Agent",
    timezone="America/Los_Angeles",
)

inbox.fetch_unread_and_store()                   # pull new mail into inbox/
inbox.retry_outbox()                             # re-attempt failed sends

unread = inbox.list_emails(status="unread")      # summaries, no bodies
results = inbox.search_emails("quarterly report")# full-text search
thread = inbox.get_thread("<message-id>")        # whole conversation, oldest first

inbox.save_draft("person@example.com", "Subject", "body ...")
ok, err = inbox.send_email("person@example.com", "Subject", "body ...", in_reply_to="<message-id>")
inbox.mark_email_read("msg_001")
inbox.add_label("msg_001", "follow-up")
```

Or use the config wrapper (loads `.env` for you):

```python
from config import build_inbox
inbox = build_inbox()
inbox.fetch_unread_and_store()
```

### Method cheat sheet

- `fetch_unread_and_store()` — fetch unread Gmail mail into `inbox/`, update the index, mark them `\Seen` in Gmail.
- `list_emails(status=None, label=None)` / `list_drafts()` / `list_outbox()` / `list_by_label(label)` — summaries.
- `search_emails(query)` — header + body search, returns matches with snippets.
- `get_thread(message_id)` — full conversation, oldest first, with bodies. Accepts a raw Message-ID or a local id (`msg_001`).
- `mark_email_read(email_id)` — mark incoming mail read in the index.
- `send_email(to, subject, body, in_reply_to=None, cc=None, bcc=None)` — sends, archives to `sent/`; on failure queues to `outbox/`. Returns `(success, error)`.
- `retry_outbox()` — retries queued mail; 3 failures → `failed/`.
- `save_draft(to, subject, body, ...)` — save a draft.
- `add_label(email_id, label)` / `remove_label(email_id, label)`.
- `send_session_digest(...)` — optional operator digest (sent directly, not archived).

### Behaviors worth knowing

- **Fetch consumes.** Fetching sets `\Seen` in Gmail, so each email is fetched
  once. (It's an inbox you own — nothing is deleted, it's just not re-fetched.)
- **Address validation warns, doesn't block.** `send_email` rejects obviously
  invalid/reserved addresses (e.g. `x@example.com`) with a specific reason and
  does NOT queue them to the outbox; a genuinely undeliverable real address is
  caught by SMTP and retried.
- **HTML-only mail** is converted to plain text (tag-stripped).
- **Retries** happen in the outbox, not automatically after `send_email` —
  call `retry_outbox()` on your schedule.
- **Blocklist / leak-guard** (redacting other people's emails before they're
  published) is *engine-level* policy in whatever agent uses this library, and
  is intentionally NOT part of this file — keep it in your harness.

## Keeping the archive in git (scheduling)

The library only writes files. To version/back up the archive, commit it. Two
common patterns:

- **Your agent's own session loop** already clones the archive repo; have it
  call `fetch_unread_and_store()` at session start and commit the diff like any
  other file.
- **A cron / GitHub Actions job** runs fetch + retry on a schedule and commits.

### GitHub Actions (optional, if you want a scheduled fetch)

This library ships no workflow (it's a library, not a service). If you want
GitHub Actions to run it, put a workflow in the archive repo that:

1. checks out the archive repo and this library,
2. runs `python agent_inbox.py` (or your wrapper),
3. commits and pushes the `record/emails/` changes.

Set these in the **archive repo's** Settings → **Secrets and variables** →
**Actions**:

| Secret | Purpose |
|---|---|
| `GMAIL_EMAIL` | the agent's Gmail address |
| `GMAIL_APP_PASSWORD` | the Gmail App Password |
| `OPERATOR_EMAIL` | (optional) digest recipient |
| a git token (`GITHUB_TOKEN` or a PAT) | push the committed archive back |

> The library itself needs **no** GitHub secrets — it reads the values from the
> environment. Secrets are only needed by the *deployment* that runs it and
> commits the result.

## Not (yet) included

- Non-Gmail IMAP/SMTP providers. The server names are hardcoded
  (`imap.gmail.com`, `smtp.gmail.com:465`). To use another provider, change
  those two host strings in `agent_inbox.py` (the methods are standard IMAP/SMTP).

## License

MIT — see the header of `agent_inbox.py`.
