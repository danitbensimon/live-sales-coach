# sales-coach

Real-time AI sales coaching overlay. Captures call audio via BlackHole, transcribes live with Deepgram (or local Whisper), sends transcript chunks to Claude with a sales playbook, and displays coaching tips in an always-on-top desktop window.

## Trigger
`/sales-coach`

## Usage
```
/sales-coach              # Start coaching session with defaults
/sales-coach --playbook   # Open/edit the sales playbook
/sales-coach --device     # List and select audio input device
/sales-coach --whisper    # Use local Whisper instead of Deepgram
```

## Architecture
```
BlackHole (virtual audio) --> sounddevice capture
    --> Deepgram streaming STT (or local Whisper)
        --> transcript buffer (rolling window)
            --> Claude API (with sales playbook system prompt)
                --> tkinter always-on-top overlay
```

## Files
- `SKILL.md` — this file
- `coach.py` — main orchestrator
- `transcriber.py` — audio capture + STT (Deepgram/Whisper)
- `advisor.py` — Claude coaching engine
- `overlay.py` — tkinter always-on-top UI
- `playbook.md` — default sales playbook (system prompt)
- `requirements.txt` — Python dependencies

## Requirements
- macOS with BlackHole virtual audio device
- Python 3.9+ with: sounddevice, numpy, anthropic, deepgram-sdk (or openai-whisper)
- portaudio (via Homebrew)
- Anthropic API key in env (`ANTHROPIC_API_KEY`)
- Deepgram API key in env (`DEEPGRAM_API_KEY`) — unless using `--whisper`

## Setup
```bash
brew install portaudio
brew install --cask blackhole-2ch
pip3 install -r requirements.txt
# Configure macOS Multi-Output Device to include BlackHole
```

## Instructions for Claude
When the user invokes `/sales-coach`:
1. Check that BlackHole and dependencies are installed
2. Run `python3 ~/.claude/skills/sales-coach/coach.py` to start the session
3. The overlay window appears and coaching begins automatically
4. Ctrl+C or closing the window ends the session
