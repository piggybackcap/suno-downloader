"""Download a Suno song as MP3 from CDN (same source the web player uses)."""

from __future__ import annotations

from io import BytesIO
import argparse
import os
import re
import shutil
import subprocess
import time
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from mutagen.id3 import COMM, TCOM, TDRC, TENC, TIT2, TPE1, TXXX, ID3
from mutagen.mp3 import MP3

CDN_MP3 = "https://cdn1.suno.ai/{song_id}.mp3"
OEMBED_API = "https://studio-api-prod.suno.com/api/oembed"
PLAYLIST_API = "https://studio-api-prod.suno.com/api/playlist/{playlist_id}"
SONG_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?suno\.com/song/([0-9a-f-]{36})",
    re.IGNORECASE,
)
PLAYLIST_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?suno\.com/playlist/([0-9a-f-]{36})",
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

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

ProgressCallback = Callable[[int, int | None], None]
TrackProgressCallback = Callable[[int, int, str], None]

SunoInputKind = Literal["song", "playlist"]


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
    clip_start_sec: float | None = None
    clip_end_sec: float | None = None


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


def parse_suno_input(value: str) -> tuple[SunoInputKind, str]:
    """Detect whether input is a song or playlist URL/ID."""
    stripped = value.strip()
    if PLAYLIST_URL_PATTERN.search(stripped):
        return "playlist", parse_playlist_id(stripped)
    if SONG_URL_PATTERN.search(stripped):
        return "song", parse_song_id(stripped)
    if UUID_PATTERN.match(stripped):
        return "song", stripped
    raise ValueError(
        "Could not parse Suno URL or ID. Expected a song or playlist URL, "
        "or a 36-character UUID."
    )


def _unescape_json_string(value: str) -> str:
    return bytes(value, "utf-8").decode("unicode_escape")


def _extract_field(window: str, key: str) -> str | None:
    if key == "duration":
        match = re.search(r'\\"duration\\":([0-9.]+)', window)
        return match.group(1) if match else None

    match = re.search(rf'\\"{key}\\":\\"((?:\\\\.|[^"\\])*)\\"', window)
    return _unescape_json_string(match.group(1)) if match else None


def fetch_song_metadata(song_id: str, client: httpx.Client | None = None) -> SongMetadata:
    """Fetch song metadata from oEmbed and the public song page."""
    if client is None:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=30) as c:
            return fetch_song_metadata(song_id, client=c)

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
    except httpx.HTTPError:
        pass

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


def _format_created_date(created_at: str | None) -> str | None:
    if not created_at:
        return None
    try:
        normalized = created_at.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d")
    except ValueError:
        return created_at[:10] if len(created_at) >= 10 else created_at


def apply_mp3_tags(path: Path, metadata: SongMetadata) -> None:
    """Write ID3 tags for broad cross-platform player compatibility."""
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    tags = audio.tags
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.delall("TDRC")
    tags.delall("TCOM")
    tags.delall("TENC")
    tags.delall("COMM")
    tags.delall("TCON")
    tags.delall("TXXX")

    tags.add(TIT2(encoding=3, text=metadata.title))
    if metadata.artist:
        tags.add(TPE1(encoding=3, text=metadata.artist))

    created = _format_created_date(metadata.created_at)
    if created:
        tags.add(TDRC(encoding=3, text=created))

    if metadata.model_name:
        tags.add(TCOM(encoding=3, text=metadata.model_name))
    if metadata.model_version:
        tags.add(TENC(encoding=3, text=metadata.model_version))

    comment_lines = [
        f"Suno song: {metadata.page_url}",
        f"ID: {metadata.id}",
    ]
    if metadata.model_version:
        comment_lines.append(f"Model: {metadata.model_version}")
    if metadata.model_name:
        comment_lines.append(f"Engine: {metadata.model_name}")
    if metadata.created_at:
        comment_lines.append(f"Created: {metadata.created_at}")
    if metadata.duration_sec is not None:
        comment_lines.append(f"Duration: {metadata.duration_sec:.1f}s")
    if metadata.clip_start_sec is not None and metadata.clip_end_sec is not None:
        comment_lines.append(
            f"Clip: {format_timestamp(metadata.clip_start_sec)}–"
            f"{format_timestamp(metadata.clip_end_sec)}"
        )
    if metadata.styles:
        comment_lines.append(f"Styles: {metadata.styles}")
    if metadata.prompt:
        comment_lines.append(f"Prompt: {metadata.prompt}")
    if metadata.playlist_name and metadata.playlist_url:
        comment_lines.append(
            f"Playlist: {metadata.playlist_name} ({metadata.playlist_url})"
        )

    # Empty desc so players show the comment body (not just a label).
    tags.add(
        COMM(
            encoding=3,
            lang="eng",
            desc="",
            text="\n".join(comment_lines),
        )
    )

    if metadata.styles:
        tags.add(TXXX(encoding=3, desc="Styles", text=metadata.styles))
    if metadata.model_version:
        tags.add(TXXX(encoding=3, desc="Model", text=metadata.model_version))
    if metadata.created_at:
        tags.add(TXXX(encoding=3, desc="Created", text=metadata.created_at))
    if metadata.playlist_name:
        tags.add(TXXX(encoding=3, desc="Playlist", text=metadata.playlist_name))

    audio.save()


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
) -> tuple[Path, SongMetadata]:
    """Download and tag an MP3 using pre-fetched metadata (no extra API calls)."""
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

    if mp3_path.exists() and not overwrite:
        apply_mp3_tags(mp3_path, metadata)
        if metadata.bitrate_kbps is None:
            try:
                metadata.bitrate_kbps = round(MP3(mp3_path).info.bitrate / 1000)
            except Exception:
                pass
        return mp3_path, metadata

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

    apply_mp3_tags(mp3_path, metadata)
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
) -> tuple[Path, SongMetadata]:
    """Download a Suno song as MP3 from Suno's CDN and tag the file."""
    song_id = parse_song_id(song_id_or_url)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=120) as client:
        metadata = fetch_song_metadata(song_id, client=client)
        return download_song_mp3_from_metadata(
            metadata,
            output_dir,
            client,
            progress=progress,
            overwrite=overwrite,
            download_mode=download_mode,
            clip=clip,
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
    )
    print(f"\nDone: {path}")


if __name__ == "__main__":
    main()
