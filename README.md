# suno-downloader

Download single songs and playlists from Suno via UI. Alternatively, use the download scripts to grab songs and playlists programmatically. Only mp3 format can be downloaded (see "Why Only MP3?" note below).

![Suno MP3 Downloader GUI](https://github.com/user-attachments/assets/4f64bcec-9413-4a22-8f23-33d557d707e6)

## Downloading Songs/Playlists via UI

1. Clone or download the code from this repository.

1. Open the UI:
    - **Windows:** `run_gui.bat`
    - **macOS / Linux:** `./run_gui.sh` (run `chmod +x run_gui.sh` once)

    The launcher installs `uv` and sets up the project environment if needed, then opens the app. You can also run it manually:

    ```bash
    uv sync
    uv run suno_download_gui.py
    ```

1. Once the GUI is open: paste a Suno song or playlist URL, or a 36-character ID. The GUI supports single songs and public playlists, optional clip times for songs, background downloads with progress, a persistent download folder, playlist subfolders, dark mode, and ID3 metadata tagging.

## Command Line Interface

For scripting, use the download scripts in the project root:

```bash
# Single song (fast)
uv run python download_song_mp3.py <url-or-id> [-o downloads] [--start 1:30] [--end 2:45] [--no-metadata]

# Playlist
uv run python download_playlist_mp3.py <playlist-url-or-id> [-o downloads] [--flat] [--no-metadata]
```

Use `--no-metadata` to skip writing ID3 tags (cover art, styles, comments, etc.). Song titles are still fetched for filenames. Metadata tagging is enabled by default.

Playlist downloads save into a subfolder named after the playlist by default. Use `--flat` to write files directly into the output folder. Clip times accept seconds or `MM:SS`; clipping uses ffmpeg (bundled after `uv sync`).

## How It Works (Currently)

Suno's web player streams MP3 via CDN (no auth). This tool fetches metadata from the public song page and oEmbed API (songs) or the public playlist API (playlists), downloads that CDN file, and writes ID3 tags (title, artist, date, cover art, model, styles, prompt, playlist context, etc.).

**Why Only MP3?** That is the only format Suno exposes on the public CDN — the same file you get when you press Play in the browser. There is no WAV or other lossless source to download (outside of the paid API which is monthly quota limited), and no higher-bitrate encode to request. Tracks are typically around 190 kbps MP3 (varies by track); saving them as-is gives you the best quality Suno actually serves for playback.