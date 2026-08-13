"""Download a Suno song as MP3 from CDN (same source the web player uses)."""

from __future__ import annotations

from io import BytesIO
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import time
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from mutagen.id3 import APIC, COMM, TIT2, TPE1, TXXX, TYER, ID3
from mutagen.mp3 import MP3

CDN_MP3 = "https://cdn1.suno.ai/{song_id}.mp3"
OEMBED_API = "https://studio-api-prod.suno.com/api/oembed"
PLAYLIST_API = "https://studio-api-prod.suno.com/api/playlist/{playlist_id}"
PROFILE_API = "https://studio-api-prod.suno.com/api/profiles/{handle}"
FEED_V3_API = "https://studio-api-prod.suno.com/api/feed/v3"
FEED_PAGE_SIZE = 20
PROFILE_PAGE_BACKOFF_INITIAL_SEC = 10
PROFILE_PAGE_BACKOFF_MAX_SEC = 60
SONG_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?suno\.com/song/([0-9a-f-]{36})",
    re.IGNORECASE,
)
PLAYLIST_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?suno\.com/playlist/([0-9a-f-]{36})",
    re.IGNORECASE,
)
HOOK_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?suno\.com/(?:@[^/]+/)?hook/([0-9a-f-]{36})",
    re.IGNORECASE,
)
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
DEFAULT_MP3_BITRATE_KBPS = 190
STREAMING_INITIAL_CHUNK_BYTES = 65_536
STREAMING_CHUNK_BYTES = 256 * 1024
STREAMING_BUFFER_AHEAD_SEC = 20.0

DownloadMode = Literal["fast", "streaming"]
HookDownloadFormat = Literal["both", "mp4", "mp3"]
BulkMediaKind = Literal["song", "hook"]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

ProgressCallback = Callable[[int, int | None], None]
TrackProgressCallback = Callable[[int, int, str], None]

SunoInputKind = Literal["song", "playlist", "hook"]


@dataclass
class SongMetadata:
    id: str
    title: str
    artist: str
    audio_url: str
    page_url: str
    model_version: str | None = None
    model_name: str | None = None
    created_at: str | None = None
    duration_sec: float | None = None
    styles: str | None = None
    prompt: str | None = None
    bitrate_kbps: int | None = None
    playlist_name: str | None = None
    playlist_url: str | None = None
    image_url: str | None = None
    clip_start_sec: float | None = None
    clip_end_sec: float | None = None
    play_count: int | None = None
    upvote_count: int | None = None


@dataclass(frozen=True)
class ClipRange:
    """Optional clip window. end_sec None means end of the track."""

    start_sec: float
    end_sec: float | None = None


@dataclass
class PlaylistMetadata:
    id: str
    name: str
    description: str | None
    page_url: str
    song_count: int
    clips: list[SongMetadata]


@dataclass
class UserProfileMetadata:
    handle: str
    display_name: str
    num_total_clips: int
    clips: list[SongMetadata]


@dataclass
class HookMetadata:
    id: str
    title: str
    page_url: str
    video_url: str | None = None
    audio_url: str | None = None
    created_at: str | None = None
    play_count: int | None = None
    upvote_count: int | None = None
    duration_sec: float | None = None
    image_url: str | None = None
    artist: str | None = None
    song_id: str | None = None


@dataclass
class TokenSession:
    token: str
    handle: str
    display_name: str | None = None
    expires_at: datetime | None = None


@dataclass
class BulkMediaItem:
    kind: BulkMediaKind
    id: str
    title: str
    created_at: str | None = None
    play_count: int | None = None
    upvote_count: int | None = None
    duration_sec: float | None = None
    song: SongMetadata | None = None
    hook: HookMetadata | None = None


def parse_timestamp(value: str) -> float:
    """Parse seconds (90.5) or MM:SS / HH:MM:SS into seconds."""
    text = value.strip()
    if not text:
        raise ValueError("Timestamp cannot be empty.")

    if re.fullmatch(r"\d+(\.\d+)?", text):
        seconds = float(text)
        if seconds < 0:
            raise ValueError(f"Timestamp cannot be negative: {value}")
        return seconds

    parts = text.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            total = int(minutes) * 60 + float(seconds)
        elif len(parts) == 3:
            hours, minutes, seconds = parts
            total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        else:
            raise ValueError
    except ValueError:
        raise ValueError(
            f"Invalid timestamp: {value!r}. Use seconds (90) or MM:SS (1:30)."
        ) from None

    if total < 0:
        raise ValueError(f"Timestamp cannot be negative: {value}")
    return total


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS for tags and display."""
    total = max(0, int(round(seconds)))
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}:{secs:02d}"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_timestamp_filename(seconds: float) -> str:
    """Filesystem-safe clip timestamp (no colons)."""
    total = max(0, int(round(seconds)))
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m{secs:02d}s"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def clip_from_optional_strings(
    start_raw: str | None,
    end_raw: str | None,
) -> ClipRange | None:
    """
    Build a clip range when either start or end is provided.

    Blank start is treated as 0; blank end means through end of file.
    Both blank returns None (full song).
    """
    start_text = (start_raw or "").strip()
    end_text = (end_raw or "").strip()
    if not start_text and not end_text:
        return None

    start_sec = parse_timestamp(start_text) if start_text else 0.0
    end_sec = parse_timestamp(end_text) if end_text else None
    return ClipRange(start_sec=start_sec, end_sec=end_sec)


def finalize_clip_range(
    clip: ClipRange,
    *,
    duration_sec: float | None,
    file_duration_sec: float | None = None,
) -> tuple[float, float]:
    """Resolve clip end and validate against known or probed duration."""
    end_sec = clip.end_sec
    if end_sec is None:
        end_sec = duration_sec if duration_sec is not None else file_duration_sec
    if end_sec is None:
        raise ValueError(
            "Could not determine clip end time. Provide an end time or ensure "
            "song duration is available."
        )

    if duration_sec is not None and end_sec > duration_sec:
        end_sec = duration_sec

    if clip.start_sec >= end_sec:
        raise ValueError(
            f"Clip start ({format_timestamp(clip.start_sec)}) must be before end "
            f"({format_timestamp(end_sec)})."
        )
    return clip.start_sec, end_sec


def resolve_ffmpeg_exe() -> str:
    """Return ffmpeg on PATH, or the bundled binary from imageio-ffmpeg."""
    if path := shutil.which("ffmpeg"):
        return path
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "Clip download requires ffmpeg. Install imageio-ffmpeg (uv sync) "
            "or install ffmpeg on your system PATH."
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def trim_mp3_clip(
    src: Path,
    dest: Path,
    start_sec: float,
    end_sec: float,
) -> None:
    """Trim an MP3 to [start_sec, end_sec] using ffmpeg stream copy."""
    ffmpeg = resolve_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        str(start_sec),
        "-to",
        str(end_sec),
        "-i",
        str(src),
        "-c",
        "copy",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip() if exc.stderr else str(exc)
        raise RuntimeError(f"ffmpeg clip failed: {detail}") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg executable not found. Run uv sync or install ffmpeg."
        ) from exc


def parse_song_id(value: str) -> str:
    """Extract a Suno song UUID from a URL or raw ID string."""
    url_match = SONG_URL_PATTERN.search(value.strip())
    if url_match:
        return url_match.group(1)

    candidate = value.strip()
    if UUID_PATTERN.match(candidate):
        return candidate

    raise ValueError(f"Could not parse Suno song ID from: {value}")


def parse_playlist_id(value: str) -> str:
    """Extract a Suno playlist UUID from a URL or raw ID string."""
    url_match = PLAYLIST_URL_PATTERN.search(value.strip())
    if url_match:
        return url_match.group(1)

    candidate = value.strip()
    if UUID_PATTERN.match(candidate):
        return candidate

    raise ValueError(f"Could not parse Suno playlist ID from: {value}")


def parse_playlist_id(value: str) -> str:
    """Extract a Suno playlist UUID from a URL or raw ID string."""
    url_match = PLAYLIST_URL_PATTERN.search(value.strip())
    if url_match:
        return url_match.group(1)

    candidate = value.strip()
    if UUID_PATTERN.match(candidate):
        return candidate

    raise ValueError(f"Could not parse Suno playlist ID from: {value}")


def parse_hook_id(value: str) -> str:
    """Extract a Suno hook UUID from a URL or raw ID string."""
    url_match = HOOK_URL_PATTERN.search(value.strip())
    if url_match:
        return url_match.group(1)

    candidate = value.strip()
    if UUID_PATTERN.match(candidate):
        return candidate

    raise ValueError(f"Could not parse Suno hook ID from: {value}")


def parse_suno_input(value: str) -> tuple[SunoInputKind, str]:
    """Detect whether input is a song, playlist, or hook URL/ID."""
    stripped = value.strip()
    if PLAYLIST_URL_PATTERN.search(stripped):
        return "playlist", parse_playlist_id(stripped)
    if HOOK_URL_PATTERN.search(stripped):
        return "hook", parse_hook_id(stripped)
    if SONG_URL_PATTERN.search(stripped):
        return "song", parse_song_id(stripped)
    if UUID_PATTERN.match(stripped):
        return "song", stripped
    raise ValueError(
        "Could not parse Suno URL or ID. Expected a song, playlist, or hook URL, "
        "or a 36-character UUID."
    )


def normalize_bearer_token(raw: str) -> str:
    """Normalize pasted bearer token input (strip prefixes, validate JWT shape)."""
    text = raw.strip()
    if not text:
        raise ValueError("Bearer token cannot be empty.")

    if text.lower().startswith("authorization:"):
        text = text.split(":", 1)[1].strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()

    parts = text.split(".")
    if len(parts) != 3 or not parts[0].startswith("eyJ"):
        raise ValueError(
            "Invalid token format. Paste only the long eyJ… string from the "
            "Authorization header (the part after 'Bearer ')."
        )
    return text


def decode_bearer_token_claims(token: str) -> dict[str, Any] | None:
    """Decode JWT payload without verifying signature (UX only)."""
    try:
        normalized = normalize_bearer_token(token)
    except ValueError:
        return None
    parts = normalized.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def bearer_token_expires_at(token: str) -> datetime | None:
    claims = decode_bearer_token_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)


def bearer_token_is_expired(token: str) -> bool:
    expires = bearer_token_expires_at(token)
    if expires is None:
        return False
    return expires <= datetime.now(tz=timezone.utc)


def mask_bearer_token(token: str) -> str:
    if len(token) <= 8:
        return "…"
    return f"…{token[-4:]}"


def build_auth_headers(token: str) -> dict[str, str]:
    normalized = normalize_bearer_token(token)
    return {
        **DEFAULT_HEADERS,
        "Authorization": f"Bearer {normalized}",
        "Origin": "https://suno.com",
        "Referer": "https://suno.com/",
        "Content-Type": "application/json",
    }


def _profile_handle(handle: str) -> str:
    return handle.strip().lstrip("@")


def _get_json_with_backoff(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    backoff = PROFILE_PAGE_BACKOFF_INITIAL_SEC
    while True:
        response = client.get(url, params=params)
        if response.status_code == 429:
            if backoff > PROFILE_PAGE_BACKOFF_MAX_SEC:
                raise RuntimeError(
                    f"Rate limited fetching profile (HTTP 429). "
                    f"Exceeded {PROFILE_PAGE_BACKOFF_MAX_SEC}s backoff."
                )
            time.sleep(backoff)
            backoff += 5
            continue
        if response.status_code == 404:
            raise ValueError("User not found or profile is not public.")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected profile API response.")
        return data


def fetch_user_profile_clips(
    handle: str,
    client: httpx.Client | None = None,
    *,
    on_page: Callable[[int, int], None] | None = None,
) -> UserProfileMetadata:
    """Fetch all published songs from a public user profile."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=60) as c:
            return fetch_user_profile_clips(handle, client=c, on_page=on_page)

    username = _profile_handle(handle)
    url = PROFILE_API.format(handle=username)
    clips: list[SongMetadata] = []
    page = 1
    display_name = username
    num_total = 0

    while True:
        data = _get_json_with_backoff(
            client,
            url,
            params={
                "page": page,
                "playlists_sort_by": "created_at",
                "clips_sort_by": "created_at",
            },
        )
        if page == 1:
            display_name = data.get("display_name") or username
            num_total = int(data.get("num_total_clips") or 0)

        batch = data.get("clips") or []
        for clip in batch:
            if not isinstance(clip, dict):
                continue
            metadata = clip_dict_to_song_metadata(clip)
            if metadata is not None:
                clips.append(metadata)

        if on_page:
            on_page(page, num_total or len(clips))

        if not batch:
            break
        if num_total and len(clips) >= num_total:
            break
        page += 1

    return UserProfileMetadata(
        handle=username,
        display_name=display_name,
        num_total_clips=num_total or len(clips),
        clips=clips,
    )


def _hook_display_title(raw: dict[str, Any], hook_id: str) -> str:
    """Hooks often use caption instead of title in Suno payloads."""
    for key in ("caption", "title"):
        value = raw.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and cleaned != hook_id:
                return cleaned
    return hook_id


def _parse_hook_dict(raw: dict[str, Any]) -> HookMetadata | None:
    hook_id = raw.get("id")
    if not hook_id:
        return None
    title = _hook_display_title(raw, hook_id)
    video_url = raw.get("video_url")
    audio_url = raw.get("audio_url")
    if not video_url and not audio_url:
        return None

    nested = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    duration_raw = nested.get("duration")
    duration_sec = float(duration_raw) if duration_raw is not None else None
    handle = raw.get("handle")
    page_url = (
        f"https://suno.com/@{handle}/hook/{hook_id}"
        if handle
        else f"https://suno.com/hook/{hook_id}"
    )
    image_url = raw.get("image_large_url") or raw.get("image_url")
    song_id = (
        nested.get("clip_id")
        or nested.get("speed_clip_id")
        or raw.get("original_clip_id")
    )

    return HookMetadata(
        id=hook_id,
        title=title,
        page_url=page_url,
        video_url=video_url,
        audio_url=audio_url,
        created_at=raw.get("created_at"),
        play_count=raw.get("play_count"),
        upvote_count=raw.get("upvote_count"),
        duration_sec=duration_sec,
        image_url=image_url.strip() if isinstance(image_url, str) else None,
        artist=handle,
        song_id=song_id if isinstance(song_id, str) else None,
    )


def _parse_embedded_clip_objects(html: str, entity_type: str) -> list[dict[str, Any]]:
    """Extract clip-like JSON objects from Suno SSR/RSC payloads."""
    results: list[dict[str, Any]] = []
    marker = f'\\"entity_type\\":\\"{entity_type}\\"'
    start = 0
    while True:
        idx = html.find(marker, start)
        if idx < 0:
            break
        window_start = max(0, idx - 4000)
        window = html[window_start : idx + 8000]
        hook_id = _extract_field(window, "id")
        title = _extract_field(window, "title")
        caption = _extract_field(window, "caption")
        if not hook_id:
            start = idx + len(marker)
            continue

        nested = {}
        duration_raw = _extract_field(window, "duration")
        if duration_raw:
            try:
                nested["duration"] = float(duration_raw)
            except ValueError:
                pass

        raw: dict[str, Any] = {
            "id": hook_id,
            "title": title or caption or hook_id,
            "caption": caption,
            "entity_type": entity_type,
            "video_url": _extract_field(window, "video_url"),
            "audio_url": _extract_field(window, "audio_url"),
            "created_at": _extract_field(window, "created_at"),
            "handle": _extract_field(window, "handle"),
            "image_url": _extract_field(window, "image_url"),
            "image_large_url": _extract_field(window, "image_large_url"),
            "original_clip_id": _extract_field(window, "original_clip_id"),
            "metadata": nested,
        }
        play_count = _extract_field(window, "play_count")
        if play_count and play_count.isdigit():
            raw["play_count"] = int(play_count)
        upvote_count = _extract_field(window, "upvote_count")
        if upvote_count and upvote_count.isdigit():
            raw["upvote_count"] = int(upvote_count)
        results.append(raw)
        start = idx + len(marker)
    return results


def fetch_user_hooks(
    handle: str,
    client: httpx.Client | None = None,
) -> list[HookMetadata]:
    """Fetch published hooks from a user's profile hooks tab HTML."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=60) as c:
            return fetch_user_hooks(handle, client=c)

    username = _profile_handle(handle)
    page_url = f"https://suno.com/@{username}?page=hooks"
    response = client.get(page_url, follow_redirects=True)
    response.raise_for_status()
    html = response.text

    hooks: list[HookMetadata] = []
    seen: set[str] = set()
    for raw in _parse_embedded_clip_objects(html, "hook_schema"):
        metadata = _parse_hook_dict(raw)
        if metadata is None or metadata.id in seen:
            continue
        seen.add(metadata.id)
        hooks.append(metadata)
    return hooks


def fetch_hook_metadata(
    hook_id: str,
    client: httpx.Client | None = None,
) -> HookMetadata:
    """Fetch hook metadata by scraping the public hook page."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=60) as c:
            return fetch_hook_metadata(hook_id, client=c)

    page_url = f"https://suno.com/hook/{hook_id}"
    response = client.get(page_url, follow_redirects=True)
    response.raise_for_status()
    html = response.text

    for raw in _parse_embedded_clip_objects(html, "hook_schema"):
        if raw.get("id") == hook_id:
            metadata = _parse_hook_dict(raw)
            if metadata is not None:
                return metadata

    audio_url = None
    audio_match = re.search(
        rf'\\"audio_url\\":\\"(https://cdn[^\\]*{re.escape(hook_id)}[^\\]*)\\"',
        html,
    )
    if audio_match:
        audio_url = audio_match.group(1)
    video_match = re.search(
        rf'\\"video_url\\":\\"(https://cdn[^\\]*)\\"',
        html,
    )
    video_url = video_match.group(1) if video_match else None
    if not audio_url and not video_url:
        raise ValueError(f"Could not find downloadable media for hook: {hook_id}")

    title = hook_id
    for key in ("caption", "title"):
        match = re.search(rf'\\"{key}\\":\\"((?:\\\\.|[^"\\])*)\\"', html)
        if match:
            candidate = _unescape_json_string(match.group(1)).strip()
            if candidate and candidate != hook_id:
                title = candidate
                break

    return HookMetadata(
        id=hook_id,
        title=title,
        page_url=page_url,
        video_url=video_url,
        audio_url=audio_url,
    )


def resolve_token_handle(
    token: str,
    client: httpx.Client | None = None,
) -> TokenSession:
    """Validate bearer token and resolve the authenticated user's handle."""
    normalized = normalize_bearer_token(token)
    if bearer_token_is_expired(normalized):
        raise ValueError("Bearer token has expired. Get a fresh token from suno.com.")

    if client is None:
        with httpx.Client(headers=build_auth_headers(normalized), timeout=60) as c:
            return resolve_token_handle(normalized, client=c)

    response = client.post(
        FEED_V3_API,
        json={
            "cursor": None,
            "limit": 1,
            "filters": {
                "disliked": "False",
                "trashed": "False",
                "stem": {"presence": "False"},
                "fromStudioProject": {"presence": "False"},
            },
        },
    )
    if response.status_code == 401:
        raise ValueError(
            "Bearer token was rejected (401). Get a fresh token from suno.com."
        )
    response.raise_for_status()
    data = response.json()
    clips = data.get("clips") or []
    if not clips:
        raise ValueError("Token is valid but no library clips were returned.")

    first = clips[0]
    handle = first.get("handle")
    if not handle:
        raise ValueError("Could not determine Suno handle from token.")

    return TokenSession(
        token=normalized,
        handle=handle,
        display_name=first.get("display_name"),
        expires_at=bearer_token_expires_at(normalized),
    )


def fetch_private_library(
    token: str,
    client: httpx.Client | None = None,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> list[SongMetadata]:
    """Fetch all songs from the authenticated user's library (including private)."""
    normalized = normalize_bearer_token(token)
    if client is None:
        with httpx.Client(headers=build_auth_headers(normalized), timeout=120) as c:
            return fetch_private_library(normalized, client=c, on_progress=on_progress)

    clips: list[SongMetadata] = []
    cursor: str | None = None
    while True:
        payload: dict[str, Any] = {
            "cursor": cursor,
            "limit": FEED_PAGE_SIZE,
            "filters": {
                "disliked": "False",
                "trashed": "False",
                "stem": {"presence": "False"},
                "fromStudioProject": {"presence": "False"},
            },
        }
        response = client.post(FEED_V3_API, json=payload)
        if response.status_code == 401:
            raise ValueError(
                "Bearer token was rejected (401). Get a fresh token from suno.com."
            )
        response.raise_for_status()
        data = response.json()
        batch = data.get("clips") or []
        for clip in batch:
            if not isinstance(clip, dict):
                continue
            metadata = clip_dict_to_song_metadata(clip)
            if metadata is not None:
                clips.append(metadata)
        if on_progress:
            on_progress(len(clips))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return clips


def song_metadata_to_bulk_item(metadata: SongMetadata) -> BulkMediaItem:
    return BulkMediaItem(
        kind="song",
        id=metadata.id,
        title=metadata.title,
        created_at=metadata.created_at,
        play_count=metadata.play_count,
        upvote_count=metadata.upvote_count,
        duration_sec=metadata.duration_sec,
        song=metadata,
    )


def hook_metadata_to_bulk_item(metadata: HookMetadata) -> BulkMediaItem:
    return BulkMediaItem(
        kind="hook",
        id=metadata.id,
        title=metadata.title,
        created_at=metadata.created_at,
        play_count=metadata.play_count,
        upvote_count=metadata.upvote_count,
        duration_sec=metadata.duration_sec,
        hook=metadata,
    )


def extract_audio_from_video(src: Path, dest: Path) -> None:
    """Extract MP3 audio from a video file using ffmpeg."""
    ffmpeg = resolve_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-q:a",
        "2",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip() if exc.stderr else str(exc)
        raise RuntimeError(f"ffmpeg audio extract failed: {detail}") from exc


def download_hook_media(
    hook: HookMetadata,
    output_dir: Path,
    client: httpx.Client,
    *,
    hook_format: HookDownloadFormat = "both",
    progress: ProgressCallback | None = None,
    overwrite: bool = False,
    attach_metadata: bool = True,
) -> list[Path]:
    """Download hook MP4 and/or MP3 according to format preference."""
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = sanitize_filename(hook.title)
    saved: list[Path] = []

    want_mp4 = hook_format in ("both", "mp4")
    want_mp3 = hook_format in ("both", "mp3")

    mp4_path = output_dir / f"{base_name}.mp4"
    mp3_path = output_dir / f"{base_name}.mp3"
    if not overwrite:
        mp4_path = unique_filepath(mp4_path)
        mp3_path = unique_filepath(mp3_path)
    temp_mp4: Path | None = None

    if want_mp4 and hook.video_url:
        download_file(hook.video_url, mp4_path, client, progress=progress)
        saved.append(mp4_path)

    if want_mp3:
        if hook.audio_url:
            download_file(hook.audio_url, mp3_path, client, progress=progress)
            if attach_metadata and hook.song_id:
                song_meta = fetch_song_metadata(hook.song_id, client=client, full=False)
                song_meta.title = hook.title
                apply_mp3_tags(mp3_path, song_meta, client=client)
            saved.append(mp3_path)
        elif hook.video_url:
            download_file(hook.audio_url, mp3_path, client, progress=progress)
            if attach_metadata and hook.song_id:
                song_meta = fetch_song_metadata(hook.song_id, client=client, full=False)
                song_meta.title = hook.title
                apply_mp3_tags(mp3_path, song_meta, client=client)
            saved.append(mp3_path)
        elif hook.video_url:
            source_mp4 = mp4_path if mp4_path.exists() else None
            if source_mp4 is None:
                fd, temp_name = tempfile.mkstemp(suffix=".mp4", dir=output_dir)
                os.close(fd)
                temp_mp4 = Path(temp_name)
                download_file(hook.video_url, temp_mp4, client, progress=progress)
                source_mp4 = temp_mp4
            extract_audio_from_video(source_mp4, mp3_path)
            if attach_metadata and hook.song_id:
                song_meta = fetch_song_metadata(hook.song_id, client=client, full=False)
                song_meta.title = hook.title
                apply_mp3_tags(mp3_path, song_meta, client=client)
            saved.append(mp3_path)
        else:
            raise ValueError(f"No audio or video URL for hook: {hook.title}")

    if temp_mp4 is not None:
        temp_mp4.unlink(missing_ok=True)

    return saved


def download_hook(
    hook_id_or_url: str,
    output_dir: Path,
    *,
    hook_format: HookDownloadFormat = "both",
    progress: ProgressCallback | None = None,
    overwrite: bool = False,
    attach_metadata: bool = True,
) -> tuple[list[Path], HookMetadata]:
    hook_id = parse_hook_id(hook_id_or_url)
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=120) as client:
        metadata = fetch_hook_metadata(hook_id, client=client)
        paths = download_hook_media(
            metadata,
            output_dir,
            client,
            hook_format=hook_format,
            progress=progress,
            overwrite=overwrite,
            attach_metadata=attach_metadata,
        )
        return paths, metadata


def download_bulk_items(
    items: list[BulkMediaItem],
    output_dir: Path,
    *,
    folder_name: str,
    use_subfolder: bool = True,
    hook_format: HookDownloadFormat = "both",
    progress: ProgressCallback | None = None,
    on_item: Callable[[int, int, str], None] | None = None,
    overwrite: bool = False,
    attach_metadata: bool = True,
) -> tuple[list[Path], list[tuple[BulkMediaItem, str]]]:
    """Download a list of bulk media items (songs and hooks)."""
    target_dir = output_dir
    if use_subfolder:
        target_dir = output_dir / sanitize_filename(folder_name)

    paths: list[Path] = []
    failures: list[tuple[BulkMediaItem, str]] = []
    total = len(items)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=120) as client:
        for index, item in enumerate(items, start=1):
            if on_item:
                on_item(index, total, item.title)

            track_progress: ProgressCallback | None = None
            if progress:
                base_pct = (index - 1) * 100

                def track_progress(done: int, total_bytes: int | None, _base=base_pct) -> None:
                    if total_bytes and total_bytes > 0:
                        track_pct = min(99, done * 100 // total_bytes)
                    else:
                        track_pct = 0 if done == 0 else 50
                    overall = min(100, (_base + track_pct) // total)
                    progress(overall, 100)

            try:
                if item.kind == "song" and item.song is not None:
                    path, _ = download_song_mp3_from_metadata(
                        item.song,
                        target_dir,
                        client,
                        progress=track_progress,
                        overwrite=overwrite,
                        attach_metadata=attach_metadata,
                    )
                    paths.append(path)
                elif item.kind == "hook" and item.hook is not None:
                    saved = download_hook_media(
                        item.hook,
                        target_dir,
                        client,
                        hook_format=hook_format,
                        progress=track_progress,
                        overwrite=overwrite,
                        attach_metadata=attach_metadata,
                    )
                    paths.extend(saved)
                else:
                    raise ValueError(f"Missing metadata for item: {item.title}")
            except Exception as exc:
                failures.append((item, str(exc)))

    if progress:
        progress(100, 100)
    return paths, failures


def _unescape_json_string(value: str) -> str:
    """Decode JSON-style escapes from SSR payloads without mangling UTF-8 text."""
    if not value or "\\" not in value:
        return value
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        try:
            return bytes(value, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            return value


def _extract_field(window: str, key: str) -> str | None:
    if key == "duration":
        match = re.search(r'\\"duration\\":([0-9.]+)', window)
        return match.group(1) if match else None

    match = re.search(rf'\\"{key}\\":\\"((?:\\\\.|[^"\\])*)\\"', window)
    return _unescape_json_string(match.group(1)) if match else None


def fetch_song_metadata(
    song_id: str,
    client: httpx.Client | None = None,
    *,
    full: bool = True,
) -> SongMetadata:
    """Fetch song metadata from oEmbed and the public song page."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as c:
            return fetch_song_metadata(song_id, client=c, full=full)

    page_url = f"https://suno.com/song/{song_id}"
    oembed_url = f"{OEMBED_API}?url={page_url}"
    audio_url = CDN_MP3.format(song_id=song_id)

    title = song_id
    try:
        response = client.get(oembed_url)
        if response.status_code == 200:
            title = response.json().get("title") or title
    except httpx.HTTPError:
        pass

    artist = ""
    model_version = None
    model_name = None
    created_at = None
    duration_sec = None
    styles = None
    prompt = None
    image_url = f"https://cdn2.suno.ai/image_large_{song_id}.jpeg"

    try:
        page = client.get(page_url, follow_redirects=True)
        page.raise_for_status()
        text = page.text
        audio_pattern = rf'\\"audio_url\\":\\"(https://cdn[^\\]*{re.escape(song_id)}[^\\]*)\\"'
        audio_match = re.search(audio_pattern, text)
        if audio_match:
            audio_url = audio_match.group(1)
            window = text[audio_match.start() - 3000 : audio_match.end() + 8000]
            title = _extract_field(window, "title") or title
            artist = _extract_field(window, "handle") or ""
            model_version = _extract_field(window, "major_model_version")
            model_name = _extract_field(window, "model_name")
            created_at = _extract_field(window, "created_at")
            styles = _extract_field(window, "tags") or _extract_field(window, "display_tags")
            prompt = _extract_field(window, "prompt")
            duration_raw = _extract_field(window, "duration")
            if duration_raw:
                duration_sec = float(duration_raw)
            image_url = (
                _extract_field(window, "image_large_url")
                or _extract_field(window, "image_url")
                or image_url
            )
            if image_url:
                image_url = image_url.strip()
    except httpx.HTTPError:
        pass

    if not full:
        return SongMetadata(
            id=song_id,
            title=title,
            artist="",
            audio_url=audio_url,
            page_url=page_url,
            duration_sec=duration_sec,
        )

    return SongMetadata(
        id=song_id,
        title=title,
        artist=artist,
        audio_url=audio_url,
        page_url=page_url,
        model_version=model_version,
        model_name=model_name,
        created_at=created_at,
        duration_sec=duration_sec,
        styles=styles,
        prompt=prompt,
        image_url=image_url,
    )


def clip_dict_to_song_metadata(
    clip: dict,
    *,
    playlist: PlaylistMetadata | None = None,
) -> SongMetadata | None:
    """Map a playlist API clip object to SongMetadata."""
    if clip.get("status") != "complete":
        return None

    song_id = clip.get("id")
    audio_url = clip.get("audio_url")
    if not song_id or not audio_url:
        return None

    nested = clip.get("metadata") or {}
    duration_raw = nested.get("duration")
    duration_sec = float(duration_raw) if duration_raw is not None else None
    styles = nested.get("tags") or clip.get("display_tags")

    image_url = (
        clip.get("image_large_url")
        or clip.get("image_url")
        or f"https://cdn2.suno.ai/image_large_{song_id}.jpeg"
    )

    return SongMetadata(
        id=song_id,
        title=clip.get("title") or song_id,
        artist=clip.get("handle") or "",
        audio_url=audio_url,
        page_url=f"https://suno.com/song/{song_id}",
        model_version=clip.get("major_model_version"),
        model_name=clip.get("model_name"),
        created_at=clip.get("created_at"),
        duration_sec=duration_sec,
        styles=styles,
        prompt=nested.get("prompt"),
        playlist_name=playlist.name if playlist else None,
        playlist_url=playlist.page_url if playlist else None,
        image_url=image_url.strip() if image_url else None,
        play_count=clip.get("play_count"),
        upvote_count=clip.get("upvote_count"),
    )


def fetch_playlist_metadata(
    playlist_id: str,
    client: httpx.Client | None = None,
) -> PlaylistMetadata:
    """Fetch playlist metadata and all downloadable clips from the public API."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as c:
            return fetch_playlist_metadata(playlist_id, client=c)

    page_url = f"https://suno.com/playlist/{playlist_id}"
    response = client.get(PLAYLIST_API.format(playlist_id=playlist_id))
    if response.status_code == 404:
        raise ValueError(f"Playlist not found or not public: {playlist_id}")
    response.raise_for_status()
    data = response.json()

    name = data.get("name") or playlist_id
    description = data.get("description")
    song_count = int(data.get("num_total_results") or 0)
    playlist = PlaylistMetadata(
        id=playlist_id,
        name=name,
        description=description,
        page_url=page_url,
        song_count=song_count,
        clips=[],
    )

    for entry in data.get("playlist_clips") or []:
        clip = entry.get("clip") if isinstance(entry, dict) else None
        if not isinstance(clip, dict):
            continue
        metadata = clip_dict_to_song_metadata(clip, playlist=playlist)
        if metadata is not None:
            playlist.clips.append(metadata)

    if song_count and len(playlist.clips) < song_count:
        # API may omit hidden/trashed clips; callers should not treat this as fatal.
        pass

    return playlist


def probe_mp3_bitrate_kbps(
    audio_url: str,
    client: httpx.Client | None = None,
    sample_bytes: int = 131_072,
) -> int | None:
    """Estimate MP3 bitrate by parsing the file header from a partial download."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as c:
            return probe_mp3_bitrate_kbps(audio_url, client=c, sample_bytes=sample_bytes)

    try:
        with client.stream("GET", audio_url, follow_redirects=True) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(32_768):
                data.extend(chunk)
                if len(data) >= sample_bytes:
                    break
        info = MP3(BytesIO(data))
        if info.info.bitrate:
            return round(info.info.bitrate / 1000)
    except Exception:
        return None
    return None


def sanitize_filename(name: str) -> str:
    """Make a filesystem-safe filename from a song title."""
    safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    return safe[:120] or "untitled"


def unique_filepath(path: Path) -> Path:
    """Return path, or the next available (2), (3), ... variant if it exists."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _format_created_date(created_at: str | None) -> str | None:
    if not created_at:
        return None
    try:
        normalized = created_at.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d")
    except ValueError:
        return created_at[:10] if len(created_at) >= 10 else created_at


def _guess_image_mime(url: str, content_type: str | None) -> str:
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime.startswith("image/"):
            return mime
    path = url.lower().split("?", 1)[0]
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _fetch_cover_image(
    image_url: str,
    client: httpx.Client | None = None,
) -> tuple[bytes, str] | None:
    try:
        if client is None:
            with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as c:
                return _fetch_cover_image(image_url, client=c)
        response = client.get(image_url, follow_redirects=True)
        response.raise_for_status()
        mime = _guess_image_mime(
            image_url,
            response.headers.get("content-type"),
        )
        return response.content, mime
    except Exception:
        return None


def apply_mp3_tags(
    path: Path,
    metadata: SongMetadata,
    *,
    client: httpx.Client | None = None,
) -> None:
    """Write ID3 tags for broad cross-platform player compatibility."""
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    tags = audio.tags
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.delall("TDRC")
    tags.delall("TYER")
    tags.delall("TCOM")
    tags.delall("TENC")
    tags.delall("COMM")
    tags.delall("TCON")
    tags.delall("TXXX")
    tags.delall("APIC")

    tags.add(TIT2(encoding=3, text=metadata.title))
    if metadata.artist:
        tags.add(TPE1(encoding=3, text=metadata.artist))

    created = _format_created_date(metadata.created_at)
    if created:
        tags.add(TYER(encoding=3, text=created[:4]))

    comment_lines: list[str] = []
    if metadata.styles:
        comment_lines.append(f"Styles: {metadata.styles}")
    comment_lines.extend(
        [
            f"Suno song: {metadata.page_url}",
            f"ID: {metadata.id}",
        ]
    )
    if metadata.model_name:
        comment_lines.append(f"Model: {metadata.model_name}")
    if metadata.model_version:
        comment_lines.append(f"Version: {metadata.model_version}")
    if metadata.created_at:
        comment_lines.append(f"Created: {metadata.created_at}")
    if metadata.duration_sec is not None:
        comment_lines.append(f"Duration: {metadata.duration_sec:.1f}s")
    if metadata.clip_start_sec is not None and metadata.clip_end_sec is not None:
        comment_lines.append(
            f"Clip: {format_timestamp(metadata.clip_start_sec)}–"
            f"{format_timestamp(metadata.clip_end_sec)}"
        )
    if metadata.prompt:
        comment_lines.append(f"Prompt: {metadata.prompt}")
    if metadata.playlist_name and metadata.playlist_url:
        comment_lines.append(
            f"Playlist: {metadata.playlist_name} ({metadata.playlist_url})"
        )

    comment_text = "\r\n".join(comment_lines)
    # WMP reads this COMM key for the Comments field; avoid multiple COMM frames.
    tags.add(
        COMM(
            encoding=1,
            lang="eng",
            desc="ID3v1 Comment",
            text=comment_text,
        )
    )

    if metadata.styles:
        tags.add(TXXX(encoding=3, desc="Styles", text=metadata.styles))
    if metadata.model_name:
        tags.add(TXXX(encoding=3, desc="Model", text=metadata.model_name))
    if metadata.model_version:
        tags.add(TXXX(encoding=3, desc="Version", text=metadata.model_version))
    if metadata.created_at:
        tags.add(TXXX(encoding=3, desc="Created", text=metadata.created_at))
    if metadata.playlist_name:
        tags.add(TXXX(encoding=3, desc="Playlist", text=metadata.playlist_name))

    if metadata.image_url:
        cover = _fetch_cover_image(metadata.image_url, client=client)
        if cover:
            data, mime = cover
            tags.add(
                APIC(
                    encoding=0,
                    mime=mime,
                    type=3,
                    desc="",
                    data=data,
                )
            )

    audio.save(v2_version=3, v1=0)


def download_file(
    url: str,
    dest: Path,
    client: httpx.Client,
    progress: ProgressCallback | None = None,
) -> Path:
    """Download a URL as fast as possible (single full GET)."""
    with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0)) or None
        downloaded = 0

        if progress:
            progress(downloaded, total)

        with dest.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)

    if progress and total is None:
        progress(downloaded, downloaded)

    return dest


def _streaming_request_headers(page_url: str) -> dict[str, str]:
    return {
        **DEFAULT_HEADERS,
        "Referer": page_url,
        "Accept": "audio/mpeg,audio/*;q=0.9,*/*;q=0.8",
    }


def _probe_content_length(url: str, client: httpx.Client, page_url: str) -> int:
    headers = _streaming_request_headers(page_url)
    response = client.head(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    total = int(response.headers.get("content-length", 0))
    if total > 0:
        return total

    probe_end = STREAMING_INITIAL_CHUNK_BYTES - 1
    response = client.get(
        url,
        headers={**headers, "Range": f"bytes=0-{probe_end}"},
        follow_redirects=True,
    )
    response.raise_for_status()
    content_range = response.headers.get("content-range", "")
    if "/" in content_range:
        return int(content_range.rsplit("/", 1)[-1])
    raise ValueError("Could not determine file size for streaming download")


def _fetch_byte_range(
    url: str,
    client: httpx.Client,
    page_url: str,
    start: int,
    end: int,
) -> bytes:
    headers = {
        **_streaming_request_headers(page_url),
        "Range": f"bytes={start}-{end}",
    }
    response = client.get(url, headers=headers, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _byte_rate(
    total_size: int,
    duration_sec: float | None,
    bitrate_kbps: int | None,
) -> float:
    if duration_sec and duration_sec > 0:
        return total_size / duration_sec
    kbps = bitrate_kbps or DEFAULT_MP3_BITRATE_KBPS
    return (kbps * 1000) / 8


def download_file_streaming(
    url: str,
    dest: Path,
    client: httpx.Client,
    *,
    duration_sec: float | None,
    page_url: str,
    bitrate_kbps: int | None = None,
    progress: ProgressCallback | None = None,
    initial_chunk_bytes: int = STREAMING_INITIAL_CHUNK_BYTES,
    chunk_bytes: int = STREAMING_CHUNK_BYTES,
    buffer_ahead_sec: float = STREAMING_BUFFER_AHEAD_SEC,
) -> Path:
    """
    Download using HTTP Range requests paced like HTML5 audio playback.

    Fetches a small initial burst, then requests sequential ranges only when
    the virtual playhead (elapsed time) allows, keeping a modest read-ahead buffer.
    """
    total_size = _probe_content_length(url, client, page_url)
    byte_rate = _byte_rate(total_size, duration_sec, bitrate_kbps)
    max_ahead_bytes = buffer_ahead_sec * byte_rate

    downloaded = 0
    start_time = time.monotonic()

    if progress:
        progress(downloaded, total_size)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        initial_end = min(initial_chunk_bytes, total_size) - 1
        chunk = _fetch_byte_range(url, client, page_url, 0, initial_end)
        handle.write(chunk)
        downloaded += len(chunk)
        if progress:
            progress(downloaded, total_size)

        while downloaded < total_size:
            elapsed = time.monotonic() - start_time
            playhead_byte = elapsed * byte_rate
            ahead = downloaded - playhead_byte

            if ahead >= max_ahead_bytes:
                time.sleep(0.1)
                continue

            range_start = downloaded
            range_end = min(downloaded + chunk_bytes, total_size) - 1
            chunk = _fetch_byte_range(url, client, page_url, range_start, range_end)
            handle.write(chunk)
            downloaded += len(chunk)
            if progress:
                progress(downloaded, total_size)

    return dest


def download_song_mp3_from_metadata(
    metadata: SongMetadata,
    output_dir: Path,
    client: httpx.Client,
    *,
    progress: ProgressCallback | None = None,
    overwrite: bool = False,
    download_mode: DownloadMode = "fast",
    clip: ClipRange | None = None,
    attach_metadata: bool = True,
) -> tuple[Path, SongMetadata]:
    """Download and optionally tag an MP3 using pre-fetched metadata."""
    if clip is not None and download_mode == "streaming":
        raise ValueError(
            "Clip download cannot be used with streaming mode. Use fast download."
        )

    if metadata.bitrate_kbps is None:
        metadata.bitrate_kbps = probe_mp3_bitrate_kbps(metadata.audio_url, client=client)

    output_dir.mkdir(parents=True, exist_ok=True)

    resolved_clip: tuple[float, float] | None = None
    if clip is not None:
        if clip.end_sec is not None or metadata.duration_sec is not None:
            resolved_clip = finalize_clip_range(
                clip,
                duration_sec=metadata.duration_sec,
            )

    if clip is not None:
        if resolved_clip:
            start_sec, end_sec = resolved_clip
            clip_label = (
                f"{format_timestamp_filename(start_sec)}-"
                f"{format_timestamp_filename(end_sec)}"
            )
        else:
            clip_label = f"{format_timestamp_filename(clip.start_sec)}-end"
        mp3_path = output_dir / (
            f"{sanitize_filename(metadata.title)} (clip {clip_label}).mp3"
        )
    else:
        mp3_path = output_dir / f"{sanitize_filename(metadata.title)}.mp3"

    if not overwrite:
        mp3_path = unique_filepath(mp3_path)

    download_dest = mp3_path
    temp_path: Path | None = None
    if clip is not None:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{sanitize_filename(metadata.title)}.",
            suffix=".full.tmp.mp3",
            dir=output_dir,
        )
        os.close(fd)
        temp_path = Path(temp_name)
        download_dest = temp_path

    if download_mode == "streaming":
        download_file_streaming(
            metadata.audio_url,
            download_dest,
            client,
            duration_sec=metadata.duration_sec,
            page_url=metadata.page_url,
            bitrate_kbps=metadata.bitrate_kbps,
            progress=progress,
        )
    else:
        download_file(metadata.audio_url, download_dest, client, progress=progress)

    if clip is not None:
        if resolved_clip is None:
            file_duration = MP3(download_dest).info.length
            resolved_clip = finalize_clip_range(
                clip,
                duration_sec=file_duration,
            )
            start_sec, end_sec = resolved_clip
            clip_label = (
                f"{format_timestamp_filename(start_sec)}-"
                f"{format_timestamp_filename(end_sec)}"
            )
            final_mp3_path = output_dir / (
                f"{sanitize_filename(metadata.title)} (clip {clip_label}).mp3"
            )
            if final_mp3_path != mp3_path:
                mp3_path = final_mp3_path
                if not overwrite:
                    mp3_path = unique_filepath(mp3_path)
        start_sec, end_sec = resolved_clip
        trim_mp3_clip(download_dest, mp3_path, start_sec, end_sec)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        metadata.clip_start_sec = start_sec
        metadata.clip_end_sec = end_sec
        try:
            metadata.duration_sec = MP3(mp3_path).info.length
        except Exception:
            metadata.duration_sec = end_sec - start_sec

    if attach_metadata:
        apply_mp3_tags(mp3_path, metadata, client=client)
    if metadata.bitrate_kbps is None:
        try:
            metadata.bitrate_kbps = round(MP3(mp3_path).info.bitrate / 1000)
        except Exception:
            pass
    return mp3_path, metadata


def download_song_mp3(
    song_id_or_url: str,
    output_dir: Path,
    *,
    progress: ProgressCallback | None = None,
    overwrite: bool = False,
    download_mode: DownloadMode = "fast",
    clip: ClipRange | None = None,
    attach_metadata: bool = True,
) -> tuple[Path, SongMetadata]:
    """Download a Suno song as MP3 from Suno's CDN and optionally tag the file."""
    song_id = parse_song_id(song_id_or_url)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=120) as client:
        metadata = fetch_song_metadata(
            song_id,
            client=client,
            full=attach_metadata,
        )
        return download_song_mp3_from_metadata(
            metadata,
            output_dir,
            client,
            progress=progress,
            overwrite=overwrite,
            download_mode=download_mode,
            clip=clip,
            attach_metadata=attach_metadata,
        )


def download_playlist_mp3(
    playlist_id_or_url: str,
    output_dir: Path,
    *,
    use_subfolder: bool = True,
    progress: ProgressCallback | None = None,
    on_track: TrackProgressCallback | None = None,
    overwrite: bool = False,
    download_mode: DownloadMode = "fast",
    attach_metadata: bool = True,
) -> tuple[list[Path], list[tuple[SongMetadata, str]], PlaylistMetadata]:
    """
    Download all songs from a public Suno playlist.

    Returns (successful paths, failures as (metadata, error) pairs, playlist info).
    """
    playlist_id = parse_playlist_id(playlist_id_or_url)
    paths: list[Path] = []
    failures: list[tuple[SongMetadata, str]] = []

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=120) as client:
        playlist = fetch_playlist_metadata(playlist_id, client=client)
        if not playlist.clips:
            raise ValueError(f"No downloadable songs found in playlist: {playlist.name}")

        target_dir = output_dir
        if use_subfolder:
            target_dir = output_dir / sanitize_filename(playlist.name)

        total = len(playlist.clips)
        for index, metadata in enumerate(playlist.clips, start=1):
            if on_track:
                on_track(index, total, metadata.title)

            track_progress: ProgressCallback | None = None
            if progress:
                base_pct = (index - 1) * 100

                def track_progress(done: int, total_bytes: int | None, _base=base_pct) -> None:
                    if total_bytes and total_bytes > 0:
                        track_pct = min(99, done * 100 // total_bytes)
                    else:
                        track_pct = 0 if done == 0 else 50
                    overall = min(100, (_base + track_pct) // total)
                    progress(overall, 100)

            try:
                path, _ = download_song_mp3_from_metadata(
                    metadata,
                    target_dir,
                    client,
                    progress=track_progress,
                    overwrite=overwrite,
                    download_mode=download_mode,
                    attach_metadata=attach_metadata,
                )
                paths.append(path)
            except Exception as exc:
                failures.append((metadata, str(exc)))

        if progress:
            progress(100, 100)

    return paths, failures, playlist


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a Suno song as MP3 from CDN"
    )
    parser.add_argument("song", help="Suno song URL or UUID")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        help="Output directory (default: downloads)",
    )
    parser.add_argument(
        "--start",
        metavar="TIME",
        help="Clip start time (seconds or MM:SS). Blank start means 0.",
    )
    parser.add_argument(
        "--end",
        metavar="TIME",
        help="Clip end time (seconds or MM:SS). Blank end means end of song.",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Download audio only; skip fetching Suno metadata and writing ID3 tags",
    )
    args = parser.parse_args()

    clip = clip_from_optional_strings(args.start, args.end)

    def on_progress(done: int, total: int | None) -> None:
        if total:
            pct = done * 100 // total
            print(f"\rDownloading... {pct}%", end="", flush=True)
        else:
            print(f"\rDownloading... {done:,} bytes", end="", flush=True)

    path, _metadata = download_song_mp3(
        args.song,
        args.output,
        progress=on_progress,
        clip=clip,
        attach_metadata=not args.no_metadata,
    )
    print(f"\nDone: {path}")


if __name__ == "__main__":
    main()
