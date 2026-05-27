"""Audio capture via sounddevice + transcription via ElevenLabs Scribe v2 Realtime,
Deepgram streaming, or local Whisper."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import sys
import threading
import time
import numpy as np

# Attempt imports — fail gracefully with clear messages
try:
    import sounddevice as sd
except ImportError:
    sd = None

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_DURATION = 0.5  # seconds per audio block
CHUNK_SECONDS = 5     # seconds of audio before sending to STT


def list_audio_devices():
    """List available audio input devices."""
    if sd is None:
        print("ERROR: sounddevice not installed. Run: pip3 install sounddevice")
        return []
    devices = sd.query_devices()
    inputs = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            inputs.append((i, d["name"], d["max_input_channels"]))
            print(f"  [{i}] {d['name']} ({d['max_input_channels']} ch)")
    return inputs


def find_blackhole_device() -> int | None:
    """Find BlackHole device index."""
    if sd is None:
        return None
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if "blackhole" in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    return None


def find_input_device(name_substring: str) -> int | None:
    """Find an input device whose name contains the given substring (case-insensitive).
    Indices shift when devices come/go, so resolve at runtime."""
    if sd is None:
        return None
    needle = name_substring.lower()
    for i, d in enumerate(sd.query_devices()):
        if needle in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    return None


class ElevenLabsTranscriber:
    """Stream audio to ElevenLabs Scribe v2 Realtime via WebSocket.

    Runs an asyncio event loop in a worker thread; the sounddevice callback
    pushes PCM chunks onto the loop's queue via run_coroutine_threadsafe.
    """

    WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    EL_SAMPLE_RATE = 16000
    CHUNK_DURATION_MS = 100

    def __init__(self, transcript_callback, device_index=None, language_code="he", partial_callback=None):
        self.transcript_callback = transcript_callback   # called on COMMITTED text (sent to advisor)
        self.partial_callback = partial_callback         # called on PARTIAL text (display only)
        self.device_index = device_index
        self.language_code = language_code
        self._running = False
        self._loop = None
        self._thread = None
        self._audio_queue = None  # asyncio.Queue, created inside loop thread
        self._stream = None
        self._api_key = None

    def start(self):
        try:
            import websockets  # noqa: F401
        except ImportError:
            print("ERROR: websockets not installed. Run: pip3 install websockets")
            return

        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            print("ERROR: ELEVENLABS_API_KEY not set in environment")
            return
        self._api_key = api_key

        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

        # Wait for the loop to initialize its queue
        deadline = time.time() + 3
        while self._audio_queue is None and time.time() < deadline:
            time.sleep(0.05)
        if self._audio_queue is None:
            print("ERROR: ElevenLabs event loop failed to start", file=sys.stderr)
            self._running = False
            return

        # Determine how many input channels this device actually exposes.
        # Aggregate devices (BlackHole + MacBook mic) have 3+ channels; we mix to mono.
        if self.device_index is not None:
            dev_info = sd.query_devices(self.device_index)
            in_channels = max(1, int(dev_info.get("max_input_channels", 1)))
        else:
            in_channels = CHANNELS
        self._in_channels = in_channels

        chunk_samples = int(self.EL_SAMPLE_RATE * self.CHUNK_DURATION_MS / 1000)
        kwargs = {
            "samplerate": self.EL_SAMPLE_RATE,
            "channels": in_channels,
            "dtype": "int16",
            "blocksize": chunk_samples,
            "callback": self._audio_callback,
        }
        if self.device_index is not None:
            kwargs["device"] = self.device_index

        self._stream = sd.InputStream(**kwargs)
        self._stream.start()
        dev_str = self.device_index if self.device_index is not None else "default"
        print(f"ElevenLabs Scribe v2 transcription started (lang={self.language_code}, device={dev_str}, channels={in_channels})")

    def _audio_callback(self, indata, frames, time_info, status):
        if not self._running or self._loop is None or self._audio_queue is None:
            return
        # Mix down to mono if device gave us multiple channels.
        # indata shape is (frames, channels) int16.
        # We SUM (with clipping) rather than average — averaging would divide a
        # single-channel signal by N, dropping it below ElevenLabs' VAD threshold.
        # Summing preserves full amplitude when only one channel has signal.
        if indata.shape[1] > 1:
            summed = indata.astype(np.int32).sum(axis=1)
            mono = np.clip(summed, -32768, 32767).astype(np.int16).reshape(-1, 1)
        else:
            mono = indata.copy()
        try:
            asyncio.run_coroutine_threadsafe(
                self._audio_queue.put(mono),
                self._loop,
            )
        except Exception:
            pass

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._audio_queue = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._ws_session())
        except Exception as e:
            print(f"ElevenLabs WS error: {e}", file=sys.stderr)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _ws_session(self):
        import websockets

        ws_params = {
            "model_id": "scribe_v2_realtime",
            "audio_format": f"pcm_{self.EL_SAMPLE_RATE}",
            "commit_strategy": "vad",
            "include_timestamps": "false",
            "no_verbatim": "false",
        }
        # Only pass language_code if explicitly set. Omitting it lets
        # ElevenLabs Scribe v2 auto-detect (handles Hebrew + English mix).
        if self.language_code and self.language_code.lower() not in ("auto", "multi", ""):
            ws_params["language_code"] = self.language_code
        query = "&".join(f"{k}={v}" for k, v in ws_params.items())
        url = f"{self.WS_URL}?{query}"
        headers = {"xi-api-key": self._api_key}

        async with websockets.connect(url, additional_headers=headers, ping_interval=20) as ws:

            async def sender():
                while self._running:
                    try:
                        chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue
                    pcm_bytes = chunk.tobytes()
                    b64 = base64.b64encode(pcm_bytes).decode("ascii")
                    msg = {
                        "message_type": "input_audio_chunk",
                        "audio_base_64": b64,
                        "sample_rate": self.EL_SAMPLE_RATE,
                    }
                    try:
                        await ws.send(json.dumps(msg))
                    except websockets.exceptions.ConnectionClosed:
                        self._running = False
                        break

            async def receiver():
                while self._running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        self._running = False
                        break
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    msg_type = data.get("message_type", "")
                    if msg_type == "partial_transcript":
                        # Streaming text — show live but don't send to advisor
                        text = (data.get("text") or "").strip()
                        if text and self.partial_callback:
                            try:
                                self.partial_callback(text)
                            except Exception:
                                pass
                    elif msg_type in ("committed_transcript", "committed_transcript_with_timestamps"):
                        text = (data.get("text") or "").strip()
                        if text:
                            self.transcript_callback(text)
                    elif "error" in msg_type:
                        print(f"ElevenLabs error: {data}", file=sys.stderr)
                        self._running = False
                        break

            await asyncio.gather(sender(), receiver())

    def stop(self):
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


class DeepgramTranscriber:
    """Stream audio to Deepgram for live transcription."""

    def __init__(self, transcript_callback, device_index=None):
        self.transcript_callback = transcript_callback
        self.device_index = device_index
        self._running = False
        self._audio_queue = queue.Queue()

    def start(self):
        try:
            from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
        except ImportError:
            print("ERROR: deepgram-sdk not installed. Run: pip3 install deepgram-sdk")
            return

        api_key = os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            print("ERROR: DEEPGRAM_API_KEY not set in environment")
            return

        self._running = True
        dg = DeepgramClient(api_key)
        connection = dg.listen.live.v("1")

        def on_message(self_dg, result, **kwargs):
            transcript = result.channel.alternatives[0].transcript
            if transcript.strip():
                self.transcript_callback(transcript)

        def on_error(self_dg, error, **kwargs):
            print(f"Deepgram error: {error}")

        connection.on(LiveTranscriptionEvents.Transcript, on_message)
        connection.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model="nova-2",
            language="en",
            smart_format=True,
            interim_results=True,
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
        )
        connection.start(options)

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"Audio status: {status}", file=sys.stderr)
            if self._running:
                audio_bytes = (indata * 32767).astype(np.int16).tobytes()
                connection.send(audio_bytes)

        kwargs = {"samplerate": SAMPLE_RATE, "channels": CHANNELS, "dtype": "float32", "blocksize": int(SAMPLE_RATE * BLOCK_DURATION), "callback": audio_callback}
        if self.device_index is not None:
            kwargs["device"] = self.device_index

        self._stream = sd.InputStream(**kwargs)
        self._stream.start()
        print(f"Deepgram transcription started (device: {self.device_index or 'default'})")

    def stop(self):
        self._running = False
        if hasattr(self, "_stream"):
            self._stream.stop()
            self._stream.close()


class WhisperTranscriber:
    """Buffer audio and transcribe with local Whisper in chunks."""

    def __init__(self, transcript_callback, device_index=None, model_name="base"):
        self.transcript_callback = transcript_callback
        self.device_index = device_index
        self.model_name = model_name
        self._running = False
        self._buffer = []
        self._lock = threading.Lock()

    def start(self):
        try:
            import whisper
        except ImportError:
            print("ERROR: openai-whisper not installed. Run: pip3 install openai-whisper")
            return

        self._running = True
        self._model = __import__("whisper").load_model(self.model_name)

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"Audio status: {status}", file=sys.stderr)
            if self._running:
                with self._lock:
                    self._buffer.append(indata.copy())

        kwargs = {"samplerate": SAMPLE_RATE, "channels": CHANNELS, "dtype": "float32", "blocksize": int(SAMPLE_RATE * BLOCK_DURATION), "callback": audio_callback}
        if self.device_index is not None:
            kwargs["device"] = self.device_index

        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

        # Transcription thread
        self._thread = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._thread.start()
        print(f"Whisper transcription started (model: {self.model_name}, device: {self.device_index or 'default'})")

    def _transcribe_loop(self):
        import whisper
        blocks_needed = int(CHUNK_SECONDS / BLOCK_DURATION)
        while self._running:
            with self._lock:
                if len(self._buffer) < blocks_needed:
                    continue
                audio_data = np.concatenate(self._buffer[:blocks_needed], axis=0).flatten()
                self._buffer = self._buffer[blocks_needed:]

            result = self._model.transcribe(audio_data, fp16=False, language="en")
            text = result.get("text", "").strip()
            if text:
                self.transcript_callback(text)

    def stop(self):
        self._running = False
        if hasattr(self, "_stream"):
            self._stream.stop()
            self._stream.close()


if __name__ == "__main__":
    print("Available audio input devices:")
    list_audio_devices()
    bh = find_blackhole_device()
    if bh is not None:
        print(f"\nBlackHole found at device index: {bh}")
    else:
        print("\nBlackHole NOT found. Install it: brew install --cask blackhole-2ch")
