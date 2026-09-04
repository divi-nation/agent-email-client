#!/usr/bin/env python3
"""
Tests for the email client, using a stand-in for Gmail so no network is used.

These cover the ways mail could previously go missing: being marked read on the
server before it was safely saved, and sent mail losing its index entry during an
outbox retry.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest import mock

# This file is shipped with the standalone email library as well as living
# here, so it looks for agent_inbox.py beside itself first and one level up
# second — the two layouts it runs in.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import agent_inbox  # noqa: E402
from agent_inbox import AgentInbox  # noqa: E402


def raw_email(sender, subject, body, message_id):
    mid = f"Message-ID: {message_id}\n" if message_id else ""
    return (f"From: {sender}\nSubject: {subject}\n"
            f"Date: Tue, 1 Sep 2026 10:00:00 -0700\n{mid}"
            f"Content-Type: text/plain\n\n{body}").encode()


class FakeIMAP:
    """A stand-in for Gmail. Records which UIDs were marked read."""

    def __init__(self, messages, fail_on_uid=None):
        self.messages = messages          # {uid: raw bytes}
        self.seen = set()
        self.fail_on_uid = fail_on_uid
        self.logged_out = False

    def login(self, *a):
        return "OK", []

    def select(self, *a):
        return "OK", []

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            unread = [u for u in self.messages if u not in self.seen]
            return "OK", [b" ".join(u.encode() for u in unread)]
        if command == "FETCH":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            if uid == self.fail_on_uid:
                raise RuntimeError("connection dropped mid-fetch")
            # A real server sets \Seen when the body is read, and only
            # BODY.PEEK[] avoids it. A fake that is gentler than the server
            # cannot show that leaving mail unread does not work.
            spec = " ".join(str(a) for a in args[1:]).upper()
            if "PEEK" not in spec:
                self.seen.add(uid)
            return "OK", [(b"1", self.messages[uid])]
        if command == "STORE":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            self.seen.add(uid)
            return "OK", []
        return "OK", []

    def close(self):
        pass

    def logout(self):
        self.logged_out = True


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.inbox = AgentInbox("agent@example.com", "app-password", self.repo,
                                operator_email="operator@example.com",
                                agent_name="Test Agent")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def index(self):
        return self.inbox.load_index()


class TestFetchDoesNotLoseMail(InboxTestCase):

    def test_mail_is_saved_before_it_is_marked_read(self):
        fake = FakeIMAP({"101": raw_email("a@x.com", "One", "body one", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        self.assertEqual(len(self.index()), 1)
        self.assertIn("101", fake.seen)

    def test_a_failure_partway_keeps_the_earlier_mail(self):
        """If the connection drops on the third email, the first two must still be
        in the index. Otherwise they are marked read on the server with no record
        of them, and only unread mail is ever fetched again."""
        fake = FakeIMAP({
            "101": raw_email("a@x.com", "One", "body one", "<1@x>"),
            "102": raw_email("b@x.com", "Two", "body two", "<2@x>"),
            "103": raw_email("c@x.com", "Three", "body three", "<3@x>"),
        }, fail_on_uid="103")
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()

        saved = {e["message_id"] for e in self.index()}
        self.assertEqual(saved, {"<1@x>", "<2@x>"})
        # Nothing may be marked read without a matching record.
        self.assertTrue(fake.seen.issubset({"101", "102"}))

    def test_deferred_marking_leaves_mail_unread_until_told(self):
        fake = FakeIMAP({"101": raw_email("a@x.com", "One", "body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store(mark_seen=False)
            self.assertEqual(fake.seen, set(), "must still be unread on the server")
            self.assertEqual(len(self.index()), 1, "but saved locally")

            self.inbox.mark_pending_seen()
            self.assertEqual(fake.seen, {"101"})

    def test_mail_is_refetched_if_it_was_never_marked_read(self):
        """A session that fails before saving leaves the mail unread, so the next
        run collects it again. Arriving twice is recoverable; disappearing is not."""
        fake = FakeIMAP({"101": raw_email("a@x.com", "One", "body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store(mark_seen=False)
            # mark_pending_seen() is never called - the session died.
            os.remove(self.inbox._index_path())          # the unsaved copy is lost
            self.inbox._pending_seen_uids = []

            self.inbox.fetch_unread_and_store(mark_seen=False)
        self.assertEqual(len(self.index()), 1, "the email should have come back")

    def test_the_same_email_is_not_saved_twice(self):
        fake = FakeIMAP({"101": raw_email("a@x.com", "One", "body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
            fake.seen.clear()                             # pretend it is unread again
            self.inbox.fetch_unread_and_store()
        self.assertEqual(len(self.index()), 1)

    def test_mail_without_a_message_id_is_still_delivered(self):
        """Two emails with no Message-ID are two emails, not a duplicate."""
        fake = FakeIMAP({
            "101": raw_email("a@x.com", "One", "body one", None),
            "102": raw_email("b@x.com", "Two", "body two", None),
        })
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        self.assertEqual(len(self.index()), 2)
        self.assertEqual(fake.seen, {"101", "102"})


class TestOutboxRetryKeepsTheRecord(InboxTestCase):

    def test_a_retried_email_stays_in_the_index(self):
        """_archive_sent writes the index for each email it archives. retry_outbox
        must not hold its own copy and save it afterwards, or the record of every
        email it just sent is overwritten."""
        with mock.patch.object(AgentInbox, "_send_raw_email", return_value=(False, "smtp down")):
            self.inbox.send_email("someone@realdomain.org", "Queued", "body")
        self.assertEqual(self.inbox.count_outbox(), 1)

        with mock.patch.object(AgentInbox, "_send_raw_email", return_value=(True, None)):
            self.inbox.retry_outbox()

        sent = [e for e in self.index() if e.get("direction") == "outgoing"]
        self.assertEqual(len(sent), 1, "the sent email lost its index entry")
        self.assertEqual(sent[0]["subject"], "Queued")
        self.assertEqual(self.inbox.count_outbox(), 0)

    def test_several_retried_emails_all_stay_in_the_index(self):
        with mock.patch.object(AgentInbox, "_send_raw_email", return_value=(False, "smtp down")):
            for i in range(3):
                self.inbox.send_email(f"p{i}@realdomain.org", f"Queued {i}", "body")

        with mock.patch.object(AgentInbox, "_send_raw_email", return_value=(True, None)):
            self.inbox.retry_outbox()

        sent = [e for e in self.index() if e.get("direction") == "outgoing"]
        self.assertEqual(len(sent), 3)
        self.assertEqual(len({e["id"] for e in sent}), 3, "ids must be unique")


class TestOperatorAlerts(InboxTestCase):

    def test_an_alert_is_not_saved_in_the_email_record(self):
        """Budget notices come from the software, not the agent, so they must not
        appear in the agent's correspondence."""
        before = len(self.index())
        with mock.patch.object(AgentInbox, "_send_raw_email", return_value=(True, None)) as send:
            ok = self.inbox.send_operator_alert("Low balance", "You have $1.50 left.")
        self.assertTrue(ok)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0], "operator@example.com")
        self.assertEqual(len(self.index()), before, "alert must not be archived")
        self.assertEqual(len(list((self.inbox.private_repo_path / "record" / "emails" / "sent").iterdir())), 0)

    def test_no_operator_means_no_alert(self):
        inbox = AgentInbox("a@example.com", "pw", self.repo, operator_email=None)
        self.assertFalse(inbox.send_operator_alert("Subject", "Body"))


class TestIdGeneration(InboxTestCase):

    def test_a_broken_index_entry_does_not_stop_new_mail(self):
        with open(self.inbox._index_path(), "w") as f:
            json.dump([{"file": "x.md"}, {"id": None}, {"id": "msg_007"}], f)
        fake = FakeIMAP({"101": raw_email("a@x.com", "One", "body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        ids = [e.get("id") for e in self.index() if e.get("id")]
        self.assertIn("msg_008", ids)




# This one class tests the engine's rendering of a search result rather than the
# library, so it is skipped when the file runs beside the standalone library.
try:
    import session  # noqa: F401
    HAS_ENGINE = True
except Exception:
    HAS_ENGINE = False


@unittest.skipUnless(HAS_ENGINE, "engine-only: session.py is not here")
class TestSearchNamesWhoWroteWhat(unittest.TestCase):
    """A sent email has no `from` — it has a `to`. Describing results by `from`
    alone renders everything the agent sent as "From: None", which reads as a
    broken record rather than as its own letter."""

    def test_a_sent_email_says_who_it_went_to(self):
        from session import _describe_email
        line = _describe_email({"direction": "outgoing", "to": "someone@example.org",
                                "subject": "Re: the kiln", "date": "2026-08-30T19:50:00",
                                "id": "msg_201"})
        self.assertIn("You wrote to someone@example.org", line)
        self.assertNotIn("None", line)

    def test_a_received_email_says_who_it_came_from(self):
        from session import _describe_email
        line = _describe_email({"direction": "incoming", "from": "someone@example.org",
                                "subject": "the kiln", "date": "2026-08-30T18:00:00",
                                "id": "msg_200"})
        self.assertIn("From someone@example.org", line)
        self.assertNotIn("None", line)

    def test_a_missing_address_does_not_read_as_None(self):
        from session import _describe_email
        for entry in ({"direction": "outgoing", "id": "a"},
                      {"direction": "incoming", "id": "b"}):
            self.assertNotIn("None", _describe_email(entry))

    def test_the_id_is_included_so_a_thread_can_be_reopened(self):
        from session import _describe_email
        self.assertIn("id=msg_201",
                      _describe_email({"direction": "outgoing", "to": "x@y.org", "id": "msg_201"}))


class ASendIsNotLostWhenTheCopyFails(InboxTestCase):
    """By the time the copy is written the letter has gone. Reporting a failure
    would be untrue, and sending again to get a copy would send it twice."""

    def test_the_send_is_still_reported_as_successful(self):
        with mock.patch.object(AgentInbox, "_send_raw_email",
                               return_value=(True, None)), \
             mock.patch.object(AgentInbox, "_archive_sent",
                               side_effect=OSError("disk full")):
            ok, err = self.inbox.send_email("a@b.com", "Subject", "Body")
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_it_does_not_raise(self):
        """It used to propagate, ending the round with the letter already sent."""
        with mock.patch.object(AgentInbox, "_send_raw_email",
                               return_value=(True, None)), \
             mock.patch.object(AgentInbox, "_archive_sent",
                               side_effect=OSError("disk full")):
            self.inbox.send_email("a@b.com", "Subject", "Body")

    def test_it_is_not_queued_for_retry(self):
        """Retrying would send the same letter a second time."""
        with mock.patch.object(AgentInbox, "_send_raw_email",
                               return_value=(True, None)), \
             mock.patch.object(AgentInbox, "_archive_sent",
                               side_effect=OSError("disk full")):
            self.inbox.send_email("a@b.com", "Subject", "Body")
        outbox = self.inbox.private_repo_path / "record" / "emails" / "outbox"
        self.assertEqual(list(outbox.glob("*")), [])

    def test_a_normal_send_still_archives(self):
        with mock.patch.object(AgentInbox, "_send_raw_email",
                               return_value=(True, None)):
            ok, _ = self.inbox.send_email("a@b.com", "Subject", "Body")
        self.assertTrue(ok)
        self.assertTrue(any(e.get("direction") == "outgoing"
                            for e in self.index()))

if __name__ == "__main__":
    unittest.main()


class TestHeadersAreReadable(InboxTestCase):
    """A subject with anything but plain ASCII in it arrives encoded.

    Stored as it arrives, it is unreadable to the agent and — worse — it does
    not match a search for the words it contains, so half a conversation goes
    missing from a search that should find all of it."""

    ENCODED = "=?UTF-8?Q?Re=3A_Operator_instructions_=E2=80=94_repo_cleanup?="
    PLAIN = "Re: Operator instructions — repo cleanup"

    def fetch_one(self, subject, sender="a@x.com"):
        fake = FakeIMAP({"101": raw_email(sender, subject, "body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        return self.index()[0]

    def test_an_encoded_subject_is_stored_as_words(self):
        self.assertEqual(self.fetch_one(self.ENCODED)["subject"], self.PLAIN)

    def test_an_encoded_sender_is_stored_as_words(self):
        entry = self.fetch_one("Hello", "=?UTF-8?Q?Andr=C3=A9?= <andre@x.com>")
        self.assertIn("André", entry["from"])

    def test_a_plain_subject_is_left_alone(self):
        self.assertEqual(self.fetch_one("Just a subject")["subject"],
                         "Just a subject")

    def test_an_encoded_subject_can_be_searched_for(self):
        self.fetch_one(self.ENCODED)
        self.assertEqual(len(self.inbox.search_emails("operator instructions")), 1)

    def test_a_malformed_header_is_kept_rather_than_lost(self):
        self.assertEqual(agent_inbox._decode_header("=?bogus?X?zz?="),
                         "=?bogus?X?zz?=")

    def test_an_empty_subject_falls_back(self):
        fake = FakeIMAP({"101": raw_email("a@x.com", "", "body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        self.assertEqual(self.index()[0]["subject"], "(no subject)")


class TestSentMailCanBeRepliedTo(InboxTestCase):
    """Sent mail carried no Message-ID of its own.

    Gmail assigned one at send time and the engine never learned it, so a reply
    named a parent nothing held: every reply arrived orphaned and no thread ever
    included the agent's own side of it."""

    def send_one(self):
        sent = {}

        def fake_smtp(*a, **k):
            server = mock.MagicMock()
            server.send_message.side_effect = (
                lambda msg, **kw: sent.update(msg=msg))
            return server

        with mock.patch.object(agent_inbox.smtplib, "SMTP_SSL", fake_smtp):
            ok, err = self.inbox.send_email("b@x.com", "Subject", "Body")
        self.assertTrue(ok, err)
        return sent["msg"]

    def test_the_letter_carries_a_message_id(self):
        self.assertTrue(self.send_one()["Message-ID"])

    def test_the_id_does_not_name_the_machine_it_was_sent_from(self):
        """The default would use this machine's hostname, telling everyone the
        agent writes to where it runs."""
        self.assertTrue(self.send_one()["Message-ID"].endswith("@example.com>"))

    def test_the_archived_copy_holds_the_same_id(self):
        msg = self.send_one()
        entry = [e for e in self.index() if e.get("direction") == "outgoing"][0]
        self.assertEqual(entry["message_id"], msg["Message-ID"])

    def test_a_reply_to_it_joins_the_thread(self):
        msg = self.send_one()
        reply = (f"From: b@x.com\nSubject: Re: Subject\n"
                 f"Date: Tue, 1 Sep 2026 11:00:00 -0700\n"
                 f"Message-ID: <reply@x>\nIn-Reply-To: {msg['Message-ID']}\n"
                 f"Content-Type: text/plain\n\nthanks").encode()
        fake = FakeIMAP({"101": reply})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        thread = self.inbox.get_thread("<reply@x>")
        self.assertEqual(len(thread), 2, "the agent's own letter is missing")
        self.assertEqual({m["direction"] for m in thread},
                         {"outgoing", "incoming"})
        mine = [m for m in thread if m["direction"] == "outgoing"][0]
        self.assertEqual(mine["message_id"], msg["Message-ID"])

    def test_a_reply_is_no_longer_orphaned(self):
        """Before the fix the reply named a parent nothing held, so asking for
        the thread gave back only the reply itself."""
        msg = self.send_one()
        reply = (f"From: b@x.com\nSubject: Re: Subject\n"
                 f"Date: Tue, 1 Sep 2026 11:00:00 -0700\n"
                 f"Message-ID: <reply@x>\nIn-Reply-To: {msg['Message-ID']}\n"
                 f"Content-Type: text/plain\n\nthanks").encode()
        fake = FakeIMAP({"101": reply})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        index = self.inbox.load_index()
        held = {e.get("message_id") for e in index if e.get("message_id")}
        orphans = [e for e in index
                   if e.get("in_reply_to") and e["in_reply_to"] not in held]
        self.assertEqual(orphans, [])


def raw_with_attachment(parts, sender="a@x.com", subject="With a file"):
    """A multipart message. Each part is (content_type, disposition, filename, body)."""
    out = [f"From: {sender}", f"Subject: {subject}",
           "Date: Tue, 1 Sep 2026 10:00:00 -0700", "Message-ID: <att@x>",
           "MIME-Version: 1.0",
           'Content-Type: multipart/mixed; boundary="B"', "", "--B"]
    for ctype, disp, name, body in parts:
        out.append(f"Content-Type: {ctype}")
        if disp:
            d = f"Content-Disposition: {disp}"
            if name:
                d += f'; filename="{name}"'
            out.append(d)
        out += ["", body, "--B"]
    out[-1] = "--B--"
    return "\n".join(out).encode()


class TestAnAttachedFileIsNotTheLetter(InboxTestCase):
    """The body hunt took the first text/plain part anywhere in the message,
    including inside an attachment. Forward a letter with a covering note and
    the forwarded text became the body — what the sender wrote was thrown away."""

    def fetch(self, raw):
        fake = FakeIMAP({"101": raw})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        entry = self.index()[0]
        path = os.path.join(self.repo, "record", "emails", entry["file"])
        with open(path, encoding="utf-8") as f:
            return entry, f.read().split("---\n", 2)[2].strip()

    def test_the_covering_note_is_the_body_not_the_forwarded_text(self):
        raw = raw_with_attachment([
            ("message/rfc822", "attachment", "fwd.eml",
             "From: c@x.com\nContent-Type: text/plain\n\nFORWARDED TEXT"),
            ("text/plain", None, None, "My covering note."),
        ])
        _, body = self.fetch(raw)
        self.assertEqual(body, "My covering note.")

    def test_a_letter_with_no_attachment_is_unchanged(self):
        _, body = self.fetch(raw_email("a@x.com", "Plain", "Just words.", "<p@x>"))
        self.assertEqual(body, "Just words.")


class TestAttachmentsAreNamedNotSwallowed(InboxTestCase):
    """An attachment used to leave no trace at all: the agent could be sent a
    file and have no way to know it existed. Its bytes still do not enter the
    repository — both are cloned every session and git keeps a file for good."""

    def fetch(self, parts):
        fake = FakeIMAP({"101": raw_with_attachment(parts)})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        return self.index()[0]

    def note(self):
        return [("text/plain", None, None, "See attached.")]

    def test_a_binary_is_named_but_not_kept(self):
        entry = self.fetch(self.note() + [
            ("application/pdf", "attachment", "report.pdf", "%PDF-1.4 binary")])
        att = entry["attachments"]
        self.assertEqual(att[0]["name"], "report.pdf")
        self.assertEqual(att[0]["type"], "application/pdf")
        self.assertNotIn("saved", att[0])
        self.assertFalse(os.path.isdir(
            os.path.join(self.repo, "record", "emails", "attachments")))

    def test_a_small_text_file_is_kept_where_it_can_be_read(self):
        entry = self.fetch(self.note() + [
            ("text/plain", "attachment", "notes.md", "The notes.")])
        saved = entry["attachments"][0]["saved"]
        self.assertTrue(saved.startswith("record/emails/attachments/"))
        # The path is relative to the repository root, which is how read_file
        # is given a file to open.
        with open(os.path.join(self.repo, saved), encoding="utf-8") as f:
            self.assertEqual(f.read(), "The notes.")

    def test_a_text_file_too_large_is_named_but_not_kept(self):
        entry = self.fetch(self.note() + [
            ("text/plain", "attachment", "huge.txt",
             "x" * (agent_inbox.MAX_STORED_ATTACHMENT_BYTES + 10))])
        self.assertNotIn("saved", entry["attachments"][0])

    def test_a_signature_logo_is_not_listed(self):
        """Every letter from an office carries one, and listing them would bury
        the attachment that was meant."""
        entry = self.fetch(self.note() + [
            ("image/png", "inline", "image001.png", "logo bytes"),
            ("application/pdf", "attachment", "real.pdf", "the actual file")])
        self.assertEqual([a["name"] for a in entry["attachments"]], ["real.pdf"])

    def test_the_body_is_still_the_covering_note(self):
        entry = self.fetch(self.note() + [
            ("text/plain", "attachment", "notes.md", "The notes.")])
        path = os.path.join(self.repo, "record", "emails", entry["file"])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().split("---\n", 2)[2].strip(), "See attached.")

    def test_a_letter_with_nothing_attached_says_nothing(self):
        fake = FakeIMAP({"101": raw_email("a@x.com", "S", "b", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        self.assertEqual(self.index()[0]["attachments"], [])
        self.assertEqual(agent_inbox.describe_attachments([]), "")

    def test_what_the_agent_is_told(self):
        lines = agent_inbox.describe_attachments([
            {"name": "report.pdf", "type": "application/pdf", "size": 412334},
            {"name": "notes.md", "type": "text/plain", "size": 2210,
             "saved": "record/emails/attachments/x_notes.md"},
        ])
        self.assertIn("report.pdf (403 KB, application/pdf)", lines)
        self.assertIn("cannot be read here", lines)
        self.assertIn("notes.md (2 KB) — saved at", lines)
        self.assertIn("`read_file` it", lines)

    def test_the_frontmatter_survives_a_quote_in_a_filename(self):
        entry = self.fetch(self.note() + [
            ("application/pdf", "attachment", 'the "final" draft.pdf', "bytes")])
        path = os.path.join(self.repo, "record", "emails", entry["file"])
        with open(path, encoding="utf-8") as f:
            front = f.read().split("---\n")[1]
        line = [l for l in front.splitlines() if l.startswith("attachments:")][0]
        self.assertEqual(json.loads(line.split(": ", 1)[1])[0]["name"],
                         'the "final" draft.pdf')


class TestAConversationSurvivesAMissingLetter(InboxTestCase):
    """In-Reply-To names one letter. When the archive does not hold that one —
    it predates the agent, or was sent from elsewhere — References lists the
    rest, and the thread should stay in one piece rather than break in two."""

    def fetch(self, uid, message_id, in_reply_to=None, references=None):
        head = (f"From: b@x.com\nSubject: Re: Thing\n"
                f"Date: Tue, 1 Sep 2026 10:00:00 -0700\n"
                f"Message-ID: {message_id}\n")
        if in_reply_to:
            head += f"In-Reply-To: {in_reply_to}\n"
        if references:
            head += f"References: {references}\n"
        raw = (head + "Content-Type: text/plain\n\nbody").encode()
        fake = FakeIMAP({uid: raw})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()

    def test_references_are_stored(self):
        self.fetch("101", "<a@x>", references="<root@x> <mid@x>")
        self.assertEqual(self.index()[0]["references"], "<root@x> <mid@x>")

    def test_a_reply_joins_the_thread_through_a_letter_not_held(self):
        self.fetch("101", "<root@x>")
        self.fetch("102", "<later@x>", in_reply_to="<missing@x>",
                   references="<root@x> <missing@x>")
        thread = self.inbox.get_thread("<later@x>")
        self.assertEqual([m["message_id"] for m in thread],
                         ["<root@x>", "<later@x>"])

    def test_the_direct_parent_still_wins_when_it_is_held(self):
        self.fetch("101", "<root@x>")
        self.fetch("102", "<mid@x>", in_reply_to="<root@x>",
                   references="<root@x>")
        self.fetch("103", "<leaf@x>", in_reply_to="<mid@x>",
                   references="<root@x> <mid@x>")
        self.assertEqual([m["message_id"] for m in self.inbox.get_thread("<leaf@x>")],
                         ["<root@x>", "<mid@x>", "<leaf@x>"])


class TestARepliesReferencesCarryTheConversation(InboxTestCase):
    """A References header naming only the letter being answered puts a long
    exchange in two pieces in mail programs that thread on it."""

    def sent_message(self, in_reply_to=None):
        sent = {}

        def fake_smtp(*a, **k):
            server = mock.MagicMock()
            server.send_message.side_effect = lambda m, **kw: sent.update(msg=m)
            return server

        with mock.patch.object(agent_inbox.smtplib, "SMTP_SSL", fake_smtp):
            self.inbox.send_email("b@x.com", "Re: Thing", "Body",
                                  in_reply_to=in_reply_to)
        return sent["msg"]

    def test_a_first_letter_has_no_references(self):
        self.assertIsNone(self.sent_message()["References"])

    def test_a_reply_carries_the_whole_chain(self):
        raw = ("From: b@x.com\nSubject: Thing\n"
               "Date: Tue, 1 Sep 2026 10:00:00 -0700\n"
               "Message-ID: <parent@x>\nReferences: <root@x> <mid@x>\n"
               "Content-Type: text/plain\n\nbody").encode()
        fake = FakeIMAP({"101": raw})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        msg = self.sent_message(in_reply_to="<parent@x>")
        self.assertEqual(msg["References"], "<root@x> <mid@x> <parent@x>")
        self.assertEqual(msg["In-Reply-To"], "<parent@x>")

    def test_a_reply_to_a_letter_that_began_a_thread(self):
        raw = ("From: b@x.com\nSubject: Thing\n"
               "Date: Tue, 1 Sep 2026 10:00:00 -0700\n"
               "Message-ID: <parent@x>\nContent-Type: text/plain\n\nbody").encode()
        fake = FakeIMAP({"101": raw})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        self.assertEqual(self.sent_message(in_reply_to="<parent@x>")["References"],
                         "<parent@x>")


class TestOneLetterCannotSpendTheSession(unittest.TestCase):
    """The whole body of every unread letter went into the prompt, uncapped. A
    newsletter runs to a hundred thousand characters, and five of them would
    spend a session's budget before the agent had done anything."""

    def test_a_short_letter_is_untouched(self):
        self.assertEqual(agent_inbox.trim_for_prompt("Short.", "inbox/a.md"),
                         "Short.")

    def test_a_letter_at_the_limit_is_untouched(self):
        body = "x" * agent_inbox.MAX_EMAIL_BODY_CHARS
        self.assertEqual(agent_inbox.trim_for_prompt(body, "inbox/a.md"), body)

    def test_a_long_letter_is_shortened(self):
        out = agent_inbox.trim_for_prompt("x" * 60000, "inbox/a.md")
        self.assertLess(len(out), 13000)

    def test_it_says_where_the_rest_is(self):
        out = agent_inbox.trim_for_prompt("x" * 60000, "inbox/a.md")
        self.assertIn("record/emails/inbox/a.md", out)
        self.assertIn("`read_file`", out)
        self.assertIn("48000 more characters", out)

    def test_nothing_is_lost_from_the_record(self):
        """Only the prompt is shortened. What is on disk is the whole letter."""
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        inbox = AgentInbox("a@example.com", "p", repo)
        long_body = "y" * 60000
        fake = FakeIMAP({"101": raw_email("a@x.com", "S", long_body, "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            inbox.fetch_unread_and_store()
        entry = inbox.load_index()[0]
        with open(os.path.join(repo, "record", "emails", entry["file"]),
                  encoding="utf-8") as f:
            self.assertIn(long_body, f.read())


class TestALetterCanBeSetDown(InboxTestCase):
    """Answering a letter is not the only honest response, and deciding not to
    answer is not the only alternative. Setting one down is a third thing — and
    it only works if it comes back. A label the agent set and is never shown
    again is worse than no label: it feels like something was done."""

    def fetch(self, n=3):
        msgs = {str(100 + i): raw_email(f"p{i}@x.com", f"Subject {i}", f"Body {i}",
                                        f"<{i}@x>") for i in range(n)}
        fake = FakeIMAP(msgs)
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        return [e["id"] for e in self.index()]

    def test_setting_aside_files_it(self):
        ids = self.fetch()
        self.inbox.set_aside(ids[0])
        entry = [e for e in self.index() if e["id"] == ids[0]][0]
        self.assertIn(agent_inbox.REVIEW_LATER, entry["labels"])

    def test_it_comes_back_in_the_standing_summary(self):
        ids = self.fetch()
        self.inbox.set_aside(ids[0])
        self.inbox.set_aside(ids[2])
        summary = self.inbox.set_aside_summary()
        self.assertIn("You set 2 letters aside", summary)
        self.assertIn(ids[0], summary)
        self.assertIn(ids[2], summary)
        self.assertIn("Subject 0", summary)

    def test_nothing_set_aside_says_nothing(self):
        self.fetch()
        self.assertEqual(self.inbox.set_aside_summary(), "")

    def test_one_letter_reads_as_one(self):
        ids = self.fetch()
        self.inbox.set_aside(ids[0])
        self.assertIn("You set 1 letter aside", self.inbox.set_aside_summary())

    def test_picking_it_up_takes_it_off(self):
        ids = self.fetch()
        self.inbox.set_aside(ids[0])
        self.inbox.pick_up(ids[0])
        self.assertEqual(self.inbox.set_aside_summary(), "")

    def test_it_survives_being_marked_read(self):
        """The whole point: a letter dealt with as 'later' is not unread any
        more, so the unread list cannot be what brings it back."""
        ids = self.fetch()
        self.inbox.set_aside(ids[0])
        self.inbox.mark_email_read(ids[0])
        self.assertNotIn(ids[0], self.inbox.unread_for_prompt())
        self.assertIn(ids[0], self.inbox.set_aside_summary())

    def test_setting_the_same_one_aside_twice_says_so(self):
        ids = self.fetch()
        self.inbox.set_aside(ids[0])
        self.assertIn("already", self.inbox.set_aside(ids[0]))

    def test_a_letter_that_does_not_exist(self):
        self.fetch()
        self.assertIn("no letter", self.inbox.set_aside("msg_999"))
        self.assertIn("not set aside", self.inbox.pick_up("msg_999"))


class TestALabelIsVisible(InboxTestCase):
    """A label could be set and then never seen again: nothing showed it on the
    letter, and finding it back meant guessing the exact words it was written
    with."""

    def fetch_one(self):
        fake = FakeIMAP({"101": raw_email("a@x.com", "Hello", "Body", "<1@x>")})
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL", return_value=fake):
            self.inbox.fetch_unread_and_store()
        return self.index()[0]["id"]

    def test_the_letter_shows_how_it_is_filed(self):
        eid = self.fetch_one()
        self.inbox.add_label(eid, "from a stranger")
        self.assertIn("Filed as: from a stranger", self.inbox.unread_for_prompt())

    def test_an_unlabelled_letter_says_nothing(self):
        self.fetch_one()
        self.assertNotIn("Filed as", self.inbox.unread_for_prompt())
        self.assertEqual(agent_inbox.labels_line([]), "")
        self.assertEqual(agent_inbox.labels_line(None), "")

    def test_several_labels_read_as_a_list(self):
        self.assertEqual(agent_inbox.labels_line(["one", "two"]),
                         "Filed as: one, two")

    def test_every_label_can_be_found_without_guessing(self):
        eid = self.fetch_one()
        self.inbox.add_label(eid, "zebra")
        self.inbox.set_aside(eid)
        self.assertEqual(dict(self.inbox.list_labels()),
                         {agent_inbox.REVIEW_LATER: 1, "zebra": 1})

    def test_labels_are_counted_most_used_first(self):
        msgs = {str(100 + i): raw_email(f"p{i}@x.com", f"S{i}", "b", f"<{i}@x>")
                for i in range(3)}
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL",
                               return_value=FakeIMAP(msgs)):
            self.inbox.fetch_unread_and_store()
        ids = [e["id"] for e in self.index()]
        for i in ids:
            self.inbox.add_label(i, "common")
        self.inbox.add_label(ids[0], "rare")
        self.assertEqual(self.inbox.list_labels(), [("common", 3), ("rare", 1)])


class TestOneCallPutsTheWholeSituationInFront(InboxTestCase):
    """The agent is told that a letter it sets down comes back. That is only
    true if whoever wired the library up remembered the second half, so
    mail_for_prompt does both — one line to add is a promise that keeps itself."""

    def two(self):
        msgs = {str(100 + i): raw_email(f"p{i}@x.com", f"Subject {i}", "b",
                                        f"<{i}@x>") for i in range(2)}
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL",
                               return_value=FakeIMAP(msgs)):
            self.inbox.fetch_unread_and_store()
        return [e["id"] for e in self.index()]

    def test_it_carries_both_halves(self):
        ids = self.two()
        self.inbox.set_aside(ids[0])
        self.inbox.mark_email_read(ids[0])
        out = self.inbox.mail_for_prompt()
        self.assertIn("You have 1 unread", out)
        self.assertIn("You set 1 letter aside", out)
        self.assertIn(ids[0], out)

    def test_a_set_aside_letter_survives_leaving_the_unread_list(self):
        ids = self.two()
        self.inbox.set_aside(ids[0])
        self.inbox.mark_email_read(ids[0])
        self.assertNotIn(ids[0], self.inbox.unread_for_prompt())
        self.assertIn(ids[0], self.inbox.mail_for_prompt())

    def test_with_nothing_set_aside_it_is_just_the_unread(self):
        self.two()
        self.assertEqual(self.inbox.mail_for_prompt(),
                         self.inbox.unread_for_prompt())

    def test_an_empty_inbox_says_so_once(self):
        self.assertEqual(self.inbox.mail_for_prompt(), "No unread email.")

    def test_what_the_agent_is_told_matches_what_happens(self):
        """The shipped instructions must not promise more than the library does."""
        self.assertIn("set_aside", agent_inbox.AGENT_TOOL_INSTRUCTIONS)
        self.assertNotIn("every session", agent_inbox.AGENT_TOOL_INSTRUCTIONS)


class TestPickingUpIsNotDismissing(InboxTestCase):
    """A letter set aside has usually been marked read too. Taking the label off
    on its own dropped it out of both lists at once — gone from the waiting list
    and not in the unread one, reachable only by searching for a letter the
    agent would have to remember existed."""

    def one(self):
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL",
                               return_value=FakeIMAP({"101": raw_email(
                                   "p@x.com", "Subject", "Body", "<1@x>")})):
            self.inbox.fetch_unread_and_store()
        return self.index()[0]["id"]

    def test_a_read_letter_comes_back_to_unread(self):
        eid = self.one()
        self.inbox.set_aside(eid)
        self.inbox.mark_email_read(eid)
        self.inbox.pick_up(eid)
        self.assertIn(eid, self.inbox.unread_for_prompt())

    def test_it_does_not_vanish(self):
        eid = self.one()
        self.inbox.set_aside(eid)
        self.inbox.mark_email_read(eid)
        self.inbox.pick_up(eid)
        out = self.inbox.mail_for_prompt()
        self.assertIn(eid, out)
        self.assertNotIn("set 1 letter aside", out)

    def test_an_unread_one_simply_loses_the_label(self):
        eid = self.one()
        self.inbox.set_aside(eid)
        self.inbox.pick_up(eid)
        self.assertIn(eid, self.inbox.unread_for_prompt())
        self.assertEqual(self.inbox.set_aside_summary(), "")

    def test_it_says_where_the_letter_went(self):
        eid = self.one()
        self.inbox.set_aside(eid)
        self.assertIn("unread", self.inbox.pick_up(eid))

    def test_picking_up_something_never_set_aside_changes_nothing(self):
        eid = self.one()
        self.inbox.mark_email_read(eid)
        self.assertIn("was not set aside", self.inbox.pick_up(eid))
        self.assertNotIn(eid, self.inbox.unread_for_prompt())


class TestAnsweringClosesTheLetterOut(InboxTestCase):
    """Answering something is the clearest possible statement that it has been
    dealt with, so it should not also have to be said. Before this, a letter had
    to be marked read by hand after replying — and one that was picked up first
    would sit in the unread list for good if that was forgotten."""

    def one(self, mid="<q@x>"):
        with mock.patch.object(agent_inbox.imaplib, "IMAP4_SSL",
                               return_value=FakeIMAP({"101": raw_email(
                                   "p@x.com", "A question", "Body", mid)})):
            self.inbox.fetch_unread_and_store()
        return self.index()[0]["id"]

    def reply(self, in_reply_to="<q@x>"):
        def fake_smtp(*a, **k):
            srv = mock.MagicMock()
            srv.send_message.side_effect = lambda m, **kw: None
            return srv
        with mock.patch.object(agent_inbox.smtplib, "SMTP_SSL", fake_smtp):
            return self.inbox.send_email("p@x.com", "Re: A question", "Body",
                                         in_reply_to=in_reply_to)

    def status(self, eid):
        return [e for e in self.index() if e["id"] == eid][0]

    def test_replying_marks_the_letter_read(self):
        eid = self.one()
        self.reply()
        self.assertEqual(self.status(eid)["status"], "read")

    def test_replying_takes_it_off_the_set_aside_pile(self):
        eid = self.one()
        self.inbox.set_aside(eid)
        self.reply()
        self.assertEqual(self.inbox.set_aside_summary(), "")
        self.assertNotIn(agent_inbox.REVIEW_LATER, self.status(eid)["labels"])

    def test_the_whole_cycle_ends_empty(self):
        """Set aside, marked read, picked up, answered — nothing left waiting."""
        eid = self.one()
        self.inbox.set_aside(eid)
        self.inbox.mark_email_read(eid)
        self.inbox.pick_up(eid)
        self.assertIn(eid, self.inbox.unread_for_prompt())
        self.reply()
        self.assertEqual(self.inbox.mail_for_prompt(), "No unread email.")

    def test_other_labels_are_left_alone(self):
        eid = self.one()
        self.inbox.add_label(eid, "from a stranger")
        self.inbox.set_aside(eid)
        self.reply()
        self.assertEqual(self.status(eid)["labels"], ["from a stranger"])

    def test_a_first_contact_closes_nothing(self):
        eid = self.one()
        self.reply(in_reply_to=None)
        self.assertEqual(self.status(eid)["status"], "unread")

    def test_a_reply_to_something_not_held_is_harmless(self):
        eid = self.one()
        self.reply(in_reply_to="<never-seen@x>")
        self.assertEqual(self.status(eid)["status"], "unread")

    def test_a_failed_send_closes_nothing(self):
        """It is queued in the outbox, not answered."""
        eid = self.one()
        with mock.patch.object(AgentInbox, "_send_raw_email",
                               return_value=(False, "smtp down")):
            self.inbox.send_email("p@x.com", "Re: A question", "Body",
                                  in_reply_to="<q@x>")
        self.assertEqual(self.status(eid)["status"], "unread")

    def test_the_send_survives_bookkeeping_that_fails(self):
        """The reply has gone. Losing the send over what happens afterwards
        would be the wrong trade."""
        self.one()
        with mock.patch.object(AgentInbox, "_archive_sent"):
            with mock.patch.object(AgentInbox, "save_index",
                                   side_effect=RuntimeError("disk gone")):
                ok, err = self.reply()
        self.assertTrue(ok, err)
