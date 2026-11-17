"""Voice mode (#98): talk to the agent from the terminal.

Records with ``arecord`` (Linux), ``sox``/``rec`` or ``ffmpeg``; transcribes with
local faster-whisper, Groq or OpenAI Whisper; speaks replies with edge-tts /
OpenAI / ElevenLabs and plays them with ``afplay``/``mpv``/``ffplay``/``aplay``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def record(seconds: int = 8) -> Path:
    path = Path(tempfile.mkstemp(suffix=".wav", prefix="hermes-voice-")[1])
    if shutil.which("arecord"):
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(seconds), str(path)]
    elif shutil.which("rec"):
        cmd = ["rec", "-q", "-r", "16000", "-c", "1", str(path), "trim", "0", str(seconds)]
    elif shutil.which("ffmpeg"):
        src = ["-f", "avfoundation", "-i", ":0"] if shutil.which("afplay") else ["-f", "pulse", "-i", "default"]
        cmd = ["ffmpeg", "-loglevel", "quiet", "-y", *src, "-t", str(seconds), "-ar", "16000", "-ac", "1", str(path)]
    else:
        raise RuntimeError("no recorder found: install alsa-utils (arecord), sox or ffmpeg")
    subprocess.run(cmd, check=True)
    return path


def play(path: str) -> None:
    for player in (["afplay"], ["mpv", "--really-quiet"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["aplay", "-q"]):
        if shutil.which(player[0]):
            subprocess.run([*player, path], check=False)
            return


def voice_loop(agent, cfg: dict, seconds: int = 8, speak: bool = True) -> None:
    from hermes.tools.media import synthesize, transcribe

    print(f"🎙  Voice mode: press Enter to talk ({seconds}s), type 'q' to quit.")
    while True:
        try:
            if input().strip().lower() in ("q", "quit", "exit"):
                return
        except (EOFError, KeyboardInterrupt):
            return
        print("…listening")
        audio = record(seconds)
        text = transcribe(str(audio), cfg).strip()
        audio.unlink(missing_ok=True)
        if not text:
            print("(didn't catch that)")
            continue
        print(f"you › {text}")
        res = agent.run_conversation(text)
        reply = res.get("final_response") or ""
        print(f"hermes › {reply}")
        if speak and reply:
            try:
                play(str(synthesize(reply[:3000], cfg)))
            except Exception as exc:
                print(f"(tts unavailable: {exc})")
