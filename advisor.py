"""Claude-powered coaching engine. Sends transcript chunks and returns tips."""

from __future__ import annotations

import os
import re
import anthropic
from pathlib import Path

PLAYBOOK_PATH = Path(__file__).parent / "playbook.md"
# Haiku for the live coaching loop — much faster (~1-2s) than Sonnet, still strong
# on short rule-following responses. Debrief uses a different model below.
MODEL = "claude-haiku-4-5"
DEBRIEF_MODEL = "claude-sonnet-4-6"

def load_playbook() -> str:
    return PLAYBOOK_PATH.read_text()

def build_system_prompt(playbook: str) -> str:
    return f"""{playbook}

You receive rolling chunks of a live sales call transcript. Each chunk is what was said in the last few seconds. Apply the playbook strictly:

- Max 2 lines. No preamble. Action-only. English only.
- Use ⚠️ / ✅ / → as defined in the playbook.
- ALWAYS produce a useful nudge — never reply "OK" or stay silent. If nothing dramatic happened, suggest the next move based on the call stage (e.g., "→ Now pivot to budget", "→ Anchor: ask about the last campaign they ran", "→ You're talking too much — ask a question").
- Never repeat a tip you already gave in this conversation. Always advance.
- Honor the quiet/back pause command.
"""


class SalesAdvisor:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.playbook = load_playbook()
        self.system_prompt = build_system_prompt(self.playbook)
        self.history = []
        self.muted = False

    def _check_pause_commands(self, text: str) -> None:
        """Toggle muted state based on 'quiet' / 'back' commands in transcript."""
        lowered = text.lower()
        last_quiet = max([m.start() for m in re.finditer(r"\bquiet\b", lowered)], default=-1)
        last_back = max([m.start() for m in re.finditer(r"\bback\b", lowered)], default=-1)
        if last_quiet == -1 and last_back == -1:
            return
        if last_quiet > last_back:
            self.muted = True
        elif last_back > last_quiet:
            self.muted = False

    def get_tips(self, transcript_chunk: str) -> list[str]:
        """Send a transcript chunk to Claude and get coaching tips back."""
        self._check_pause_commands(transcript_chunk)
        if self.muted:
            return []

        self.history.append({
            "role": "user",
            "content": f"[LIVE TRANSCRIPT]\n{transcript_chunk}"
        })

        if len(self.history) > 40:
            self.history = self.history[-20:]

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=self.system_prompt,
            messages=self.history,
        )

        reply = response.content[0].text.strip()
        self.history.append({"role": "assistant", "content": reply})

        if not reply:
            return []
        # Drop literal "OK" lines if the model still sends them, but keep everything else
        lines = [line.strip() for line in reply.split("\n") if line.strip() and line.strip() != "OK"]
        return lines[:2]

    def get_debrief(self, full_transcript: str) -> str:
        """Generate a post-call summary and feedback from the full transcript."""
        debrief_system = f"""{self.playbook}

You just finished coaching a live sales call. Now write a post-call debrief.

# Output format (Markdown)
## Call summary
2–4 sentence summary of what happened on the call.

## What went well ✅
3–5 specific bullets — quote or paraphrase actual moments from the transcript.

## What to improve ⚠️
3–5 specific bullets — be direct and concrete. Reference the playbook rules where relevant (budget, decision-maker, timeline, past attempts, red flags).

## Discovery score
| Item | Asked? | Answer captured |
|---|---|---|
| Budget | Y / N | … |
| Decision-maker | Y / N | … |
| Timeline | Y / N | … |
| Past attempts | Y / N | … |

## Next move
2–3 bullets — what to send / do in the next 24 hours to keep this deal moving (or what to send if disqualifying).

# Rules
- Write the ENTIRE debrief in English, even if the call was in Hebrew.
- Be specific to THIS call. Quote the actual transcript, do not give generic advice.
- Be honest — if she did something wrong, say so plainly.
"""

        response = self.client.messages.create(
            model=DEBRIEF_MODEL,  # Sonnet for the longer, more analytical debrief
            max_tokens=2000,
            system=debrief_system,
            messages=[{
                "role": "user",
                "content": f"[FULL CALL TRANSCRIPT]\n\n{full_transcript}\n\n[END OF TRANSCRIPT]\n\nWrite the debrief now."
            }],
        )
        return response.content[0].text.strip()


if __name__ == "__main__":
    advisor = SalesAdvisor()
    test_chunks = [
        "Sales rep: Hi, thanks for taking my call. Tell me about your event.",
        "Prospect: We're planning a big launch event in 3 weeks for our new SaaS platform.",
        "Sales rep: Great, what's the budget you've allocated?",
        "Prospect: Oh, we haven't really figured that out yet. But your work looks amazing, you're so talented!",
    ]
    for chunk in test_chunks:
        print(f"\n--- Chunk ---\n{chunk}")
        tips = advisor.get_tips(chunk)
        for tip in tips:
            print(f"  {tip}")
