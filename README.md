# suno-downloader

From Suno, download single songs and playlists, or bulk download songs from your library or other user's public songs/hooks. Alternatively, use the download scripts to grab songs and playlists programmatically. Only mp3 format can be downloaded for songs (see "Why Only MP3?" note below). Hooks can be downloaded as MP4, MP3, or both.

<p align="center">
  <img src="https://github.com/user-attachments/assets/4f64bcec-9413-4a22-8f23-33d557d707e6" alt="Suno MP3 Downloader GUI" height="300" />
  &nbsp;
  <img src="https://github.com/user-attachments/assets/ec7fee21-d7f3-4fd2-9c9e-49e589b7f3e4" alt="Suno Bulk Downloader Tab" height="300" />
</p>

## Downloading Songs/Playlists via UI

1. Clone or download the code from this repository.

1. Open the UI:
    - **Windows:** `run_gui.bat`
    - **macOS / Linux:** `./run_gui.sh` (run `chmod +x run_gui.sh` once)

    The launcher installs `uv` and sets up the project environment if needed, then opens the app. You can also run it manually by installing `uv` then:

    ```bash
    uv sync
    uv run suno_download_gui.py
    ```

1. Once the GUI is open: paste a Suno song, playlist, or hook URL, or a 36-character ID. The GUI supports single songs and public playlists, optional clip times for songs, background downloads with progress, a persistent download folder, playlist subfolders, dark mode, and ID3 metadata tagging.

## Bulk Download Public Songs/Hooks

Use the **Bulk** tab to load a user's published songs and hooks, filter the list, select items, and download in batch.

1. Enter a Suno username (without `@`) and click **Load profile** (enable Songs and/or Hooks).
2. Filter by title, date range, or sort by date/views/likes.
3. Use **Select all filtered** or check individual rows.
4. Click **Download selected** (uses the download folder from the Download tab).

Songs save to `{username} - Songs/` and hooks to `{username} - Hooks/` when playlist subfolders are enabled in Options.

## Bulk Download Private Library (needs bearer token for authentication)

To download your own **unpublished/private** songs, add a bearer token in the Bulk Download UI tab. To get a bearer token:

1. Open **Chrome** or **Edge** and go to [suno.com](https://suno.com)
2. Make sure you are logged in
3. Open Developer Tools: **F12**
4. Go to the **Network** tab
5. Reload the page: **F5**
6. Type `feed` in the filter field
7. Click on the `v3` POST request
8. Under **Request Headers**, find the `Authorization` entry — it reads: `Bearer ey...`
9. Copy **only the part after `Bearer `** — the long `ey...` string

⚠️ Tokens expire after a few hours. If the script reports a 401 error, simply get a fresh token and restart.

🔒 Your token is never stored or transmitted anywhere other than directly to Suno's own API.


Paste the token into Options, click **Test token**, then use **Load my library** on the Bulk tab.

> Tokens expire after a few hours. If you get a 401 error, get a fresh token.

> Your token is like a password. The app stores it locally on your computer only.

Click the **?** button next to the token field in Options for the full guide (same text as [docs/bearer_token.md](docs/bearer_token.md)).

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

User profile bulk loading uses Suno's public profile API for published songs and scrapes the hooks tab HTML for hooks. Private library loading uses your bearer token with the internal `feed/v3` API.

**Why Only MP3 for songs?** That is the only format Suno exposes on the public CDN — the same file you get when you press Play in the browser. Hooks can also be saved as MP4 video. There is no WAV or other lossless source to download (outside of the paid API which is monthly quota limited), and no higher-bitrate encode to request. Tracks are typically around 190 kbps MP3 (varies by track); saving them as-is gives you the best quality Suno actually serves for playback.



## Issue Reporting

The Suno API could change in the future which may break one or more features of this tool. This is not my main Github account / email, so I'll be more responsive if you tag me on X (@piggybackcap).