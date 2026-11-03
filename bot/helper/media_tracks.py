import asyncio
import json
import logging
import shutil
import subprocess
import time
from typing import Dict, List, Optional
from bot.config import Telegram

logger = logging.getLogger(__name__)

MEDIA_TRACKS_CACHE: Dict[str, dict] = {}
MEDIA_TRACKS_CACHE_TIME: Dict[str, float] = {}
CACHE_TTL = 3600  # 1 hour

TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"
}


def clear_media_tracks_cache(key: Optional[str] = None) -> None:
    """Clear in-memory tracks cache for a specific key or all keys."""
    if key:
        MEDIA_TRACKS_CACHE.pop(key, None)
        MEDIA_TRACKS_CACHE_TIME.pop(key, None)
    else:
        MEDIA_TRACKS_CACHE.clear()
        MEDIA_TRACKS_CACHE_TIME.clear()


async def probe_media_tracks(chat_id: int, message_id: int, secure_hash: str, stream_url: Optional[str] = None) -> dict:
    """
    Probes audio and subtitle tracks for a media stream using ffprobe.
    Returns:
        {
            "audio_tracks": [
                {"stream_index": int, "track_index": int, "codec": str, "language": str, "title": str, "channels": int}
            ],
            "subtitle_tracks": [
                {"stream_index": int, "track_index": int, "codec": str, "language": str, "title": str, "is_text": bool}
            ]
        }
    """
    cache_key = f"{chat_id}_{message_id}"
    now = time.time()
    if cache_key in MEDIA_TRACKS_CACHE:
        if now - MEDIA_TRACKS_CACHE_TIME.get(cache_key, 0) < CACHE_TTL:
            return MEDIA_TRACKS_CACHE[cache_key]

    empty_result = {"audio_tracks": [], "subtitle_tracks": []}

    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        logger.warning("ffprobe binary not found in PATH")
        return empty_result

    if not stream_url:
        stream_url = f"http://127.0.0.1:{Telegram.PORT}/{chat_id}/probe?id={message_id}&hash={secure_hash}"

    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "stream=index,codec_name,codec_type,channels:stream_tags=language,title",
        "-of", "json",
        stream_url
    ]

    try:
        proc_res = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            timeout=20.0
        )
        stdout = proc_res.stdout
        stderr = proc_res.stderr
        returncode = proc_res.returncode
    except subprocess.TimeoutExpired:
        logger.error("ffprobe timed out while probing %s", stream_url)
        return empty_result
    except Exception as e:
        logger.error("Error executing ffprobe: %s", e)
        return empty_result

    if returncode != 0:
        err_msg = stderr.decode('utf-8', errors='ignore') if stderr else ""
        logger.warning("ffprobe returned non-zero code %d: %s", returncode, err_msg)
        return empty_result

    try:
        probe_data = json.loads(stdout.decode('utf-8', errors='ignore'))
    except Exception as e:
        logger.error("Failed to parse ffprobe json output: %s", e)
        return empty_result

    streams = probe_data.get("streams", [])
    audio_tracks = []
    subtitle_tracks = []

    audio_idx = 0
    subtitle_idx = 0

    for stream in streams:
        c_type = stream.get("codec_type")
        stream_index = int(stream.get("index", 0))
        codec_name = stream.get("codec_name", "")
        tags = stream.get("tags") or {}
        tags_lower = {str(k).lower(): str(v) for k, v in tags.items()}
        lang = tags_lower.get("language", "und")
        title = tags_lower.get("title", "").strip()

        if c_type == "audio":
            channels = int(stream.get("channels", 2))
            track_title = title if title else f"Track {audio_idx + 1} ({lang})"
            audio_tracks.append({
                "stream_index": stream_index,
                "track_index": audio_idx,
                "codec": codec_name,
                "language": lang,
                "title": track_title,
                "channels": channels
            })
            audio_idx += 1
        elif c_type == "subtitle":
            is_text = codec_name.lower() in TEXT_SUBTITLE_CODECS
            sub_title = title if title else f"Subtitle {subtitle_idx + 1} ({lang})"
            subtitle_tracks.append({
                "stream_index": stream_index,
                "track_index": subtitle_idx,
                "codec": codec_name,
                "language": lang,
                "title": sub_title,
                "is_text": is_text
            })
            subtitle_idx += 1

    result = {
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks
    }

    MEDIA_TRACKS_CACHE[cache_key] = result
    MEDIA_TRACKS_CACHE_TIME[cache_key] = now
    return result
