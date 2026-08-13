"""CLI for downloading all songs from a public Suno playlist."""

from __future__ import annotations

import argparse
from pathlib import Path

from download_song_mp3 import download_playlist_mp3


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all songs from a public Suno playlist as MP3 from CDN"
    )
    parser.add_argument("playlist", help="Suno playlist URL or UUID")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        help="Output directory (default: downloads)",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Save MP3 files directly in the output directory instead of a playlist subfolder",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download even if output files already exist",
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Use browser-style paced downloads (slow; not recommended for playlists)",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Download audio only; skip fetching Suno metadata and writing ID3 tags",
    )
    args = parser.parse_args()

    use_subfolder = not args.flat

    def on_track(current: int, total: int, title: str) -> None:
        print(f"[{current}/{total}] {title}")

    def on_progress(done: int, total: int | None) -> None:
        if total:
            pct = done * 100 // total
            print(f"\rOverall progress: {pct}%", end="", flush=True)

    download_mode = "streaming" if args.streaming else "fast"
    paths, failures, playlist = download_playlist_mp3(
        args.playlist,
        args.output,
        use_subfolder=use_subfolder,
        progress=on_progress,
        on_track=on_track,
        overwrite=args.overwrite,
        download_mode=download_mode,
        attach_metadata=not args.no_metadata,
    )

    print()
    print(f"Playlist: {playlist.name} ({len(paths)} downloaded, {len(failures)} failed)")
    for path in paths:
        print(f"  {path}")
    for metadata, error in failures:
        print(f"  FAILED {metadata.title}: {error}")


if __name__ == "__main__":
    main()
