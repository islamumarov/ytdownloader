# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick start

```sh
uv sync                          # install deps into .venv
export BOT_TOKEN=123:ABC...      # from @BotFather; or `source .env`
uv run bot.py                    # poll Telegram
```

## External requirements

Bot requires **runtime binaries**, not pip packages:
- `yt-dlp` binary in repo root. Download from https://github.com/yt-dlp/yt-dlp/releases, `chmod +x yt-dlp`.
- `ffmpeg` on `PATH` (`brew install ffmpeg`).
- `.env` with `BOT_TOKEN` (get from [@BotFather](https://t.me/BotFather)).
- `cookies.txt` (optional, Netscape format) — YouTube may reject requests without it, but `-f sb0` usually works.

## Architecture

Telegram message → extract YouTube URL → resolve format → download → handle output → send reply.

### Format resolution (`resolve_format`)

User sends YT link. Bot needs to decide what to download (`-f` selector for yt-dlp).

- **`FORMAT="medium"` (default):** List available formats (`yt-dlp -J`), pick median of the distinct video heights.
  - Examples: 144/240/360/480/720/1080p → 480p; 360/720/1080p → 720p.
  - Rationale: yields reasonable quality without massive files; adaptive to each video's encoding ladder.
  - Implemented in `pick_medium_height()` (filters audio-only, handles deduplication).

- **`FORMAT="best"` or any other `-f` selector:** Pass directly to yt-dlp.

- **`FORMAT="sb0"` (opt-in):** Storyboard (thumbnail slideshow). See **Storyboard conversion** below.

Resolved selector + summary → `download()`.

### Download (`download`)

Run `yt-dlp -f <selector> --write-info-json --merge-output-format mp4 <url>`. Returns:
- Media file (`.mhtml` for sb0; `.mp4` for real video).
- `.info.json` (format metadata, used by storyboard splitter).

### Output handling

**Real video (`.mp4`):** Send as-is to Telegram.

**Storyboard (`.mhtml`):** 
1. Extract sprite-sheet images from MIME archive (`extract_images()`).
2. Determine tile grid from `.info.json` format metadata (`tile_size()`). If missing, montage fallback: each whole sprite-sheet = one frame.
3. Split each sprite-sheet into its grid of thumbnail tiles, padding-trimming the final sheet (`build_frames()`).
4. Assemble frames into mp4 with ffmpeg (`frames_to_mp4()`).
5. Send result.

Storyboards save as .mhtml because:
- yt-dlp embeds each sprite-sheet *whole* inside the MIME archive, one image per fragment (Content-ID).
- HTML index (`<figure><img src="cid:...">`) not useful — no tile geometry there.
- Geometry lives in `.info.json` format metadata (width/height or columns/rows).
- ffmpeg can't decode .mhtml; manual extraction + frame splitting required.

Blank-tile detection uses ImageStat stddev threshold (15.0) to trim padding on the final sheet.

## Configuration

Environment variables (or `.env`):

| Var | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — (required) | Telegram bot token |
| `YTDLP_FORMAT` | `medium` | `medium` (auto-pick), or any yt-dlp `-f` (e.g. `best`, `sb0`) |
| `YTDLP_PATH` | `./yt-dlp` | Path to yt-dlp binary |
| `COOKIES_PATH` | `cookies.txt` | Cookies file; skipped if missing |
| `VIDEOS_DIR` | `videos` | Download directory (created if missing) |
| `STORYBOARD_FPS` | `4` | Slideshow frame rate |

## Testing

Offline test of storyboard pipeline (no network/bot/yt-dlp binary required):

```sh
.venv/bin/python /path/to/test_convert.py
```

Builds synthetic MHTML + .info.json, exercises extract/split/assemble, verifies h264 mp4 output. Both tiled (with metadata) and montage (fallback) modes tested.

## Key gotchas

**`run()` vs `run_out()`:** Former merges stderr into stdout (good for user-facing errors). Latter keeps them separate (essential for `-J` JSON output, which would be corrupted by stderr).

**Median, not mean:** For even-count format lists, taking the upper-middle (index `len//2`) biases to better quality, not lower.

**Blank-tile trimming:** Only trailing blanks are trimmed (after all frames extracted); interior blanks left intact (might be intentional pauses in the storyboard).

**50 MB limit:** Telegram bots cap uploads at 50 MB. Storyboards fit easily; full 1080p videos may exceed.

## Relevant code sections

| Task | Location |
|---|---|
| Format picking | `pick_medium_height()` |
| Format resolution | `resolve_format()` |
| Download | `download()` |
| Extract MIME | `extract_images()` |
| Detect grid | `tile_size()` |
| Split frames | `build_frames()` |
| Assemble | `frames_to_mp4()` |
| Message handler | `handle()` |
| URL regex | `YT_RE` |
