# Live Sales Coach

A real-time, always-on-top desktop overlay that listens to your sales calls, gives you live coaching tips in English, and auto-generates a post-call debrief that's emailed to you and saved as a HubSpot note on the prospect's contact record.

Built on top of:
- **BlackHole** (free macOS virtual audio device) — captures both sides of your call
- **ElevenLabs Scribe v2 Realtime** — live multilingual transcription (Hebrew + English mixed)
- **Anthropic Claude** — coaching tips (Haiku for speed) + post-call debrief (Sonnet for depth)
- **Tkinter** — always-on-top overlay window
- **Gmail SMTP + HubSpot REST API** — auto-deliver the debrief

## What it does

- Listens to your microphone **and** what the prospect says (via BlackHole)
- Streams audio to ElevenLabs for live transcription
- Sends rolling chunks to Claude every ~3 seconds with a customizable sales playbook (Mom Test by Rob Fitzpatrick, by default)
- Displays coaching tips as a floating overlay you keep visible during the call
- Each tip has a ✓ button to dismiss it
- If you go quiet for ~24 seconds, the coach nudges you with a procedural tip
- On **Stop**, generates a structured debrief (summary, what went well, what to improve, discovery score, next move) and:
  - Saves it to `/tmp/sales-call-debrief-*.md` and opens it
  - Emails the summary to you via Gmail
  - Posts the summary as a note on the prospect's HubSpot contact

## Requirements

- macOS (the overlay uses tkinter + `afplay`; BlackHole is macOS-only)
- Python 3.9+ with `portaudio` (`brew install portaudio`)
- [BlackHole 2ch](https://existential.audio/blackhole/) (`brew install --cask blackhole-2ch`)
- An ElevenLabs API key with Speech-to-Text permission
- An Anthropic API key
- (Optional) Gmail App Password for auto-email
- (Optional) HubSpot Service Key for auto-note

## Install

```bash
git clone https://github.com/<YOUR-GITHUB-USERNAME>/live-sales-coach.git
cd live-sales-coach

# Python deps
pip3 install -r requirements.txt

# BlackHole virtual audio (asks for admin password)
brew install --cask blackhole-2ch

# Restart your Mac, then refresh Core Audio
sudo killall coreaudiod
```

## Configure macOS audio (one-time)

Open **Audio MIDI Setup** and create two virtual devices:

### 1. Multi-Output Device — so you hear the call AND BlackHole captures it
- Click `+` (bottom-left) → **Create Multi-Output Device**
- Check: ✅ your speakers / headphones (e.g., MacBook Pro Speakers, AirPods) **and** ✅ BlackHole 2ch
- In macOS **System Settings → Sound → Output**, pick **Multi-Output Device**

### 2. Aggregate Device named `Coach input` — combines BlackHole + your mic
- Click `+` (bottom-left) → **Create Aggregate Device**
- Rename it to `Coach input` (the script auto-detects this name)
- Check: ✅ BlackHole 2ch **and** ✅ your microphone

In **Zoom → Settings → Audio**:
- **Speaker**: Multi-Output Device
- **Microphone**: your built-in microphone (do NOT pick AirPods — drops audio quality)

## Configure env vars

```bash
cp .env.example .env
# Edit .env and fill in your keys, then load them:
set -a; source .env; set +a
```

Or add the exports to your `~/.zshrc` so they're always available.

## Customize the playbook

Copy `playbook.example.md` to `playbook.md` and edit it for your business, your services, your call stages, and what you want the coach to flag. The playbook is the system prompt — Claude follows it strictly.

```bash
cp playbook.example.md playbook.md
# edit playbook.md in your editor of choice
```

## Run

**Easiest — double-click the launcher:**

In Finder, double-click `Sales Coach.command`. macOS opens Terminal, loads your `.env`, and launches the overlay. Quit the overlay window to stop.

> First time only: macOS may say "cannot be opened because it's from an unidentified developer." Right-click the file → **Open** → confirm. After that it'll launch with a regular double-click.

**Or from the terminal:**

```bash
python3 coach.py
```

Either way, the overlay window appears in the bottom-right of your screen.

**Per-call workflow:**
1. Type the prospect's email in the **Contact email** field (optional but recommended — needed for HubSpot note)
2. Click **▶ Start** when the call begins
3. Run the call — tips appear within ~3 seconds of you speaking
4. Click **■ Stop** when the call ends
5. Within ~15 seconds: the debrief opens, lands in your inbox, and appears as a note on the HubSpot contact

## CLI flags

```bash
python3 coach.py --whisper       # use local Whisper instead of ElevenLabs
python3 coach.py --deepgram      # use Deepgram instead of ElevenLabs (needs DEEPGRAM_API_KEY)
python3 coach.py --lang he       # force Hebrew (default: auto)
python3 coach.py --lang en       # force English
python3 coach.py --device 4      # pick a specific audio input device index
python3 coach.py --list-devices  # list available audio inputs
python3 coach.py --demo          # demo mode with fake transcript (no audio needed)
```

## Project layout

```
coach.py            # main orchestrator (overlay + threading + delivery)
overlay.py          # always-on-top tkinter UI
transcriber.py      # audio capture + STT (ElevenLabs / Deepgram / Whisper)
advisor.py          # Claude coaching engine (live tips + post-call debrief)
playbook.md         # YOUR personal coaching rules (gitignored)
playbook.example.md # template with Mom Test rules baked in
```

## Cost notes

- **ElevenLabs Scribe v2 Realtime**: roughly $0.40/hr of audio (check current pricing)
- **Anthropic Haiku**: tiny — ~$0.01 per 5-min call for live tips
- **Anthropic Sonnet (debrief)**: ~$0.02 per call
- **Gmail / HubSpot**: free

Total per 30-min call: ~$0.25.

## Privacy

- Audio is sent to ElevenLabs for transcription
- Transcripts are sent to Anthropic for coaching + debrief
- Email goes through your own Gmail
- HubSpot notes go to your own HubSpot account
- Nothing is sent anywhere else — all processing is between you and the providers you've authenticated

## License

MIT — see [LICENSE](LICENSE).

## Credits

- Sales methodology: [The Mom Test](http://momtestbook.com/) by Rob Fitzpatrick
- Original `sales-coach` skill scaffolding by [@aviz85](https://github.com/aviz85/claude-skills-library)
- ElevenLabs Scribe integration adapted from the [live-transcribe](https://github.com/aviz85/claude-skills-library/tree/main/plugins/live-transcribe) plugin
