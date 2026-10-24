#!/usr/bin/env python3
"""
NENE Edge TTS relay
===================
A lightweight local HTTP bridge between the NENE ESP32 and Microsoft Edge's
online neural TTS service, via the community ``edge-tts`` library.

IMPORTANT
---------
* ``edge-tts`` is an UNOFFICIAL client of Microsoft's online TTS service.
  It is NOT an officially supported, guaranteed-free API. Microsoft can change
  or disconnect it at any time.
* The relay itself runs on your LAN, but it requires INTERNET access to
  Microsoft's Edge TTS service for every request.
* No API key or paid subscription is required for the relay.

Endpoints
---------
GET  /health                 -> {"status":"ok","voice":..., "sample_rate":..., ...}
POST /tts  (JSON body)       -> raw PCM audio (see response format)
GET  /tts?text=...&voice=... -> raw PCM audio (handy for curl / browser tests)
POST /stt  (multipart/form-data) -> {"text": "..."} from Groq Whisper

POST /stt is a transparent proxy to Groq's Whisper endpoint. The ESP32 sends
the same multipart body it would have sent to Groq directly; the relay just
re-signs it with GROQ_API_KEY and forwards it. This keeps the API key on the
laptop AND lets the ESP32 use plain HTTP for STT (no TLS handshake on-device),
which is what frees up heap for a longer recording buffer.

POST body: {"text": "...", "voice": "fil-PH-AngeloNeural"}
  - text is required; voice is optional (defaults to EDGE_TTS_VOICE).

Response format (success)
-------------------------
Content-Type:         audio/pcm
X-TTS-Voice:          fil-PH-AngeloNeural
X-TTS-Sample-Rate:    24000
X-TTS-Channels:       1
X-TTS-Bits-Per-Sample: 16
X-TTS-Duration-S:      <seconds>
Content-Length:       <bytes>
Body: little-endian signed 16-bit PCM, mono, 24000 Hz.

Errors use JSON bodies: {"error": "<message>"}
  400  bad/empty text or invalid voice
  413  text too long
  502  cannot reach Microsoft's Edge TTS service (check internet)
  500  synthesis/decoding failure

Usage
-----
  python edge_relay.py                        # HOST=0.0.0.0 PORT=8123
  python edge_relay.py --port 9000 --voice fil-PH-BlessicaNeural
  python edge_relay.py --selftest "Kumusta pre" [--out test.pcm]

Environment variables (defaults):
  EDGE_TTS_HOST   0.0.0.0
  EDGE_TTS_PORT   8123
  EDGE_TTS_VOICE  fil-PH-AngeloNeural
  EDGE_TTS_RATE   +0%
  EDGE_TTS_PITCH  +0Hz
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import logging
import os
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import edge_tts
import miniaudio
import socket
from zeroconf import ServiceInfo, Zeroconf

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

# 16-bit signed little-endian PCM @ 24000 Hz mono is the exact format the
# NENE firmware plays on its MAX98357A output path (I2S_NUM_1).
TTS_SAMPLE_RATE = 24000
TTS_CHANNELS = 1
TTS_BITS_PER_SAMPLE = 16
MAX_TEXT_CHARS = 5000

DEFAULT_HOST = os.environ.get("EDGE_TTS_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("EDGE_TTS_PORT", "8123"))
DEFAULT_VOICE = os.environ.get("EDGE_TTS_VOICE", "fil-PH-BlessicaNeural")
DEFAULT_RATE = os.environ.get("EDGE_TTS_RATE", "+0%")
DEFAULT_PITCH = os.environ.get("EDGE_TTS_PITCH", "+0Hz")

# ---------------------------------------------------------------------------
# TTS PRONUNCIATION MAP
# ---------------------------------------------------------------------------
# The voice is Filipino (fil-PH-BlessicaNeural). It speaks conversational
# Tagalog/Taglish cleanly, but mispronounces deep or literary Tagalog with
# English-ish vowels -- the "ugly accent" complaint. Deep words also push the
# LLM into formal register, which reads worse aloud.
#
# This is a purely TEXTUAL pre-pass on the way to TTS. It cannot affect STT,
# VAD, the wake word, or any audio path, so no working feature is touched.
#
# Extend freely: any time a reply sounds wrong, find the word in the
# "[GROQ] Response:" serial line, add it here, restart the relay. No reflash.
TTS_PRONUNCIATION_MAP = {
    # literary / formal -> everyday. Only entries that ACTUALLY change
    # something: identity entries (key == value) did nothing but add
    # whole-word matching risk, so they were removed.
    "marahil": "siguro",
    "subalit": "pero",
    "ngunit": "pero",
    "kung gayon": "so",
    "gayunpaman": "pero",
    "samakatuwid": "so",
    "napakahalaga": "sobrang importante",
    "pinagsasabihan": "sinasabihan",
    "nagsasabing": "sabi",
    "nais": "gusto",
    "akin": "sa akin",
    "iyan": "yan",
    "iyon": "yun",
    "lamang": "lang",
}

# ---------------------------------------------------------------------------
# TTS LOANWORD MAP
# ---------------------------------------------------------------------------
# Second class of mispronunciation, separate from the Tagalog register map
# above. These words exist in Taglish speech but the Filipino voice reads them
# with the wrong vowel or stress -- the reported case was "Wi-Fi" coming out
# as "wee fay".
#
# Unlike punctuation (a closed set, handled by normalise_for_tts), this class
# is LEXICAL: the engine's lexicon decides per word, so no regex can derive
# it. It can only be fixed with an explicit table -- which is why this table
# exists and why it is meant to grow.
#
# Method: spell the word the way the voice pronounces it CORRECTLY. Respell
# with plain letters, not IPA, e.g. Wi-Fi -> "wai fai". Test any entry in
# seconds with:  curl "http://192.168.1.3:8123/tts-preview?text=Wi-Fi"
# then listen to `tts_preview.wav`. No reflash, no device needed.
#
# Keep the LEFT side the exact word the LLM writes (matched whole-word,
# case-insensitive); the RIGHT side is what the voice should be handed.
#
# DISCIPLINE -- this table is a scalpel, not a shotgun:
#   * only add a word you have HEARD come out wrong (use /tts-preview)
#   * never add a word that is also a Tagalog word -- e.g. "dating" means
#     "arrival/wear" in Tagalog, so respelling it corrupts normal sentences
#   * don't add words the voice already says correctly (sorry, okay, wow,
#     haha) -- respelling them can only make them worse
#   * no identity entries (key == value); they do nothing but add risk
# An over-eager table is worse than a small one: it garbles good speech.
TTS_LOANWORD_MAP = {
    # Verified wrong on this device. "Wi-Fi" was spoken as "wee fay".
    "wi-fi": "wai fai",
    "wifi": "wai fai",
    # Strong loanwords with no Tagalog meaning -- safe to respell. Add here
    # only after hearing the problem; each is confirmed by ear via preview.
    "email": "i-meyl",
    "online": "on-lain",
    "offline": "off-lain",
    "deadline": "ded-lain",
    "update": "ap-deyt",
    "download": "dawn-lowd",
    "upload": "ap-lowd",
    "browser": "braw-ser",
    "password": "pas-word",
    "username": "yu-ser-neym",
    # Owner's name. The bare initials "AJ" are read as letters ("ey-jay" or
    # worse), and "Domingo" gets English stress. Respelled to the Filipino
    # pronunciation. The canned creator replies in assistant.cpp already write
    # the phonetic form inline; this entry is the backstop for when the LLM
    # writes the name itself in an ordinary reply.
    "aj domingo": "ey-jey do-ming-go",
}

_PR_PRON_RE = None
_PR_LOAN_RE = None

# ---------------------------------------------------------------------------
# TTS TEXT NORMALISATION
# ---------------------------------------------------------------------------
# The LLM emits typographic punctuation and emoji that Edge TTS mishandles.
# Reported case: the reply contained "Wi‑Fi" with a NON-BREAKING HYPHEN
# (U+2011) and the voice said "wee fay" instead of "why-fye" -- it did not
# recognise U+2011 as a word break, so it read the whole token phonetically.
# Curly quotes and emoji are similarly read aloud or swallowed.
#
# Normalise the text to plain ASCII punctuation before synthesis. This runs
# BEFORE the pronunciation map so both see clean input, and it is purely
# textual -- no audio path is involved.
# Character-level normalisation for TTS. This covers CLASS 1 only: the
# mechanical problems where the character itself is unreadable or mis-split.
# That set is CLOSED -- Unicode has a finite set of punctuation and symbol
# ranges -- so this normaliser is complete by construction. Anything left
# non-ASCII falls through to the final rule below (keep Latin letters, drop
# the rest), so no exotic codepoint can ever reach the voice.
#
# Rules are ordered: exact-character substitutions first, then range rules.
TTS_CHAR_MAP = {
    # --- dashes / hyphens -> ASCII hyphen (so "Wi-Fi" splits correctly) ---
    "\u2010": "-",   # hyphen
    "\u2011": "-",   # non-breaking hyphen  (the reported "wee fay" cause)
    "\u2012": "-",   # figure dash
    "\u2013": "-",   # en dash
    "\u2014": ", ",  # em dash -> spoken pause
    "\u2015": ", ",  # horizontal bar
    "\u2212": "-",   # minus sign
    "\uFE58": "-",   # small em dash
    "\uFE63": "-",   # small hyphen-minus
    "\uFF0D": "-",   # fullwidth hyphen-minus
    # --- quotes -> ASCII ---
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2039": "'", "\u203a": "'",
    "\u2032": "'", "\u2033": '"',   # prime / double prime
    "\u0060": "'",                  # backtick (often quoted text)
    # --- ellipsis -> spoken pause ---
    "\u2026": "...",
    # --- bullets / separators the voice should not read ---
    "\u2022": ",", "\u2023": ",", "\u2043": ",", "\u25cf": ",", "\u25aa": ",",
    "\u00b7": ",", "\u2027": ",",
    # --- spaces that are not ASCII spaces ---
    "\u00a0": " ", "\u2000": " ", "\u2001": " ", "\u2002": " ",
    "\u2003": " ", "\u2004": " ", "\u2005": " ", "\u2006": " ",
    "\u2007": " ", "\u2008": " ", "\u2009": " ", "\u200a": " ",
    "\u202f": " ", "\u205f": " ", "\u3000": " ",
    # --- invisible characters: delete entirely ---
    "\u200b": "", "\u200c": "", "\u200d": "", "\u2060": "",
    "\ufeff": "", "\u00ad": "",   # zero-width space/joiners, BOM, soft hyphen
}

# Codepoint RANGES that are always dropped or spaced. Emoji, dingbats, arrows,
# box drawing, currency and other symbol blocks: the voice either narrates
# them ("smiling face") or stumbles. Ranges are (lo, hi) inclusive.
TTS_DROP_RANGES = (
    (0x2190, 0x21FF),   # arrows
    (0x2300, 0x23FF),   # technical (incl. hourglass, watch)
    (0x2460, 0x24FF),   # enclosed alphanumerics
    (0x25A0, 0x25FF),   # geometric shapes
    (0x2600, 0x27BF),   # misc symbols + dingbats (incl. sun, hearts, stars)
    (0x2B00, 0x2BFF),   # misc symbols and arrows
    (0xFE00, 0xFE0F),   # variation selectors (emoji presentation)
    (0x1F000, 0x1FAFF),  # emoji, pictographs, transport, supplemental
    (0x1F900, 0x1F9FF),  # supplemental symbols & pictographs
)


def _is_droppable_symbol(ch: str) -> bool:
    o = ord(ch)
    for lo, hi in TTS_DROP_RANGES:
        if lo <= o <= hi:
            return True
    return False


def normalise_for_tts(text: str) -> str:
    """Make `text` speakable: plain punctuation, no emoji, no odd spaces.

    Class 1 normalisation. The guarantee is:

        no SYMBOL or EMOJI survives -- nothing the voice would narrate,
        stumble on, or mis-split as a word. Letters (any script) pass
        through, because a stray accented letter is a word to pronounce,
        not a symbol to swallow.

    In practice the LLM emits Taglish + ASCII punctuation, so the output is
    ASCII; the rules below simply make sure an exotic codepoint can never
    reach the voice as a symbol.
    """
    if not text:
        return text
    out = []
    for ch in text:
        if ch in TTS_CHAR_MAP:
            out.append(TTS_CHAR_MAP[ch])
            continue
        o = ord(ch)
        if o < 128:
            # Plain ASCII -- keep, but drop control characters.
            out.append(ch if o >= 32 else " ")
            continue
        # Typographic whitespace and Latin-1 punctuation/symbols.
        if o == 0x85:            # NEL
            out.append(" ")
            continue
        if 0xA0 <= o <= 0xBF:
            # Latin-1 punctuation & symbols (¡ ¢ £ ¤ ¥ ¦ § ¨ © ª « ¬ SHY ® ¯
            # ° ± ² ³ ´ µ ¶ · ¸ ¹ º » ¼ ½ ¾ ¿). None are letters; the voice
            # either mis-reads or narrates them. Space them out.
            out.append(" ")
            continue
        if 0xC0 <= o <= 0xFF and ch.isalpha():
            # Latin-1 letters (accented vowels etc.) -- the voice handles
            # these fine; keep them.
            out.append(ch)
            continue
        # Emoji / symbols by range -> drop.
        if _is_droppable_symbol(ch):
            out.append(" ")
            continue
        # Letters in any other script (ñ is Latin-1; ē is Extended-A; CJK,
        # Cyrillic, Greek ...) -- keep: a foreign word beats nonsense.
        if ch.isalpha():
            out.append(ch)
            continue
        # Digits and marks in other scripts -> keep.
        if ch.isalnum():
            out.append(ch)
            continue
        # Any remaining non-ASCII punctuation/mark -> space, so it becomes a
        # word break instead of gluing two words together.
        out.append(" ")

    # Collapse runs of spaces the drops above can create, then tidy spacing
    # around punctuation so the voice does not pause oddly.
    #
    # NOTE: quotes are deliberately NOT in this list. An earlier version had
    # (" '", "'") which glued an opening quote to the previous word
    # ("she said 'i love you'" -> "she said'i love you'"). The voice reads
    # that as one token. Space around a quote is harmless; removing it is not.
    normalised = " ".join("".join(out).split())
    for bad, good in ((" ,", ","), (" .", "."), (" ?", "?"), (" !", "!"),
                      (" ;", ";"), (" :", ":"), ("( ", "("), (" )", ")")):
        normalised = normalised.replace(bad, good)
    return normalised.strip()


def _apply_word_map(text: str, mapping: dict, cache_name: str) -> str:
    """Whole-word, case-insensitive substitution from a word table.

    Longest keys first so multi-word phrases ("kung gayon", "I love you")
    win over their single-word prefixes. Idempotent for unchanged text.
    """
    global _PR_PRON_RE, _PR_LOAN_RE
    if not text or not mapping:
        return text
    if cache_name == "pron":
        if _PR_PRON_RE is None:
            keys = sorted(mapping, key=len, reverse=True)
            pattern = r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)"
            _PR_PRON_RE = (re.compile(pattern, re.IGNORECASE), mapping)
        regex, table = _PR_PRON_RE
    else:
        if _PR_LOAN_RE is None:
            keys = sorted(mapping, key=len, reverse=True)
            pattern = r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)"
            _PR_LOAN_RE = (re.compile(pattern, re.IGNORECASE), mapping)
        regex, table = _PR_LOAN_RE

    def _swap(match):
        word = match.group(0)
        repl = table.get(word.lower())
        if not repl or repl == word.lower():
            return word
        # "sa akin" must not become "sa sa akin": the entry maps the bare form
        # to a phrase that already carries the preposition, and word-level
        # substitution cannot see what came before it. When the preceding text
        # already ends in "sa ", drop the additive prefix from the replacement.
        if repl.lower().startswith("sa "):
            before = match.string[:match.start()].rstrip().lower()
            if before.endswith("sa"):
                repl = repl[3:]
        # Preserve a leading capital so sentence case survives.
        if word[:1].isupper():
            return repl[:1].upper() + repl[1:]
        return repl

    return regex.sub(_swap, text)


def apply_pronunciation_map(text: str) -> str:
    """Rewrite deep Tagalog to the colloquial form the voice says correctly."""
    return _apply_word_map(text, TTS_PRONUNCIATION_MAP, "pron")


def apply_loanword_map(text: str) -> str:
    """Respell English/loanwords the Filipino voice reads wrong (Wi-Fi etc.)."""
    return _apply_word_map(text, TTS_LOANWORD_MAP, "loan")


def strip_control_tokens(text: str) -> str:
    """Remove LLM control tokens before anything else touches the text.

    gpt-oss and other reasoning models emit chat-template control tokens
    (`<|constrain|>`, `<|channel|>`, `<|start|>`) and end-of-turn markers
    (`#`, `#NENE`). Without stop sequences they leak into the visible reply
    as literal text, and the voice then SPEAKS them. Observed live:

        "...ending na 'ghost'.#NENE <|constrain|>re> <|constrain|>re> ..."

    Firmware now sends stop=["#", "<|"] so this should not happen, but the
    relay is the last line of defence and must never synthesise control
    tokens regardless of which model is configured or who edits the prompt.

    Two passes:
      1. drop any balanced <|...|> token, then any stray <| or |> fragment
      2. drop an end-of-turn marker (#word) and the `re>` fragments a
         truncated `<|constrain|>re>` leaves behind

    Returns the cleaned text; caller compares to detect that it fired.
    """
    if not text:
        return text
    out = text
    # 1. balanced special tokens, then any dangling opener/closer
    out = re.sub(r"<\|[^|>]*\|>", " ", out)
    out = re.sub(r"<\|[^|>]*", " ", out)
    out = re.sub(r"[^|>]*\|>", " ", out)
    # 2. the end-of-turn marker gpt-oss emits: '#' immediately followed by the
    #    persona name in caps ('#NENE'), optionally repeated. Deliberately
    #    narrow -- a bare '#word' could be a legitimate hashtag the LLM chose
    #    to say, and silently eating real words is worse than leaving one.
    out = re.sub(r"#\s*NENE\b", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"#(?=[A-Z]{2,}\b)", " ", out)
    # 3. leftover channel fragments from a truncated token, e.g. 're>'
    out = re.sub(r"\bre\s*>", " ", out)
    out = re.sub(r"\b(?:constrain|channel|analysis|final)\b", " ", out,
                 flags=re.IGNORECASE)
    # collapse the gaps the removals left
    return " ".join(out.split())


def prepare_for_tts(text: str) -> str:
    """The full textual pre-pass, in order:

        0. strip_control_tokens    - remove LLM control-token leakage
        1. normalise_for_tts       - mechanical: punctuation, emoji, spaces
        2. apply_pronunciation_map - register: deep Tagalog -> colloquial
        3. apply_loanword_map      - lexical: Wi-Fi -> "wai fai"

    Purely textual, so STT, VAD, the wake word and every audio path are
    untouched. Idempotent: running it twice changes nothing.
    """
    if not text:
        return text
    return apply_loanword_map(
        apply_pronunciation_map(normalise_for_tts(strip_control_tokens(text)))
    )


# Groq OpenAI-compatible Whisper transcription endpoint (STT proxy target).
GROQ_STT_URL = os.environ.get(
    "GROQ_STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions"
)
# Must match GROQ_STT_MODEL / GROQ_STT_LANGUAGE in the firmware's config.h.
# The streaming route builds its own multipart body (the device sends raw PCM,
# not a prepared WAV), so it needs these locally.
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_STT_LANGUAGE = os.environ.get("GROQ_STT_LANGUAGE", "tl")
MAX_STT_BYTES = 4_000_000  # ~2 min of 16 kHz 16-bit mono, generous

# --- Streaming STT gate ----------------------------------------------------
# The device streams raw PCM as it records, so the laptop owns the
# "is this actually speech?" decision. That makes it tunable here, from the
# relay console, with no firmware reflash.
#
# Gate on PEAK, not average RMS. Measured on NENE's INMP441:
#     idle   avg  124  peak  448
#     speech avg  149  peak 1939
# Average RMS barely separates the two (1.2x) while peak separates them 4.3x.
# An average-RMS gate is why VAD_MIN_RECORD_ENERGY=1500 on the device
# discarded every real utterance.
STT_MIN_PEAK = int(os.environ.get("STT_MIN_PEAK", "900"))
STT_MIN_AVG_RMS = int(os.environ.get("STT_MIN_AVG_RMS", "100"))

# Primary gate, measured on NENE's INMP441:
#     quiet room floor  avg = 220 - 800   (fan off, this room)
#     user speech       avg = 1330 - 2420 (5-10 inches, quiet room)
#     loud-room speech  avg = 3195 ; fan/room avg = 697 - 1013 (fan on)
# Envelope variation (below) separates fan from speech; the average separates
# silence from speech. Peak is NOT usable (it saturates at 32768 for both).
#
# SNR_MULT is applied to the quietest recent floor seen by the relay so the
# gate follows the room instead of assuming one.
STT_AVG_SNR_MULT = float(os.environ.get("STT_AVG_SNR_MULT", "1.6"))
# Hard backstop for a very quiet room, AND the ceiling the tracked floor may
# reach, so the bar can never climb into speech range.
STT_AVG_ABS_MIN = int(os.environ.get("STT_AVG_ABS_MIN", "900"))

# Envelope variance: speech is modulated at syllable rate (bursts), while fans,
# aircon and crowd rumble are steady. Normalised stddev of per-20ms RMS.
# Steady noise scores low, speech scores high. Secondary opinion only -- it
# never overrides a clear average.
STT_ENVELOPE_MIN = float(os.environ.get("STT_ENVELOPE_MIN", "0.35"))

# Whisper invents stock phrases (" Thank you.", " So,", " Outro") on clips it
# cannot actually read. Anything this short is one of those, not an utterance.
STT_MIN_CHARS = int(os.environ.get("STT_MIN_CHARS", "3"))
STT_HALLUCINATION_STEMS = {
    "thank you", "thanks for watching", "thank you for watching", "so",
    "you", "outro", "intro", "bye", "okay", "ok", ".", "...", "hmm",
    "salamat", "sige", "oo", "ano", "please subscribe", "subtitles by",
    "amara.org", "transcription by", "music",
}

# Whisper's own confidence gate: when it judges a clip to be non-speech with
# probability above this, it returns empty instead of guessing a phrase.
STT_NO_SPEECH_THRESHOLD = float(os.environ.get("STT_NO_SPEECH_THRESHOLD", "0.75"))

# Quietest recent average -- the relay's running estimate of this room's noise
# floor. Guarded because ThreadingHTTPServer serves each request in its own
# thread.
import threading

_floor_lock = threading.Lock()
_noise_floor_avg = int(os.environ.get("STT_FLOOR_SEED", "600"))


# Trailing silence kept when trimming before upload, in milliseconds. The
# device already endpoints on silence; this only shaves the long dead tail so
# Whisper is not fed 2 s of room tone (a classic hallucination source).
STT_TRIM_SILENCE_MS = int(os.environ.get("STT_TRIM_SILENCE_MS", "300"))
STT_SAMPLE_RATE = 16000  # I2S_MIC_SAMPLE_RATE on the device


# Groq chat-completions endpoint (LLM proxy target). The firmware sends the
# *whole* OpenAI-shaped payload (model, temperature, top_p, system prompt,
# messages) and we forward it verbatim -- the dashboard tunes those knobs, so
# the relay must not rewrite them.
GROQ_LLM_URL = os.environ.get(
    "GROQ_LLM_URL", "https://api.groq.com/openai/v1/chat/completions"
)
MAX_LLM_BYTES = 64_000  # system prompt (~2KB) + short chat history, generous
MAX_LLM_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("edge-relay")


# ---------------------------------------------------------------------------
# Synthesis + decode
# ---------------------------------------------------------------------------

async def synthesize_pcm(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
) -> tuple[bytes, float]:
    """Synthesize `text`, decode the resulting MP3 to raw signed-16 PCM.

    Returns (pcm_bytes, duration_seconds). Raises on any failure; the caller
    maps exceptions to HTTP errors.
    """
    # Text pre-pass, in order:
    #   1. normalise_for_tts       - plain punctuation / strip emoji, so the
    #      voice does not read "Wi‑Fi" (U+2011) as "wee fay"
    #   2. apply_pronunciation_map - deep Tagalog -> the colloquial form
    #   3. apply_loanword_map      - English loanwords -> respelled forms
    # All purely textual, so STT, VAD and every audio path are untouched.
    spoken = prepare_for_tts(text)
    if spoken != text:
        log.info("TTS text pre-pass: %d -> %d chars | %r", len(text), len(spoken),
                 spoken if len(spoken) <= 160 else spoken[:157] + "...")

    mp3 = bytearray()
    communicate = edge_tts.Communicate(spoken, voice, rate=rate, pitch=pitch)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3.extend(chunk["data"])
        # other chunk types: metadata / WordBoundary / SessionEnd — ignored

    if not mp3:
        raise ValueError("TTS service returned no audio")

    decoded = miniaudio.decode(
        bytes(mp3),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=TTS_CHANNELS,
        sample_rate=TTS_SAMPLE_RATE,
    )
    pcm = bytes(decoded.samples.tobytes())

    if decoded.sample_rate != TTS_SAMPLE_RATE or decoded.nchannels != TTS_CHANNELS:
        raise ValueError(
            f"unexpected decoded format: {decoded.sample_rate} Hz x "
            f"{decoded.nchannels} ch (expected {TTS_SAMPLE_RATE} Hz x {TTS_CHANNELS})"
        )
    if len(pcm) % 2 != 0:
        raise ValueError("decoded PCM has odd byte length (sample misalignment)")

    return pcm, decoded.duration


def synthesize(text_arg: str, voice: str = DEFAULT_VOICE,
               rate: str = DEFAULT_RATE) -> tuple[bytes, float]:
    try:
        return asyncio.run(synthesize_pcm(text_arg, voice, rate))
    except RuntimeError as exc:
        # asyncio.run() inside a worker thread is safe (fresh loop per call),
        # but re-raise anything unexpected so the handler reports it.
        raise exc


def json_error(status: int, message: str) -> bytes:
    return json.dumps({"error": message}).encode("utf-8")


def pcm_stats(pcm: bytes) -> tuple[int, int, float]:
    """Return (avg_rms, peak, duration_s) for 16-bit little-endian mono PCM."""
    n = len(pcm) // 2
    if n == 0:
        return 0, 0, 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    total = 0
    peak = 0
    for s in samples:
        if s < 0:
            s = -s
        total += s * s
        if s > peak:
            peak = s
    avg = int((total / n) ** 0.5)
    return avg, peak, n / STT_SAMPLE_RATE


def loudest_window_rms(pcm: bytes, window_ms: int = 1000) -> int:
    """RMS of the single loudest window in the clip.

    The whole-clip average is the wrong metric when a recording is part
    speech and part trailing silence. Observed on real device clips:
        avg=654  peak=13606  env=0.87   -> rejected as "quiet"
        avg=800  peak=19569  env=1.09   -> rejected as "quiet"
    Those clips DO contain clear voice (the peak and envelope say so); it is
    the silence padding around it that drags the average under the bar. The
    loudest 1 s window measures the voice itself, so a short utterance with a
    2 s tail is judged on what was actually said.
    """
    n = len(pcm) // 2
    window = max(1, int(STT_SAMPLE_RATE * window_ms / 1000))
    if n < window:
        # Clip shorter than one window: fall back to the whole thing.
        samples = array.array("h")
        samples.frombytes(pcm[: n * 2])
        total = 0
        for s in samples:
            total += s * s
        return int((total / max(1, n)) ** 0.5) if n else 0

    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])

    # Sliding sum of squares over the window (O(n), not O(n*window)).
    step = max(1, int(STT_SAMPLE_RATE * 20 / 1000))  # advance in 20 ms hops
    best = 0.0
    total_sq = 0.0
    for i in range(window):
        s = samples[i]
        total_sq += s * s
    best = total_sq
    for start in range(step, n - window + 1, step):
        # drop the samples leaving the window, add the ones entering
        for j in range(start - step, start):
            s = samples[j]
            total_sq -= s * s
        for j in range(start - step + window, start + window):
            s = samples[j]
            total_sq += s * s
        if total_sq > best:
            best = total_sq
    return int((best / window) ** 0.5)


def envelope_variation(pcm: bytes, window_ms: int = 20) -> float:
    """Normalised variation of the short-term envelope.

    Speech is bursty -- syllables rise and fall at roughly 2-8 Hz -- so the
    per-window RMS swings widely. A fan, aircon or steady crowd rumble holds a
    nearly constant level. Returns stddev/mean, so ~0 for steady noise and
    well above STT_ENVELOPE_MIN for speech.

    This is the second opinion that lets us reject steady room noise even when
    it is loud enough to clear the average gate.
    """
    n = len(pcm) // 2
    window = max(1, int(STT_SAMPLE_RATE * window_ms / 1000))
    if n < window * 4:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])

    rms_values = []
    for start in range(0, n - window + 1, window):
        total = 0
        for s in samples[start:start + window]:
            total += s * s
        rms_values.append((total / window) ** 0.5)
    if not rms_values:
        return 0.0
    mean = sum(rms_values) / len(rms_values)
    if mean <= 1.0:
        return 0.0  # pure digital silence
    var = sum((v - mean) ** 2 for v in rms_values) / len(rms_values)
    return (var ** 0.5) / mean



def trim_trailing_silence(pcm: bytes, keep_ms: int) -> bytes:
    """Shave dead room tone off the end, keeping ``keep_ms`` of it.

    Scan backwards in ~10 ms windows and stop at the first window that carries
    real energy. Never touches the start of the utterance, so the first word is
    always preserved.
    """
    if len(pcm) < 2:
        return pcm
    window = int(STT_SAMPLE_RATE * 0.010)  # 10 ms
    keep_samples = int(STT_SAMPLE_RATE * keep_ms / 1000)
    samples = array.array("h")
    samples.frombytes(pcm[: (len(pcm) // 2) * 2])

    end = len(samples)
    while end > window:
        start = end - window
        chunk = samples[start:end]
        peak = 0
        for s in chunk:
            if s < 0:
                s = -s
            if s > peak:
                peak = s
        if peak * 4 > STT_MIN_PEAK:
            break  # real audio here -- this is where the tail begins
        end = start

    end = min(len(samples), end + keep_samples)
    return samples[:end].tobytes()


def pcm_to_wav(pcm: bytes, sample_rate: int = STT_SAMPLE_RATE) -> bytes:
    """Wrap raw PCM in a canonical 44-byte WAV header.

    Defaults to the STT rate (Whisper's input). Pass ``TTS_SAMPLE_RATE`` for
    TTS output so a saved .wav plays at the right pitch/speed.
    """
    return wav_header(len(pcm), sample_rate) + pcm


def wav_header(data_bytes: int, sample_rate: int = STT_SAMPLE_RATE) -> bytes:
    byte_rate = sample_rate * 1 * 16 // 8
    block_align = 16 // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_bytes, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, byte_rate, block_align, 16,
        b"data", data_bytes,
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class RelayHandler(BaseHTTPRequestHandler):
    server_version = "NENE-EdgeRelay/1.0"
    protocol_version = "HTTP/1.0"  # connection closes after each response

    # silence default per-request stderr logging; we log our own messages
    def log_message(self, fmt: str, *args):  # noqa: A003
        log.info("%s %s", self.address_string(), fmt % args)

    def _send_pcm(self, pcm: bytes, voice: str, duration: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/pcm")
        self.send_header("X-TTS-Voice", voice)
        self.send_header("X-TTS-Sample-Rate", str(TTS_SAMPLE_RATE))
        self.send_header("X-TTS-Channels", str(TTS_CHANNELS))
        self.send_header("X-TTS-Bits-Per-Sample", str(TTS_BITS_PER_SAMPLE))
        self.send_header("X-TTS-Duration-S", f"{duration:.3f}")
        self.send_header("Content-Length", str(len(pcm)))
        self.end_headers()
        self.wfile.write(pcm)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "voice": DEFAULT_VOICE,
                    "sample_rate": TTS_SAMPLE_RATE,
                    "channels": TTS_CHANNELS,
                    "bits_per_sample": TTS_BITS_PER_SAMPLE,
                },
            )
            return

        if path == "/tts":
            text = (query.get("text") or [""])[0]
            voice = (query.get("voice") or [DEFAULT_VOICE])[0]
            self._handle_tts(text, voice)
            return

        if path == "/tts-preview":
            self._handle_tts_preview(query)
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/stt":
            self._handle_stt()
            return
        if parsed.path == "/stt/stream":
            self._handle_stt_stream()
            return
        if parsed.path == "/llm":
            self._handle_llm()
            return
        if parsed.path != "/tts":
            self._send_json(404, {"error": "not found"})
            return

        raw_len = int(self.headers.get("Content-Length") or 0)
        if raw_len <= 0 or raw_len > 1_000_000:
            self._send_json(400, {"error": "bad or missing Content-Length"})
            return

        try:
            body = json.loads(self.rfile.read(raw_len).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return

        text = str(body.get("text") or "").strip()
        voice = str(body.get("voice") or DEFAULT_VOICE).strip()
        self._handle_tts(text, voice)

    def _groq_stt_post(self, body: bytes, content_type: str,
                       hallucination_guard: bool = False) -> None:
        """Sign ``body`` with the Groq key and relay Groq's JSON back verbatim.

        Shared by the buffered ``/stt`` route and the chunked ``/stt/stream``
        route so both stay byte-for-byte identical on the wire (same model,
        language, prompt, temperature).

        When ``hallucination_guard`` is set, a reply that is too short or that
        matches a known Whisper stock phrase is rewritten to an empty
        transcript, so the device never acts on invented text.
        """
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key:
            self._send_json(
                500, {"error": "GROQ_API_KEY not set on the relay host"}
            )
            return

        req = urllib.request.Request(
            GROQ_STT_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                # Groq sits behind Cloudflare, which rejects urllib's default
                # "Python-urllib/..." user-agent with error 1010. Send a
                # browser-style agent instead.
                "User-Agent": "curl/8.5.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log.warning("Groq STT HTTP %s: %s", exc.code, detail)
            self._send_json(502, {"error": f"groq stt http {exc.code}"})
            return
        except (urllib.error.URLError, OSError) as exc:
            log.error("Cannot reach Groq STT: %s", exc)
            self._send_json(502, {"error": "cannot reach groq stt"})
            return

        log.info("STT ok: %d bytes back", len(payload))

        if hallucination_guard:
            payload = self._filter_hallucination(payload)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _filter_hallucination(self, payload: bytes) -> bytes:
        """Blank a transcript that is obviously invented rather than heard.

        Whisper reliably emits stock phrases on clips it cannot read. Returning
        them makes Nene answer a sentence nobody said, so anything too short or
        matching a known stem is replaced with an empty transcript plus
        "filtered":"hallucination" for the logs.
        """
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return payload
        text = str(data.get("text") or "").strip()
        if not text:
            return payload
        # verbose_json also reports how sure Whisper was; trust it when given.
        no_speech = data.get("no_speech_prob")
        if isinstance(no_speech, (int, float)) and no_speech >= STT_NO_SPEECH_THRESHOLD:
            log.info("STT guard: no_speech_prob=%.2f, blanking %r",
                     no_speech, text[:40])
            return json.dumps({"text": "", "filtered": "no_speech"}).encode("utf-8")
        stripped = text.strip(" \t\n\r.,!?¡¿\"'").lower()
        if len(stripped) < STT_MIN_CHARS or stripped in STT_HALLUCINATION_STEMS:
            log.info("STT guard: plausible hallucination %r, blanking", text[:40])
            return json.dumps({"text": "", "filtered": "hallucination"}).encode("utf-8")
        return payload


    def _handle_stt(self) -> None:
        """Transparent proxy: forward the multipart body to Groq Whisper.

        The device speaks plain HTTP to us; we sign the request with
        GROQ_API_KEY and relay Groq's JSON back verbatim. Keeping the TLS
        handshake on the laptop means the ESP32 needs no large contiguous heap
        block at STT time.
        """
        raw_len = int(self.headers.get("Content-Length") or 0)
        if raw_len <= 0 or raw_len > MAX_STT_BYTES:
            self._send_json(400, {"error": "bad or missing Content-Length"})
            return

        content_type = self.headers.get("Content-Type") or "application/octet-stream"
        body = self.rfile.read(raw_len)
        log.info("STT request: %d bytes, content-type=%s", len(body), content_type)
        self._groq_stt_post(body, content_type)

    def _read_chunked_body(self) -> bytes | None:
        """Decode an HTTP/1.1 ``Transfer-Encoding: chunked`` request body.

        Returns None (after replying 400/413) if the framing is malformed or
        the utterance exceeds MAX_STT_BYTES.
        """
        pcm = bytearray()
        while True:
            size_line = self.rfile.readline(64)
            if not size_line:
                log.warning("stream: connection closed mid-chunk-header")
                return None
            size_line = size_line.strip().split(b";")[0]  # ignore chunk-ext
            try:
                size = int(size_line, 16)
            except ValueError:
                log.warning("stream: bad chunk size %r", size_line)
                self._send_json(400, {"error": "bad chunk size"})
                return None
            if size == 0:
                self.rfile.readline()  # trailing CRLF after last chunk
                break
            chunk = self.rfile.read(size)
            if chunk is None or len(chunk) != size:
                log.warning("stream: short chunk (%s of %s)", len(chunk or b""), size)
                return None
            pcm += chunk
            self.rfile.read(2)  # chunk CRLF
            if len(pcm) > MAX_STT_BYTES:
                log.warning("stream: exceeded MAX_STT_BYTES (%d)", MAX_STT_BYTES)
                self._send_json(413, {"error": "utterance too long"})
                return None
        return bytes(pcm)

    def _handle_stt_stream(self) -> None:
        """Streaming STT: the device sends PCM as it records; we gate + upload.

        This is the endpoint that removes the device's 2 s recording cap. The
        ESP32 never buffers the utterance, so it can talk for as long as it
        likes and stops only when it has been silent for
        VAD_SILENCE_DURATION_MS.
        """
        pcm = self._read_chunked_body()
        if pcm is None:
            return  # handler already replied

        avg, peak, dur = pcm_stats(pcm)
        envelope = envelope_variation(pcm)
        # The whole-clip average under-reads a clip that is part speech and part
        # trailing silence -- exactly what the device sends when a short word is
        # followed by the 2 s endpoint pause. The loudest window measures the
        # voice itself, so use it as the primary level metric and keep `avg`
        # only for floor tracking.
        loudest = loudest_window_rms(pcm)

        # Track the quietest level seen recently -- that is this room's floor.
        # A gate fixed at a constant would either clip speech in a quiet room or
        # pass the fan in a loud one; follow the room instead.
        #
        # The floor is module state; declare it global (see note above).
        global _noise_floor_avg
        #
        # Learn the floor ONLY from clips that are not speech. The first
        # version decayed the floor upward by 2% on every request louder than
        # the current floor -- i.e. on the user's own speech -- so over a few
        # dozen turns the floor crept from 600 to 780 and need_avg climbed past
        # real utterances, rejecting them as "quiet". Observed: avg=1518 with
        # env=0.98 (clearly voice) rejected because the bar had risen to 1526.
        #
        # A clip with high envelope variation IS speech, so it is not room
        # noise and must not raise the floor. Only steady/quiet clips teach it.
        looks_like_speech = envelope >= STT_ENVELOPE_MIN
        if avg > 0 and not looks_like_speech:
            with _floor_lock:
                if avg < _noise_floor_avg:
                    _noise_floor_avg = avg
                else:
                    # Room got louder (a fan, a crowd). Follow it up, but stay
                    # under the absolute minimum so the bar can never climb into
                    # speech range.
                    grown = _noise_floor_avg + int(_noise_floor_avg * 0.05)
                    _noise_floor_avg = min(grown, STT_AVG_ABS_MIN)
        floor = _noise_floor_avg

        need_avg = max(STT_AVG_ABS_MIN, int(floor * STT_AVG_SNR_MULT))

        trimmed = trim_trailing_silence(pcm, STT_TRIM_SILENCE_MS)
        log.info(
            "STREAM-STT: %.2f s, %.1f KB, avg=%d loud=%d peak=%d env=%.2f "
            "(need loud>=%d [floor=%d], env>=%.2f) -> trimmed %.2f s",
            dur, len(pcm) / 1024.0, avg, loudest, peak, envelope,
            need_avg, floor, STT_ENVELOPE_MIN,
            len(trimmed) / 2 / STT_SAMPLE_RATE,
        )

        # Loud enough to be voice, AND bursty enough that it is not a fan.
        # Judged on the loudest window (see loudest_window_rms) so a short
        # utterance padded with endpoint silence is not scored as quiet.
        if loudest < need_avg:
            self._reject_stream("quiet", loudest, peak, envelope, dur)
            return
        if envelope < STT_ENVELOPE_MIN:
            self._reject_stream("steady-noise", loudest, peak, envelope, dur)
            return

        wav = pcm_to_wav(trimmed)
        boundary = "----NENEStreamBoundary"
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{GROQ_STT_MODEL}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="language"\r\n\r\n'
            f"{GROQ_STT_LANGUAGE}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="temperature"\r\n\r\n'
            "0.0\r\n"
            # NOTE: do NOT send no_speech_threshold here. Groq's Whisper
            # endpoint rejects it as an unknown param and 400s the whole
            # request, which silently produced "No transcript" on every turn.
            # Hallucination filtering is done client-side in
            # _filter_hallucination() instead, using verbose_json's own
            # no_speech_prob when the model provides it.
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            "verbose_json\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = prefix + wav + suffix
        self._groq_stt_post(body, f"multipart/form-data; boundary={boundary}",
                            hallucination_guard=True)

    def _reject_stream(self, reason: str, avg: int, peak: int,
                       envelope: float, dur: float) -> None:
        """Reply 200 with an empty transcript and never call Groq."""
        log.info("STREAM-STT: rejected (%s), not calling Groq", reason)
        self._send_json(
            200,
            {
                "text": "",
                "filtered": reason,
                "avg": avg,
                "peak": peak,
                "env": round(envelope, 3),
                "dur": round(dur, 2),
            },
        )



    def _handle_llm(self) -> None:
        """Transparent proxy: forward the chat payload to Groq chat-completions.

        The ESP32 cannot spare ~50KB of *contiguous* heap for a TLS handshake
        while its record buffer is in play (it saw -32512 / -20864), so the
        LLM hop runs plain HTTP to us and we do the TLS on the laptop.
        The body is forwarded byte-for-byte: model, temperature, top_p and the
        system prompt all stay firmware/dashboard-controlled.
        """
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key:
            self._send_json(500, {"error": "GROQ_API_KEY not set on the relay host"})
            return

        raw_len = int(self.headers.get("Content-Length") or 0)
        if raw_len <= 0 or raw_len > MAX_LLM_BYTES:
            self._send_json(400, {"error": "bad or missing Content-Length"})
            return

        body = self.rfile.read(raw_len)
        content_type = self.headers.get("Content-Type") or "application/json"
        log.info("LLM request: %d bytes", len(body))

        req = urllib.request.Request(
            GROQ_LLM_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                # Cloudflare rejects urllib's default agent (error 1010).
                "User-Agent": "curl/8.5.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=MAX_LLM_TIMEOUT) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log.warning("Groq LLM HTTP %s: %s", exc.code, detail[:500])
            self._send_json(502, {"error": f"groq llm http {exc.code}", "body": detail[:2000]})
            return
        except (urllib.error.URLError, OSError) as exc:
            log.error("Cannot reach Groq LLM: %s", exc)
            self._send_json(502, {"error": "cannot reach groq llm"})
            return

        log.info("LLM ok: %d bytes back", len(payload))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_tts(self, text: str, voice: str) -> None:
        if not text:
            self._send_json(400, {"error": "empty text"})
            return
        if len(text) > MAX_TEXT_CHARS:
            self._send_json(413, {"error": "text too long"})
            return
        if not voice:
            self._send_json(400, {"error": "empty voice"})
            return

        log.info("TTS request: %d chars, voice=%s", len(text), voice)
        try:
            pcm, duration = synthesize(text, voice)
        except ValueError as exc:
            log.warning("TTS request rejected: %s", exc)
            self._send_json(400, {"error": str(exc)})
            return
        except edge_tts.exceptions.NoAudioReceived as exc:
            # e.g. an unsupported/nonexistent voice name
            log.warning("No audio from TTS service: %s", exc)
            self._send_json(400, {"error": "TTS returned no audio - check the voice name"})
            return
        except edge_tts.exceptions.EdgeTTSException as exc:
            # WebSocketError / UnexpectedResponse / UnknownResponse / ...
            log.error("Edge TTS service error: %s", exc)
            self._send_json(502, {"error": "Microsoft Edge TTS service error"})
            return
        except aiohttp.ClientError as exc:  # no internet / MS unreachable
            log.error("Cannot reach Microsoft Edge TTS: %s", exc)
            self._send_json(502, {"error": "cannot reach Microsoft Edge TTS service (check internet)"})
            return
        except (miniaudio.DecodeError, OSError) as exc:
            log.error("Decoding failed: %s", exc)
            self._send_json(500, {"error": "audio decode failed"})
            return
        except Exception as exc:  # noqa: BLE001 - report any unexpected failure
            log.exception("Unexpected TTS error")
            self._send_json(500, {"error": f"internal error: {exc}"})
            return

        log.info(
            "TTS ok: voice=%s sample_rate=%d ch=%d bits=%d bytes=%d duration=%.3fs",
            voice,
            TTS_SAMPLE_RATE,
            TTS_CHANNELS,
            TTS_BITS_PER_SAMPLE,
            len(pcm),
            duration,
        )
        self._send_pcm(pcm, voice, duration)

    def _handle_tts_preview(self, query: dict) -> None:
        """Hear any word/phrase instantly -- the loop that makes the loanword
        table maintainable.

        GET /tts-preview?text=Wi-Fi             -> WAV file (playable anywhere)
        GET /tts-preview?text=Wi-Fi&dry=1       -> JSON only: what the pre-pass
                                                   does, no audio
        GET /tts-preview?text=...&rate=-10%     -> hear it slower/faster

        Returns a playable WAV so you can judge by ear in two seconds, and
        writes `tts_preview.wav` in the relay directory as a convenience.
        `rate` accepts Edge TTS syntax (+20%, -10%, +0%) so speech speed can
        be tuned by ear without touching the config or restarting the relay.
        No device and no reflash involved -- add a word to TTS_LOANWORD_MAP,
        restart, preview, done.
        """
        original = (query.get("text") or [""])[0].strip()
        voice = (query.get("voice") or [DEFAULT_VOICE])[0].strip()
        rate = (query.get("rate") or [DEFAULT_RATE])[0].strip() or DEFAULT_RATE
        dry = (query.get("dry") or [""])[0] not in ("", "0", "false")
        if not original:
            self._send_json(400, {"error": "pass ?text=..."})
            return
        if len(original) > MAX_TEXT_CHARS:
            self._send_json(413, {"error": "text too long"})
            return

        spoken = prepare_for_tts(original)
        if dry:
            self._send_json(200, {
                "original": original,
                "spoken": spoken,
                "changed": spoken != original,
                "loanwords": sorted(
                    w for w in re.findall(r"[A-Za-z][A-Za-z'\- ]*", original)
                    if w.strip().lower() in TTS_LOANWORD_MAP
                ),
                "tagalog": sorted(
                    w for w in re.findall(r"[A-Za-z][A-Za-z'\- ]*", original)
                    if w.strip().lower() in TTS_PRONUNCIATION_MAP
                ),
                "rate": rate,
                "voice": voice,
            })
            return

        log.info("TTS preview: %r -> %r (rate=%s)", original, spoken, rate)
        try:
            pcm, duration = synthesize(original, voice, rate)
        except (ValueError, edge_tts.exceptions.EdgeTTSException,
                aiohttp.ClientError, miniaudio.DecodeError, OSError) as exc:
            log.warning("TTS preview failed: %s", exc)
            self._send_json(400, {"error": str(exc), "spoken": spoken})
            return

        wav = pcm_to_wav(pcm, TTS_SAMPLE_RATE)
        try:
            with open("tts_preview.wav", "wb") as fh:
                fh.write(wav)
        except OSError:
            pass  # non-fatal: serving it in the response is what matters
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-TTS-Spoken", urllib.parse.quote(spoken))
        self.send_header("X-TTS-Rate", rate)
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)


# ---------------------------------------------------------------------------
# Selftest mode (no server)
# ---------------------------------------------------------------------------

def selftest(text: str, out: str | None) -> int:
    print(f"[selftest] voice={DEFAULT_VOICE} rate={DEFAULT_RATE} pitch={DEFAULT_PITCH}")
    print(f"[selftest] text: {text!r}")
    try:
        pcm, duration = synthesize(text, DEFAULT_VOICE)
    except Exception as exc:  # noqa: BLE001
        print(f"[selftest] FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"[selftest] sample_rate={TTS_SAMPLE_RATE} Hz")
    print(f"[selftest] channels={TTS_CHANNELS}")
    print(f"[selftest] bits_per_sample={TTS_BITS_PER_SAMPLE}")
    print(f"[selftest] duration={duration:.3f} s")
    print(f"[selftest] pcm_bytes={len(pcm)} (even={len(pcm) % 2 == 0})")
    if out:
        with open(out, "wb") as fh:
            fh.write(pcm)
        print(f"[selftest] wrote {out}")
    return 0


def _get_local_ip() -> str:
    """Get the local IP address used to reach the network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


_zeroconf = None


def _register_mdns(port: int) -> None:
    global _zeroconf
    try:
        zc = Zeroconf()
        local_ip = _get_local_ip()
        info = ServiceInfo(
            "_http._tcp.local.",
            "nene-relay._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={},
            server="nene-relay.local.",
        )
        zc.register_service(info)
        _zeroconf = zc
        log.info("mDNS advertised as nene-relay.local:%d (IP: %s)", port, local_ip)
    except Exception as e:
        log.warning("mDNS advertisement failed (will still work by IP): %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description="NENE Edge TTS relay")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--voice", default=None, help="default TTS voice")
    parser.add_argument(
        "--selftest",
        nargs="?",
        const="Kumusta pre? Ako si NENE.",
        metavar="TEXT",
        help="synthesize TEXT, print format info, then exit (no server)",
    )
    parser.add_argument("--out", default="", help="selftest PCM output file")
    args = parser.parse_args()

    global DEFAULT_VOICE  # noqa: PLW0603
    if args.voice:
        DEFAULT_VOICE = args.voice

    if args.selftest is not None:
        return selftest(args.selftest, args.out or None)

    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    server.daemon_threads = True
    _register_mdns(args.port)
    log.info(
        "NENE Edge TTS relay ready: http://%s:%d  (voice=%s, rate=%s, pitch=%s, "
        "needs internet to Microsoft's Edge TTS)",
        args.host,
        args.port,
        DEFAULT_VOICE,
        DEFAULT_RATE,
        DEFAULT_PITCH,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        try:
            if _zeroconf:
                _zeroconf.close()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())