#!/usr/bin/env python3
"""Sales Coach — main orchestrator.

Captures call audio via BlackHole, transcribes live, sends to Claude for
coaching tips, and displays them in an always-on-top overlay.

Usage:
    python3 coach.py                  # Deepgram + auto-detect BlackHole
    python3 coach.py --whisper        # Use local Whisper instead
    python3 coach.py --device 3       # Specify audio device index
    python3 coach.py --list-devices   # List audio input devices
    python3 coach.py --demo           # Run with fake transcript (no audio)
"""

import argparse
import json
import os
import queue
import signal
import smtplib
import subprocess
import sys
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from urllib import request as urlrequest, error as urlerror

# Load .env from skill directory
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from overlay import CoachOverlay, run_overlay
from advisor import SalesAdvisor
from transcriber import (
    ElevenLabsTranscriber,
    DeepgramTranscriber,
    WhisperTranscriber,
    find_blackhole_device,
    find_input_device,
    list_audio_devices,
)

# How many seconds of transcript to accumulate before sending to Claude.
# Lower = snappier tips, higher = bigger context per call.
COACHING_INTERVAL = 3

# ---------------- Post-call delivery helpers ----------------

HUBSPOT_API_BASE = os.environ.get("HUBSPOT_API_BASE", "https://api.hubapi.com")


def send_debrief_email(recipient: str, subject: str, body_markdown: str) -> tuple[bool, str]:
    """Send the debrief summary by email. Returns (ok, message)."""
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        return False, "GMAIL_USER / GMAIL_APP_PASSWORD not set"
    if not recipient:
        return False, "no recipient"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(body_markdown)
    # Also send HTML version so Markdown renders better in Gmail
    html_body = "<pre style=\"font-family:-apple-system,Helvetica,sans-serif;font-size:14px;white-space:pre-wrap;\">" \
                + body_markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") \
                + "</pre>"
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True, f"sent to {recipient}"
    except Exception as e:
        return False, f"SMTP error: {e}"


def hubspot_find_contact_id(email: str) -> tuple[str | None, str]:
    """Look up a HubSpot contact ID by email. Returns (contact_id_or_None, message)."""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        return None, "HUBSPOT_ACCESS_TOKEN not set"
    if not email:
        return None, "no email"

    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = json.dumps({
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "limit": 1,
        "properties": ["email", "firstname", "lastname"],
    }).encode("utf-8")
    req = urlrequest.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])
        if not results:
            return None, f"no contact found for {email}"
        return results[0]["id"], f"found contact {email} (id {results[0]['id']})"
    except urlerror.HTTPError as e:
        return None, f"HubSpot search HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}"
    except Exception as e:
        return None, f"HubSpot search error: {e}"


def hubspot_add_note(contact_id: str, body_markdown: str) -> tuple[bool, str]:
    """Attach a Note engagement to a contact. Uses the v1 engagements API
    (works with contacts.write scope, doesn't need separate notes scope)."""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        return False, "HUBSPOT_ACCESS_TOKEN not set"

    # HubSpot Notes render HTML; convert minimal markdown to HTML so the note is readable.
    html_body = (
        body_markdown
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\n", "<br>")
    )

    url = f"{HUBSPOT_API_BASE}/engagements/v1/engagements"
    payload = json.dumps({
        "engagement": {"active": True, "type": "NOTE", "timestamp": int(time.time() * 1000)},
        "associations": {"contactIds": [int(contact_id)]},
        "metadata": {"body": html_body},
    }).encode("utf-8")
    req = urlrequest.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        engagement_id = data.get("engagement", {}).get("id", "?")
        return True, f"note added (engagement id {engagement_id})"
    except urlerror.HTTPError as e:
        return False, f"HubSpot note HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}"
    except Exception as e:
        return False, f"HubSpot note error: {e}"


def main():
    parser = argparse.ArgumentParser(description="Real-time Sales Coach")
    parser.add_argument("--deepgram", action="store_true", help="Use Deepgram instead of ElevenLabs")
    parser.add_argument("--whisper", action="store_true", help="Use local Whisper instead of ElevenLabs")
    parser.add_argument("--lang", type=str, default="auto", help="ISO language code for ElevenLabs (default: auto = Hebrew+English auto-detect). Use 'he' to force Hebrew, 'en' to force English.")
    parser.add_argument("--device", type=int, default=None, help="Audio input device index")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--demo", action="store_true", help="Run demo mode (no audio needed)")
    args = parser.parse_args()

    if args.list_devices:
        print("Available audio input devices:")
        list_audio_devices()
        return

    # Shared queue for UI updates
    tip_queue = queue.Queue()

    if args.demo:
        run_demo(tip_queue)
        return

    # Resolve audio device. Priority:
    #   1. Aggregate device named "Coach Input" (BlackHole + mic combined — captures both call sides)
    #   2. Any device with "aggregate" in name
    #   3. BlackHole (captures only the other party / system output)
    #   4. MacBook mic (captures only you)
    device = args.device
    if device is None:
        for candidate in ("coach input", "coach", "aggregate"):
            found = find_input_device(candidate)
            if found is not None:
                device = found
                print(f"Auto-detected aggregate device '{candidate}' at index {device} — capturing both call sides")
                break
        if device is None:
            device = find_blackhole_device()
            if device is not None:
                print(f"Auto-detected BlackHole at device index {device} — capturing other party only")
                print("(For both sides, create an Aggregate Device named 'Coach Input' with BlackHole + your mic)")
        if device is None:
            mac_mic = find_input_device("macbook")
            if mac_mic is not None:
                device = mac_mic
                print(f"BlackHole not found — falling back to MacBook mic at device index {device}")
                print("(For real call audio, install: brew install --cask blackhole-2ch + Multi-Output)")
            else:
                print("WARNING: No BlackHole or MacBook mic found. Using system default input.")

    # Transcript buffer & session state
    transcript_buffer = []              # cleared every coaching tick (for live tips)
    session_transcript = []             # FULL session transcript (kept for debrief)
    session_started_at = [None]         # epoch timestamp when current session began
    session_contact_email = [""]        # HubSpot contact email for this session
    buffer_lock = threading.Lock()
    session_active = threading.Event()  # cleared = idle, set = recording
    current_transcriber = [None]        # mutable holder for the live instance

    def on_transcript(text):
        if not session_active.is_set():
            return
        with buffer_lock:
            transcript_buffer.append(text)
            session_transcript.append(text)
        print(f"[committed] {text}", file=sys.stderr, flush=True)
        tip_queue.put({"type": "transcript", "text": text})

    def on_partial(text):
        # Streaming text — push to overlay only, never to advisor buffer
        if not session_active.is_set():
            return
        tip_queue.put({"type": "transcript", "text": text})

    def make_transcriber():
        if args.whisper:
            return WhisperTranscriber(on_transcript, device_index=device)
        if args.deepgram:
            return DeepgramTranscriber(on_transcript, device_index=device)
        return ElevenLabsTranscriber(
            on_transcript,
            device_index=device,
            language_code=args.lang,
            partial_callback=on_partial,
        )

    # Start advisor (always loaded, only called when active)
    advisor = SalesAdvisor()

    def coaching_loop():
        """Send accumulated transcript to Claude every COACHING_INTERVAL.
        During silence, send a 'no new audio' nudge every SILENCE_NUDGE_TICKS
        empty cycles so the coach keeps guiding."""
        SILENCE_NUDGE_TICKS = 8   # 8 × 3s = ~24s of silence → silence nudge
        empty_ticks = 0
        while True:
            time.sleep(COACHING_INTERVAL)
            if not session_active.is_set():
                empty_ticks = 0
                continue

            with buffer_lock:
                if transcript_buffer:
                    chunk = " ".join(transcript_buffer)
                    transcript_buffer.clear()
                    is_silence_nudge = False
                else:
                    chunk = None
                    is_silence_nudge = False
                    empty_ticks += 1
                    if empty_ticks >= SILENCE_NUDGE_TICKS:
                        is_silence_nudge = True
                        empty_ticks = 0
                        chunk = "[SYSTEM: ~24 seconds of silence — no one has spoken. Suggest one concrete next move (e.g., follow-up question, summary check, move to budget) based on the call so far. Stay in English. Max 2 lines.]"

            if chunk is None:
                print("[coach] tick — buffer empty, skipping", file=sys.stderr, flush=True)
                continue

            if not is_silence_nudge:
                empty_ticks = 0

            label = "SILENCE NUDGE" if is_silence_nudge else "transcript"
            print(f"[coach] sending {label}: {chunk[:120]}...", file=sys.stderr, flush=True)
            try:
                tips = advisor.get_tips(chunk)
                print(f"[coach] Claude returned {len(tips)} tip(s): {tips}", file=sys.stderr, flush=True)
                for tip in tips:
                    tip_queue.put({"type": "tip", "text": tip})
            except Exception as e:
                print(f"[coach] ERROR: {e}", file=sys.stderr, flush=True)
                tip_queue.put({"type": "tip", "text": f"WARN: Coach error: {e}"})

    def start_session(contact_email=""):
        """Called by overlay's Start button. `contact_email` is the HubSpot lookup key."""
        if session_active.is_set():
            return

        def _starter():
            tip_queue.put({"type": "status", "text": "CONNECTING..."})
            t = make_transcriber()
            try:
                t.start()
            except Exception as e:
                tip_queue.put({"type": "status", "text": "ERROR"})
                tip_queue.put({"type": "tip", "text": f"WARN: Failed to start: {e}"})
                return
            current_transcriber[0] = t
            # Fresh session — reset transcript so debrief is per-call
            with buffer_lock:
                transcript_buffer.clear()
                session_transcript.clear()
            session_started_at[0] = time.time()
            session_contact_email[0] = (contact_email or "").strip().lower()
            session_active.set()
            tip_queue.put({"type": "status", "text": "● LIVE"})
            if session_contact_email[0]:
                tip_queue.put({"type": "transcript", "text": f"Listening — contact: {session_contact_email[0]}"})
            else:
                tip_queue.put({"type": "transcript", "text": "Listening — speak now (no HubSpot contact set)"})

            # Proactive opening guidance — Danit asked for guidance from the start,
            # not just reactive coaching. Seed the overlay with the intro-stage moves.
            tip_queue.put({"type": "tip", "text": "→ Intro (5 min, ~30% you): Open with anchor question. NO pitch."})
            tip_queue.put({"type": "tip", "text": "→ Try: 'Walk me through the last campaign you ran — what worked, what didn't?'"})
            tip_queue.put({"type": "tip", "text": "→ Next, push for: pipeline pain, current stack, past AI attempts, decision-maker."})

        threading.Thread(target=_starter, daemon=True).start()

    def stop_session():
        """Called by overlay's Stop button. Triggers post-call debrief."""
        if not session_active.is_set():
            return
        session_active.clear()
        t = current_transcriber[0]
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
            current_transcriber[0] = None

        # Snapshot the full transcript for debrief
        with buffer_lock:
            full_transcript = " ".join(session_transcript)
            transcript_buffer.clear()
        started_at = session_started_at[0]
        session_started_at[0] = None

        tip_queue.put({"type": "status", "text": "STOPPED"})

        # Decide whether to debrief
        word_count = len(full_transcript.split())
        if word_count < 30:
            tip_queue.put({"type": "transcript", "text": f"Session ended (only {word_count} words — no debrief). Click Start for next call."})
            return

        contact_email = session_contact_email[0]
        session_contact_email[0] = ""

        # Run debrief in background so UI stays responsive
        def _debrief():
            tip_queue.put({"type": "tip", "text": "→ Generating post-call debrief…"})
            ts = time.strftime("%Y%m%d-%H%M%S")
            out_path = Path(f"/tmp/sales-call-debrief-{ts}.md")
            try:
                debrief_text = advisor.get_debrief(full_transcript)
            except Exception as e:
                print(f"[debrief] ERROR: {e}", file=sys.stderr, flush=True)
                tip_queue.put({"type": "tip", "text": f"WARN: Debrief failed: {e}"})
                return

            duration_min = int((time.time() - started_at) / 60) if started_at else 0
            header = (
                f"# Sales Call Debrief — {ts}\n\n"
                f"Duration: {duration_min} min · Words: {word_count}"
                + (f" · Contact: {contact_email}" if contact_email else "")
                + "\n\n---\n\n"
            )

            # Summary = debrief only (NO transcript). Used for email + HubSpot note.
            summary_md = header + debrief_text

            # Full = summary + full transcript appendix. Saved to disk for reference.
            appendix = f"\n\n---\n\n## Full transcript\n\n{full_transcript}\n"
            out_path.write_text(summary_md + appendix, encoding="utf-8")

            tip_queue.put({"type": "tip", "text": f"✅ Debrief saved: {out_path.name}"})

            # Open the file in the default Markdown viewer (full version)
            try:
                subprocess.Popen(["open", str(out_path)])
            except Exception:
                pass

            # --- Send DEBRIEF email to Danit (summary only, no transcript) ---
            recipient = os.environ.get("GMAIL_USER", "")
            subject_contact = f" — {contact_email}" if contact_email else ""
            debrief_subject = f"[Coach] Debrief{subject_contact} ({duration_min} min)"
            ok, msg = send_debrief_email(recipient, debrief_subject, summary_md)
            if ok:
                tip_queue.put({"type": "tip", "text": f"✉️ Debrief email {msg}"})
            else:
                tip_queue.put({"type": "tip", "text": f"WARN: Debrief email failed — {msg}"})

            # --- Draft CLIENT-FACING follow-up email and send DRAFT to Danit for approval ---
            try:
                followup_draft = advisor.get_followup_draft(full_transcript, contact_email)
            except Exception as e:
                print(f"[followup] ERROR: {e}", file=sys.stderr, flush=True)
                tip_queue.put({"type": "tip", "text": f"WARN: Follow-up draft failed — {e}"})
                followup_draft = ""

            if followup_draft:
                # Wrap with clear approval header so Danit knows to review before sending
                approval_body = (
                    f"DRAFT FOLLOW-UP EMAIL — review before sending\n"
                    f"Suggested recipient: {contact_email or '<set contact email in overlay next time>'}\n"
                    f"{'=' * 60}\n\n"
                    f"{followup_draft}\n\n"
                    f"{'=' * 60}\n"
                    f"This draft was generated by your Sales Coach based on the call transcript.\n"
                    f"Reply to this email with edits, or copy the body above into a new email to the prospect.\n"
                )
                followup_subject = f"[Coach DRAFT] Follow-up{subject_contact} — review & send"
                ok2, msg2 = send_debrief_email(recipient, followup_subject, approval_body)
                if ok2:
                    tip_queue.put({"type": "tip", "text": f"✉️ Follow-up DRAFT {msg2} (review in inbox)"})
                else:
                    tip_queue.put({"type": "tip", "text": f"WARN: Follow-up email failed — {msg2}"})

                # Also append the draft to the on-disk debrief file
                try:
                    with out_path.open("a", encoding="utf-8") as f:
                        f.write("\n\n---\n\n## Suggested follow-up email (DRAFT — review before sending)\n\n")
                        f.write("```\n" + followup_draft + "\n```\n")
                except Exception:
                    pass

            # --- Post HubSpot note (summary only, no transcript) ---
            if not contact_email:
                tip_queue.put({"type": "tip", "text": "⚠️ No contact email entered — skipping HubSpot"})
            else:
                cid, lookup_msg = hubspot_find_contact_id(contact_email)
                if cid is None:
                    tip_queue.put({"type": "tip", "text": f"WARN: HubSpot lookup — {lookup_msg}"})
                else:
                    ok, note_msg = hubspot_add_note(cid, summary_md)
                    if ok:
                        tip_queue.put({"type": "tip", "text": f"📎 HubSpot {note_msg}"})
                    else:
                        tip_queue.put({"type": "tip", "text": f"WARN: HubSpot note — {note_msg}"})

        threading.Thread(target=_debrief, daemon=True).start()

    # Background coaching loop runs always, but only fires when session is active.
    coach_thread = threading.Thread(target=coaching_loop, daemon=True)
    coach_thread.start()

    # Run overlay in main thread (tkinter requires it)
    def cleanup(*_):
        if current_transcriber[0] is not None:
            try:
                current_transcriber[0].stop()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)

    # Initial idle state
    tip_queue.put({"type": "status", "text": "STOPPED"})
    tip_queue.put({"type": "transcript", "text": "Click Start when you're in a call"})

    try:
        overlay = CoachOverlay(tip_queue, on_start=start_session, on_stop=stop_session)
        overlay.start()  # blocks
    finally:
        if current_transcriber[0] is not None:
            try:
                current_transcriber[0].stop()
            except Exception:
                pass


def run_demo(tip_queue):
    """Demo mode — simulates a call with preset transcript and coaching."""
    advisor = SalesAdvisor()

    demo_transcript = [
        "Sales rep: Hey Sarah, thanks for hopping on the call. So I wanted to walk you through everything our platform can do. We've got real-time analytics, custom dashboards, API integrations...",
        "Prospect: Okay, sure. But honestly we're mostly just trying to figure out why our sales team's close rate dropped from 30% to 18% last quarter.",
        "Sales rep: Right, yeah. So our analytics would totally help with that. We also have this cool AI feature that predicts deal outcomes and we just launched a new mobile app...",
        "Prospect: I mean... do you have anything specifically for diagnosing pipeline issues? Like where deals are falling off?",
        "Sales rep: Absolutely! Let me share my screen and show you a quick demo of the full platform. It'll probably take about 20 minutes to walk through everything.",
        "Prospect: Actually I only have about 10 more minutes. Can we focus on the pipeline stuff?",
        "Sales rep: Oh sure, no problem. So basically if you're interested, I can set you up with a trial and you can explore it yourself. Just let me know what you think.",
    ]

    def demo_loop():
        time.sleep(2)
        for i, line in enumerate(demo_transcript):
            tip_queue.put({"type": "transcript", "text": line})
            time.sleep(1)

            tips = advisor.get_tips(line)
            for tip in tips:
                tip_queue.put({"type": "tip", "text": tip})

            if i < len(demo_transcript) - 1:
                time.sleep(4)

        tip_queue.put({"type": "status", "text": "DEMO COMPLETE"})

    t = threading.Thread(target=demo_loop, daemon=True)
    t.start()

    overlay = CoachOverlay(tip_queue)
    overlay.start()


if __name__ == "__main__":
    main()
