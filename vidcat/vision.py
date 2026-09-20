"""Describe a video with a local vision model served by Ollama."""
import base64
import re

import httpx

from . import config, media

PROMPT = (
    "These are frames sampled from a single home video. Write a short title for it: 3 to 6 words, "
    "Title Case, describing the main subject or activity (for example: Kids Building Sandcastles). "
    "Do not include dates, quotes, or file extensions. Reply with only the title."
)


class VisionUnavailable(RuntimeError):
    pass


def check_ollama(model: str, host: str | None = None) -> None:
    host = host or config.ollama_host()
    try:
        r = httpx.get(f"{host}/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise VisionUnavailable(f"Can't reach Ollama at {host}. Start it with: brew services start ollama") from e
    names = {m["name"] for m in r.json().get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        raise VisionUnavailable(f"Model '{model}' isn't installed in Ollama. Run: ollama pull {model}")


def clean_title(text: str) -> str:
    line = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    line = re.sub(r"^(title\s*:\s*)", "", line.strip(), flags=re.I)
    line = line.strip(" \"'`*#.")
    return " ".join(line.split()[:8])


def caption_video(path, duration: float | None, model: str | None = None, host: str | None = None) -> str:
    """Sample frames from the video and ask the vision model for a short title."""
    model = model or config.vision_model()
    host = host or config.ollama_host()
    frames = media.extract_frames(path, duration)
    if not frames:
        raise VisionUnavailable("Couldn't extract any frames from this video")
    try:
        r = httpx.post(
            f"{host}/api/generate",
            json={
                "model": model,
                "prompt": PROMPT,
                "images": [base64.b64encode(f).decode() for f in frames],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 40},
            },
            timeout=300,  # first call may have to load the model into memory
        )
        r.raise_for_status()
    except httpx.ConnectError as e:
        raise VisionUnavailable(f"Can't reach Ollama at {host}") from e
    except httpx.HTTPError as e:
        raise VisionUnavailable(f"Ollama request failed: {e}") from e
    title = clean_title(r.json().get("response", ""))
    if not title:
        raise VisionUnavailable("The model returned an empty title")
    return title
