"""All Gemini calls for SingBuildGroupChatBot.

Uses the current `google-genai` SDK. Do not switch back to
`google-generativeai` (imported as `google.generativeai`) -- that package is
deprecated and hit end-of-life on 2025-11-30.

Model names churn. `gemini-2.0-flash`, which this bot originally used, was shut
down on 2026-06-01. The model is therefore read from the GEMINI_MODEL env var so
the next retirement is a Render dashboard edit rather than a code change. The
default below was GA and audio-capable as of 2026-07.
"""

import os

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def _generate(contents):
    resp = client().models.generate_content(model=MODEL, contents=contents)
    return (resp.text or "").strip()


def ask(question):
    """/ask -- plain Q&A, no topic restriction beyond Gemini's own filters."""
    return _generate(
        "You are a helpful assistant in a construction company's project group "
        "chat. Answer clearly and concisely, in plain language a site team can "
        "act on.\n\nQuestion: " + question
    )


def summarize(conversation, group_name, period_label):
    """/summary -- recap a rendered conversation transcript."""
    return _generate(
        f"Below is the chat log from the construction project group "
        f"'{group_name}' for {period_label}. Summarise it for someone who was "
        f"away.\n\n"
        "Cover: decisions made, problems or blockers raised, anything requiring "
        "follow-up, and who is waiting on what. Use short bullet points. Note "
        "open questions explicitly. If the log is thin, say so rather than "
        "padding.\n\n"
        f"--- CHAT LOG ---\n{conversation}\n--- END ---"
    )


def transcribe_and_translate(audio_bytes):
    """Voice note -> English text, with the detected language reported.

    Voice notes are capped at 3 minutes upstream, so they sit far below the 20MB
    inline-request ceiling. Sending raw bytes avoids a Files API upload plus the
    cleanup round trip that would follow it.

    Returns Gemini's raw two-line response; the caller parses the TEXT: line.
    """
    prompt = (
        "Transcribe this voice message. Detect the spoken language. If it is not "
        "English, translate the transcription into English. Preserve names, "
        "numbers, measurements and dates exactly.\n\n"
        "Respond in exactly this format, nothing else:\n"
        "LANGUAGE: <detected language>\n"
        "TEXT: <English transcription>"
    )
    return _generate(
        [prompt, types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")]
    )
