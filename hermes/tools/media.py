"""Media tools: vision_analyze (#18), image_generate (#19), text_to_speech (#20),
plus speech-to-text used by voice mode (#98) and gateway voice notes."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from hermes import config
from hermes.providers import http
from hermes.tools.registry import ToolContext, ToolError, tool


def _cache(kind: str, ext: str) -> Path:
    d = config.path("cache", kind)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.{ext}"


def to_image_url(ref: str, ctx: ToolContext | None = None) -> str:
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    path = ctx.resolve_path(ref) if ctx else Path(ref).expanduser()
    if not path.exists():
        raise ToolError(f"image not found: {ref}")
    from hermes.providers.anthropic_provider import image_to_data_url

    return image_to_data_url(str(path))


def ask_vision(ctx: ToolContext, image_url: str, question: str) -> str:
    agent = ctx.agent
    if agent is None or getattr(agent, "provider", None) is None:
        raise ToolError("vision needs a running agent/provider")
    model = (ctx.config.get("vision", {}) or {}).get("model") or None
    messages = [{"role": "user", "content": [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}]
    resp = agent.provider.chat(messages, tools=None, model=model, max_tokens=4000)
    agent.record_usage(resp)
    return resp.content


@tool(
    "vision_analyze",
    "vision",
    "Analyze an image (URL or local path) with a vision-capable model and answer a question about it.",
    {"type": "object", "properties": {"image": {"type": "string", "description": "URL or file path"},
                                      "question": {"type": "string", "default": "Describe this image in detail."}},
     "required": ["image"]},
)
def vision_analyze(args: dict, ctx: ToolContext):
    url = to_image_url(args["image"], ctx)
    return {"analysis": ask_vision(ctx, url, args.get("question") or "Describe this image in detail.")}


def _image_check():
    ok = any(os.environ.get(k) for k in ("FAL_KEY", "OPENAI_API_KEY"))
    return ok, "" if ok else "set FAL_KEY or OPENAI_API_KEY"


@tool(
    "image_generate",
    "image_gen",
    "Generate an image from a text prompt. Returns a local file path (and URL when available).",
    {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["square", "landscape", "portrait"], "default": "square"},
    }, "required": ["prompt"]},
    check_fn=_image_check,
)
def image_generate(args: dict, ctx: ToolContext):
    cfg = ctx.config.get("image_gen", {}) or {}
    provider = cfg.get("provider", "auto")
    aspect = args.get("aspect_ratio") or "square"
    if provider in ("auto", "fal") and os.environ.get("FAL_KEY"):
        model = cfg.get("model") or "fal-ai/flux/schnell"
        size = {"square": "square_hd", "landscape": "landscape_16_9", "portrait": "portrait_16_9"}[aspect]
        data = http.post_json(f"https://fal.run/{model}", {"prompt": args["prompt"], "image_size": size},
                              {"Authorization": f"Key {os.environ['FAL_KEY']}"}, timeout=180)
        url = (data.get("images") or [{}])[0].get("url")
        if not url:
            raise ToolError(f"no image returned: {str(data)[:300]}")
        _, _, body = http.request("GET", url, timeout=120)
        path = _cache("images", "png")
        path.write_bytes(body)
        return {"path": str(path), "url": url, "media": f"MEDIA:{path}"}
    if os.environ.get("OPENAI_API_KEY"):
        size = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}[aspect]
        data = http.post_json("https://api.openai.com/v1/images/generations",
                              {"model": cfg.get("model") or "gpt-image-1", "prompt": args["prompt"], "size": size},
                              {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=300)
        item = (data.get("data") or [{}])[0]
        path = _cache("images", "png")
        if item.get("b64_json"):
            path.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            path.write_bytes(http.request("GET", item["url"])[2])
        else:
            raise ToolError("no image returned")
        return {"path": str(path), "media": f"MEDIA:{path}"}
    raise ToolError("no image provider configured (FAL_KEY or OPENAI_API_KEY)")


def synthesize(text: str, cfg: dict) -> Path:
    """Text -> audio file using edge-tts, OpenAI or ElevenLabs."""
    tcfg = cfg.get("tts", {}) or {}
    provider = tcfg.get("provider", "auto")
    if provider in ("auto", "elevenlabs") and os.environ.get("ELEVENLABS_API_KEY"):
        voice = tcfg.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
        _, _, body = http.request("POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                                  headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Accept": "audio/mpeg"},
                                  json_body={"text": text, "model_id": "eleven_multilingual_v2"}, timeout=120)
        path = _cache("audio", "mp3")
        path.write_bytes(body)
        return path
    if provider in ("auto", "openai") and os.environ.get("OPENAI_API_KEY"):
        _, _, body = http.request("POST", "https://api.openai.com/v1/audio/speech",
                                  headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                                  json_body={"model": "gpt-4o-mini-tts", "voice": tcfg.get("voice", "alloy"), "input": text[:4000]},
                                  timeout=120)
        path = _cache("audio", "mp3")
        path.write_bytes(body)
        return path
    if provider in ("auto", "edge") and shutil.which("edge-tts"):
        path = _cache("audio", "mp3")
        subprocess.run(["edge-tts", "--voice", tcfg.get("edge_voice", "en-US-AriaNeural"), "--text", text[:5000],
                        "--write-media", str(path)], check=True, capture_output=True, timeout=120)
        return path
    raise ToolError("no TTS provider: install edge-tts (pip install edge-tts) or set OPENAI_API_KEY / ELEVENLABS_API_KEY")


def _tts_check():
    ok = bool(shutil.which("edge-tts") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ELEVENLABS_API_KEY"))
    return ok, "" if ok else "install edge-tts or set OPENAI_API_KEY/ELEVENLABS_API_KEY"


@tool(
    "text_to_speech",
    "tts",
    "Convert text to speech. Returns an audio file path; messaging platforms deliver it as a voice message.",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    check_fn=_tts_check,
)
def text_to_speech(args: dict, ctx: ToolContext):
    path = synthesize(args["text"], ctx.config)
    return {"path": str(path), "media": f"MEDIA:{path}"}


def transcribe(audio_path: str, cfg: dict) -> str:
    """Speech -> text via local faster-whisper, OpenAI or Groq Whisper."""
    scfg = cfg.get("stt", {}) or {}
    provider = scfg.get("provider", "auto")
    path = Path(audio_path)
    if provider in ("auto", "local"):
        try:
            from faster_whisper import WhisperModel  # type: ignore

            model = WhisperModel(scfg.get("local_model", "base"), compute_type="int8")
            segments, _ = model.transcribe(str(path))
            return " ".join(s.text.strip() for s in segments)
        except ImportError:
            if provider == "local":
                raise ToolError("pip install faster-whisper for local STT")
    for name, url, env, model in (
        ("groq", "https://api.groq.com/openai/v1/audio/transcriptions", "GROQ_API_KEY", "whisper-large-v3-turbo"),
        ("openai", "https://api.openai.com/v1/audio/transcriptions", "OPENAI_API_KEY", scfg.get("model", "whisper-1")),
    ):
        if provider in ("auto", name) and os.environ.get(env):
            body, ctype = http.multipart({"model": model}, {"file": (path.name, path.read_bytes(), http.guess_type(str(path)))})
            _, _, resp = http.request("POST", url, headers={"Authorization": f"Bearer {os.environ[env]}", "Content-Type": ctype},
                                      data=body, timeout=300)
            import json

            return json.loads(resp.decode()).get("text", "")
    raise ToolError("no STT provider: pip install faster-whisper, or set GROQ_API_KEY / OPENAI_API_KEY")
