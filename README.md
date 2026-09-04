# Agent Inbox

A tiny email helper for AI agents. It lets your agent **check, search, send,
and keep a record of email** — without you writing any email code.

It works with Gmail (over IMAP/SMTP), needs no extra installs (Python's built-in
libraries only), and saves everything as plain files your agent can read.

## What it does for your agent

- Fetches new Gmail into a local `inbox/`
- Keeps a copy of everything sent in `sent/`
- **Replies that thread, both ways** — pass a letter's `Message-ID` and the
  answer arrives attached to the question rather than as a separate message; and
  because every letter your agent sends carries a `Message-ID` of its own, the
  replies it gets back attach to what it wrote
- Saves `drafts/` to finish and send later
- Retries failed sends from an `outbox/` (gives up after 3 tries → `failed/`)
- **Attachments named, not swallowed** — every attached file's name, type and
  size is recorded; small text ones are saved beside the letter for the agent to
  read if it wants them, and the rest stay in the mail account rather than in
  your repository forever
- **Letters can be set down, not just answered or refused** — `set_aside` files
  one to come back to, and it is put back in front of the agent every session
  until it is answered or picked up
- Full-text search over every email
- Threads (whole conversation, oldest first)
- Labels + read/unread tracking
- CC and BCC, and several recipients at once
- **Refuses invented addresses** — anything at `example.com` and the like is
  rejected with a reason, rather than queued and retried forever
- **Reads HTML-only mail** by falling back to the text inside it
- **Never loses a letter to a half-finished save** — fetch without marking read,
  and mark it once your copy is safely stored

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

**6. Put the mail itself in front of your agent.** Step 5 tells it what it *can*
do; this tells it what is *waiting*. One more line, in the same place:

```python
prompt += "\n\n" + inbox.mail_for_prompt()
```

That is the unread letters in full, and underneath them any letter your agent
set aside to come back to. It is safe to add unconditionally — when there is no
mail it is one short line.

> **Why it is one call and not two.** Your agent can set a letter down with
> `inbox.set_aside("msg_001")` instead of answering it, and it is told that a
> letter set down comes back. That is only true if something puts it back, so
> `mail_for_prompt()` does both halves rather than leaving the second to be
> remembered. Left out, a letter your agent decided to return to is simply gone
> — and it will not know: it will remember choosing to come back to something
> and have no way to find it. If you would rather place the two sections
> yourself, they are `unread_for_prompt()` and `set_aside_summary()`.

**Try it first:** run `python agent_email_config.py` — it fetches new mail and
retries the outbox, so you can see it working before wiring it in.

**Check it still works after you change it.** `test_agent_inbox.py` ships beside
the library and needs nothing installed and no network — it runs against a
stand-in for Gmail:

```bash
python -m unittest test_agent_inbox
```

82 tests, about half a second. Worth running before you trust a change: most of
what this library does wrong, it does quietly.

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
| `test_agent_inbox.py` | 82 tests. No network, nothing to install. |
| `README.md` | This file. |

## Things to know

- **Fetching consumes.** Fetched mail is marked read in Gmail, so each message
  is pulled once. (Nothing is deleted — it just isn't re-fetched.)
- **If you save mail somewhere that might not survive**, such as a copy of a Git
  repository you have not committed yet, use `fetch_unread_and_store(mark_seen=False)`
  and call `inbox.mark_pending_seen()` once the mail is safely stored. Mail that
  is marked read but then lost cannot be fetched again, because only unread mail
  is collected. Leaving it unread means the worst case is receiving the same
  email twice, instead of losing it.
- **Retries aren't automatic.** A failed send lands in `outbox/`; call
  `inbox.retry_outbox()` to try again. After 3 failures it moves to `failed/`.
- **Sending to a made-up address** (like `x@example.com`) is rejected with a
  clear reason instead of being retried forever.
- **Only Gmail is built in.** For another email provider, change the two server
  names in `agent_inbox.py` (`imap.gmail.com`, `smtp.gmail.com`).

## All methods available to your agent

- This is for your information only. These are automatically imported with the script.

```python
# Reading
inbox.fetch_unread_and_store(mark_seen=True)   # pull new mail; see "Things to know"
inbox.search_emails("query")                   # search all mail (headers + body)
inbox.get_thread("<message-id>")               # whole conversation, oldest first
inbox.list_emails(status="unread", label=None) # list messages (summaries)
inbox.mark_email_read("msg_001")               # mark one dealt with

# Writing
inbox.send_email(to, subject, body,
                 in_reply_to=None, cc=None, bcc=None)   # returns (ok, error)
inbox.save_draft(to, subject, body,
                 in_reply_to=None, cc=None, bcc=None)

# Setting aside — answering is not the only honest response
inbox.set_aside("msg_001")                     # come back to it another day
inbox.pick_up("msg_001")                       # take it off the pile
inbox.set_aside_summary()                      # what is waiting — put in the prompt

# Organising
inbox.add_label("msg_001", "label")            # tag an email
inbox.remove_label("msg_001", "label")         # remove a tag
inbox.list_by_label("label")                   # everything with that tag
inbox.list_labels()                            # every label in use, and how many
inbox.list_drafts()                            # drafts, unsent
inbox.list_outbox()                            # messages awaiting retry
inbox.count_outbox()                           # how many are waiting
inbox.retry_outbox()                           # try the queue again
```

**Replying closes the letter out.** Passing `in_reply_to` marks the letter being
answered as read and takes it off the set-aside pile, so a letter that has been
answered stops appearing as waiting. Answering something is the clearest
statement that it has been dealt with; it should not also have to be said.

**`in_reply_to` is what makes a reply a reply.** Pass the `Message-ID` of the
letter being answered, and begin the subject with `Re: `. Without it the answer
arrives as a separate message and the person may not connect the two.

`to`, `cc` and `bcc` each take one address or several, comma-separated.

For building the prompt (see step 6 of the quick start):

```python
inbox.mail_for_prompt()                        # everything below, in one call
inbox.unread_for_prompt()                      # just the unread letters, in full
inbox.set_aside_summary()                      # just what it set aside
```

Three more, for the program running the agent rather than the agent itself:

```python
inbox.mark_pending_seen()                      # mark mail read once safely saved
inbox.send_operator_alert(subject, body)       # notify the operator
inbox.send_session_digest(...)                 # a run summary for the operator
```

Neither of the last two is saved in the email record: they are messages from the
software about how the agent is running, not part of the agent's correspondence.

## License

MIT — see the header of `agent_inbox.py`.
