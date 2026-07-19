#!/usr/bin/env python3
"""Telegram bot that downloads a YouTube video with yt-dlp and returns an mp4.

Flow:
  1. User sends a YouTube link.
  2. resolve_format() decides what to grab:
       - FORMAT="medium" (default): list formats with `yt-dlp -J`, pick the median of the
         distinct video heights the video actually offers, and download that tier merged to
         mp4. If the video exposes no real video formats (e.g. YouTube served storyboards
         only), fall back to the `sb0` storyboard.
       - any other FORMAT (e.g. "best", "sb0"): passed straight to yt-dlp.
  3. download() runs `yt-dlp -f <selector> --write-info-json --merge-output-format mp4 ...`.
  4. Output handling:
       - real video (.mp4): sent as-is.
       - storyboard (.mhtml): a MIME archive of sprite-sheets. ffmpeg can't read it, so we
         extract the sheets, split each into its grid of thumbnails (grid geometry read from
         the .info.json — the .mhtml carries none), and assemble the frames into an mp4.
         If the grid can't be determined, each whole sheet becomes one frame.

Config via environment variables:
  BOT_TOKEN        (required) Telegram bot token from @BotFather
  YTDLP_PATH       yt-dlp binary                (default: ./yt-dlp)
  COOKIES_PATH     cookies file                 (default: cookies.txt; skipped if missing)
  YTDLP_FORMAT     "medium" or any yt-dlp -f     (default: medium)
  VIDEOS_DIR       download dir                 (default: videos)
  STORYBOARD_FPS   frames/sec for storyboards   (default: 4)
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
from email import message_from_bytes
from pathlib import Path

from PIL import Image, ImageStat
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

YTDLP = os.environ.get("YTDLP_PATH", "./yt-dlp")
COOKIES = os.environ.get("COOKIES_PATH", "cookies.txt")
FORMAT = os.environ.get("YTDLP_FORMAT", "medium")
VIDEOS_DIR = Path(os.environ.get("VIDEOS_DIR", "videos"))
FPS = float(os.environ.get("STORYBOARD_FPS", "4"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("ytbot")

YT_RE = re.compile(
    r"https?://(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?\S*?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


# --------------------------------------------------------------------------- #
# subprocess helpers                                                          #
# --------------------------------------------------------------------------- #
async def run(*cmd: str) -> tuple[int, str]:
    """Run a command, merging stderr into stdout (good for user-facing error text)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def run_out(*cmd: str) -> tuple[int, str, str]:
    """Run a command, keeping stdout and stderr separate.

    Essential for `yt-dlp -J`: yt-dlp prints warnings/progress to stderr and the JSON to
    stdout, so merging the streams would corrupt the JSON.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _tail(text: str, n: int = 1500) -> str:
    return text[-n:]


_cookies_warned = False


def cookie_args() -> list[str]:
    if Path(COOKIES).exists():
        return ["--cookies", COOKIES]
    global _cookies_warned
    if not _cookies_warned:
        log.warning("cookies file %r missing; continuing without it", COOKIES)
        _cookies_warned = True
    return []


def ensure_ytdlp() -> None:
    if shutil.which(YTDLP) is None and not Path(YTDLP).exists():
        raise RuntimeError(
            f"yt-dlp not found at {YTDLP!r}. Put the binary in the repo "
            "(https://github.com/yt-dlp/yt-dlp/releases) and `chmod +x yt-dlp`, "
            "or set YTDLP_PATH."
        )


# --------------------------------------------------------------------------- #
# format resolution                                                           #
# --------------------------------------------------------------------------- #
def _video_heights(formats: list[dict]) -> list[int]:
    """Distinct, sorted heights of real (non-storyboard, non-audio) video formats."""
    return sorted(
        {
            int(f["height"])
            for f in formats
            if f.get("height") and f.get("vcodec") not in (None, "none")
        }
    )


def pick_medium_height(formats: list[dict]) -> int | None:
    """Median distinct video height; upper-middle on ties (biases to better quality).

    e.g. 144/240/360/480/720/1080 -> 480; 360/720/1080 -> 720. None if no video formats.
    """
    heights = _video_heights(formats)
    return heights[len(heights) // 2] if heights else None


def select_medium(formats: list[dict]) -> tuple[str, str]:
    """(yt-dlp -f selector, human summary) for the medium tier, or a storyboard fallback."""
    heights = _video_heights(formats)
    if not heights:
        return "sb0", "sb0 (no video formats offered — storyboard only)"
    h = heights[len(heights) // 2]
    selector = f"bv*[height<={h}]+ba/b[height<={h}]/b"
    return selector, f"medium {h}p (of {'/'.join(map(str, heights))})"


async def resolve_format(url: str) -> tuple[str, str]:
    """Resolve FORMAT into a concrete (selector, summary)."""
    if FORMAT != "medium":
        return FORMAT, FORMAT
    code, out, err = await run_out(YTDLP, "-J", "--no-playlist", *cookie_args(), url)
    if code != 0:
        raise RuntimeError(f"yt-dlp -J failed (exit {code}):\n{_tail(err)}")
    try:
        formats = json.loads(out).get("formats", [])
    except ValueError as exc:
        raise RuntimeError(f"could not parse yt-dlp JSON: {exc}")
    return select_medium(formats)


# --------------------------------------------------------------------------- #
# download                                                                    #
# --------------------------------------------------------------------------- #
async def download(url: str, vid: str, selector: str) -> tuple[Path, Path | None]:
    """Run yt-dlp; return (media_file, info_json | None).

    media_file is a real .mp4, or the .mhtml for storyboard selectors.
    """
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(VIDEOS_DIR / "%(id)s.%(ext)s")
    code, out = await run(
        YTDLP, "-f", selector,
        "--no-playlist", "--write-info-json", "--merge-output-format", "mp4",
        "-o", out_tmpl, *cookie_args(), url,
    )
    if code != 0:
        raise RuntimeError(f"yt-dlp failed (exit {code}):\n{_tail(out)}")

    info = VIDEOS_DIR / f"{vid}.info.json"
    info = info if info.exists() else None
    media = [
        p
        for p in sorted(VIDEOS_DIR.glob(f"{vid}.*"), key=lambda p: p.stat().st_mtime)
        if p.suffix != ".json"
    ]
    if not media:
        raise RuntimeError(f"yt-dlp produced no media file:\n{_tail(out)}")
    return media[-1], info


# --------------------------------------------------------------------------- #
# storyboard (.mhtml) -> frames                                               #
# --------------------------------------------------------------------------- #
def extract_images(mhtml: Path) -> list[bytes]:
    """Pull the sprite-sheet images out of an MHTML archive, in document (time) order."""
    msg = message_from_bytes(mhtml.read_bytes())
    images: list[bytes] = []
    for part in msg.walk():
        if part.get_content_type().startswith("image/"):
            payload = part.get_payload(decode=True)
            if payload:
                images.append(payload)
    return images


def tile_size(info: Path | None, fmt: str, sheet_size: tuple[int, int]) -> tuple[int, int] | None:
    """Thumbnail (width, height) in a sprite-sheet, from yt-dlp's format metadata.

    Returns None when the grid can't be determined, in which case each whole sprite-sheet
    is treated as a single frame.
    """
    if info is None:
        return None
    try:
        formats = json.loads(info.read_text()).get("formats", [])
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", info, exc)
        return None
    meta = next((f for f in formats if f.get("format_id") == fmt), None)
    if not meta:
        return None
    tw, th = meta.get("width"), meta.get("height")
    if tw and th:
        return int(tw), int(th)
    cols, rows = meta.get("columns"), meta.get("rows")
    if cols and rows:
        sw, sh = sheet_size
        return sw // int(cols), sh // int(rows)
    return None


def _is_blank(im: Image.Image) -> bool:
    # storyboard padding tiles are flat (near-zero stddev); real thumbnails are well above
    # this even for dark scenes. JPEG bleed on flat tiles stays under ~5, so 15 is a safe cut.
    return sum(ImageStat.Stat(im).stddev) < 15.0


def build_frames(mhtml: Path, workdir: Path, info: Path | None, fmt: str) -> int:
    """Extract every thumbnail frame from the storyboard into workdir as PNGs."""
    images = extract_images(mhtml)
    if not images:
        raise RuntimeError("no images found in storyboard archive")

    first = Image.open(io.BytesIO(images[0]))
    tsize = tile_size(info, fmt, first.size)

    frames: list[Image.Image] = []
    for data in images:
        sheet = Image.open(io.BytesIO(data)).convert("RGB")
        if tsize is None:
            frames.append(sheet)  # unknown grid: whole sheet is one frame
            continue
        tw, th = tsize
        cols, rows = max(sheet.width // tw, 1), max(sheet.height // th, 1)
        for r in range(rows):
            for c in range(cols):
                frames.append(sheet.crop((c * tw, r * th, c * tw + tw, r * th + th)))

    # the final sheet is padded to a full grid; drop those trailing blank tiles
    if tsize is not None:
        while len(frames) > 1 and _is_blank(frames[-1]):
            frames.pop()

    for i, frame in enumerate(frames):
        frame.save(workdir / f"frame_{i:05d}.png")
    return len(frames)


async def frames_to_mp4(workdir: Path, out: Path) -> None:
    code, log_out = await run(
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(workdir / "frame_%05d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        str(out),
    )
    if code != 0:
        raise RuntimeError(f"ffmpeg failed (exit {code}):\n{_tail(log_out)}")


# --------------------------------------------------------------------------- #
# telegram handlers                                                           #
# --------------------------------------------------------------------------- #
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a YouTube link and I'll download it and send back an mp4."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    match = YT_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("That doesn't look like a YouTube link.")
        return

    url, vid = match.group(0), match.group(1)
    status = await update.message.reply_text("Checking formats…")
    workdir = Path(tempfile.mkdtemp(prefix="yt_"))
    try:
        ensure_ytdlp()
        selector, summary = await resolve_format(url)
        await status.edit_text(f"Downloading ({summary})…")
        media, info = await download(url, vid, selector)

        if media.suffix.lower() == ".mhtml":
            await status.edit_text("Converting storyboard to mp4…")
            n = await asyncio.to_thread(build_frames, media, workdir, info, selector)
            out = workdir / f"{vid}.mp4"
            await frames_to_mp4(workdir, out)
            caption = f"{vid} — {summary}, {n} frames @ {FPS:g}fps"
        else:
            out = media  # already a playable mp4
            caption = f"{vid} — {summary}"

        size = out.stat().st_size
        if size > 50 * 1024 * 1024:
            raise RuntimeError(
                f"result is {size // 1024 // 1024} MB; Telegram bots cap uploads at 50 MB"
            )

        await context.bot.send_chat_action(
            update.effective_chat.id, ChatAction.UPLOAD_VIDEO
        )
        with out.open("rb") as fh:
            await update.message.reply_video(
                video=fh, caption=caption, write_timeout=120, read_timeout=120
            )
        await status.delete()
    except Exception as exc:  # noqa: BLE001 — report any failure back to the user
        log.exception("request failed")
        await status.edit_text(f"Failed: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN (get one from @BotFather).")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Bot polling. Ctrl-C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
