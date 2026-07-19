# ytdownloader

Telegram bot: send a YouTube link, get back an mp4.

By default it lists the available formats (`yt-dlp -J`) and downloads the **medium** one — the
median of the distinct video heights the video actually offers — merged to mp4 and sent back.
So a video with 144/240/360/480/720/1080p yields 480p; one with only 360/720/1080p yields 720p.

Override with `YTDLP_FORMAT`:

- `best` — top quality.
- `sb0` — the **storyboard**: the strip of tiny scrubber-preview thumbnails, saved as a
  `.mhtml` archive. ffmpeg can't read `.mhtml`, so the bot pulls the sprite-sheets out of the
  archive, splits each into its grid of thumbnails (geometry read from the download's
  `.info.json`), and assembles them into a low-res slideshow mp4.
- any other yt-dlp `-f` selector.

## Requirements

- Python ≥ 3.14, [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on `PATH` (`brew install ffmpeg`)
- The `yt-dlp` binary in the repo root — [releases](https://github.com/yt-dlp/yt-dlp/releases):
  ```sh
  curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o yt-dlp
  chmod +x yt-dlp
  ```
- `cookies.txt` in Netscape format (export from a browser extension). Optional — the bot runs
  without it, but YouTube may reject requests. Storyboards usually work without cookies.
  **Cookies rotate** — if the bot reports "cookies are no longer valid", re-export fresh ones.
- A JavaScript runtime for yt-dlp's n-challenge (needed to expose real video formats):
  `brew install deno` (or `apt install deno`). Without it YouTube often serves **only
  storyboards**, so `YTDLP_FORMAT=medium` will fall back to a storyboard.
- A bot token from [@BotFather](https://t.me/BotFather).

## Setup

```sh
uv sync                       # install deps into .venv
export BOT_TOKEN=123:ABC...   # from @BotFather
uv run bot.py
```

Then DM your bot a YouTube link.

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `BOT_TOKEN` | — (required) | Telegram bot token |
| `YTDLP_PATH` | `./yt-dlp` | Path to the yt-dlp binary |
| `COOKIES_PATH` | `cookies.txt` | Cookies file; skipped if missing |
| `YTDLP_FORMAT` | `medium` | `medium` (list formats, pick median height), or any yt-dlp `-f` value (`best`, `sb0`, …) |
| `VIDEOS_DIR` | `videos` | Download directory |
| `STORYBOARD_FPS` | `4` | Slideshow frame rate |

## Notes

- Telegram bots cap uploads at 50 MB. Storyboards are tiny; a full video may exceed this.
- If the grid can't be read from metadata, each whole sprite-sheet becomes one frame
  (a montage slideshow) instead of failing.
