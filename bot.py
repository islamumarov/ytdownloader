#!/usr/bin/env python3
"""Telegram bot that downloads a YouTube storyboard with yt-dlp and returns it as mp4.

Flow:
  1. User sends a YouTube link.
  2. `yt-dlp -f sb0 --write-info-json --cookies cookies.txt -o videos/<id>.<ext> <link>`
     downloads the storyboard, which yt-dlp saves as a `.mhtml` file: a MIME archive that
     embeds each storyboard sprite-sheet whole (one image per fragment).
  3. ffmpeg cannot read .mhtml, so we pull the sprite-sheets out of the archive, split each
     into its grid of thumbnail frames, and assemble the frames into an mp4 with ffmpeg.
     The tile grid is read from the format metadata in the .info.json (the .mhtml itself
     carries no geometry). If that metadata is unavailable we fall back to using each whole
     sprite-sheet as a single frame.
  4. The mp4 is sent back to the chat.

Format selection: by default (YTDLP_FORMAT=medium) the bot first lists the available formats
(`yt-dlp -J`) and downloads the *medium* one — the median of the distinct video heights the
video actually offers — merged to mp4. Set YTDLP_FORMAT to any yt-dlp `-f` value to override,
e.g. `best` for the top quality or `sb0` for the storyboard.

Note: `sb0` is the *storyboard* (a slideshow of tiny scrubber thumbnails), saved as `.mhtml`;
only that path runs the mhtml->mp4 conversion below. Any real video format is already playable
and is sent as-is.

Config via environment variables:
  BOT_TOKEN        (required) Telegram bot token from @BotFather
  YTDLP_PATH       yt-dlp binary            (default: ./yt-dlp)
  COOKIES_PATH     cookies file             (default: cookies.txt; skipped if missing)
  YTDLP_FORMAT     "medium" | any -f value  (default: medium; e.g. best, sb0)
  VIDEOS_DIR       download dir             (default: videos)
  STORYBOARD_FPS   frames/sec for slideshow (default: 4)
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
# subprocess helper                                                           #
# --------------------------------------------------------------------------- #
async def run(*cmd: str) -> tuple[int, str]:
    """Run a command, capturing merged stdout/stderr as text."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def run_out(*cmd: str) -> tuple[int, str, str]:
    """Run a command, capturing stdout and stderr separately (keeps stdout clean for JSON)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _tail(text: str, n: int = 1500) -> str:
    return text[-n:]


def _hint(err: str) -> str:
    """Turn common yt-dlp failure signatures into actionable one-line tips for the reply."""
    e = (err or "").lower()
    tips = []
    if "cookies" in e and ("no longer valid" in e or "rotated" in e or "expired" in e):
        tips.append("cookies.txt is expired — re-export fresh YouTube cookies")
    if "not a bot" in e or "sign in to confirm" in e:
        tips.append("bot check — refresh cookies and/or use a residential IP")
    if "javascript runtime" in e or "js runtime" in e:
        tips.append("install a JS runtime (deno) for the n-challenge, or pass --js-runtimes")
    return ("\nhints: " + "; ".join(tips)) if tips else ""


def cookie_args() -> list[str]:
    if Path(COOKIES).exists():
        return ["--cookies", COOKIES]
    log.warning("cookies file %r missing; continuing without it", COOKIES)
    return []


# --------------------------------------------------------------------------- #
# download                                                                    #
# --------------------------------------------------------------------------- #
def _ensure_ytdlp() -> None:
    if shutil.which(YTDLP) is None and not Path(YTDLP).exists():
        raise RuntimeError(
            f"yt-dlp not found at {YTDLP!r}. Put the binary in the repo "
            "(https://github.com/yt-dlp/yt-dlp/releases) and `chmod +x yt-dlp`, "
            "or set YTDLP_PATH."
        )


def _area(f: dict) -> int:
    return int(f.get("width") or 0) * int(f.get("height") or 0)


def _is_storyboard(f: dict) -> bool:
    return f.get("ext") == "mhtml" or f.get("format_note") == "storyboard"


def pick_medium(formats: list[dict]) -> tuple[str, str]:
    """Pick a medium-quality format from what yt-dlp actually lists.

    Prefers real video: the median of the distinct video heights, downloaded with audio and
    merged to mp4. When YouTube exposes no real video (bot-check / stale cookies often leave
    only storyboards), falls back to the median-resolution storyboard, which then flows
    through the mhtml->mp4 pipeline. Returns (yt-dlp selector, human summary).
    """
    heights = sorted(
        {
            int(f["height"])
            for f in formats
            if f.get("height") and f.get("vcodec") not in (None, "none")
        }
    )
    if heights:
        # median; even count -> upper-middle, so "medium" leans to the better quality
        med = heights[len(heights) // 2]
        listing = ", ".join(f"{h}p" for h in heights)
        return (
            f"bv*[height<={med}]+ba/b[height<={med}]/b",
            f"video: {listing} — picking {med}p (medium)",
        )

    storyboards = sorted(
        (f for f in formats if _is_storyboard(f) and f.get("width") and f.get("height")),
        key=_area,
    )
    if storyboards:
        pick = storyboards[len(storyboards) // 2]
        listing = ", ".join(f'{f["format_id"]} {f["width"]}x{f["height"]}' for f in storyboards)
        return (
            pick["format_id"],
            f"no real video available — storyboards: {listing}; "
            f"picking {pick['format_id']} ({pick['width']}x{pick['height']})",
        )

    return "best", "no listable formats — trying best"


async def resolve_format(url: str) -> tuple[str, str | None]:
    """Turn the configured FORMAT into a concrete yt-dlp selector plus a human summary.

    FORMAT == "medium": list the available formats (`yt-dlp -J`) and pick a medium one via
    pick_medium(). Any other value is passed straight through to yt-dlp unchanged.

    Listing is best-effort: `-J` needs the full player response, which YouTube's bot-check
    can deny even when a plain `-f sb0` download still succeeds. So on a listing failure we
    fall back to the storyboard rather than aborting the whole request.
    """
    if FORMAT != "medium":
        return FORMAT, None

    _ensure_ytdlp()
    code, out, err = await run_out(YTDLP, "-J", "--no-playlist", *cookie_args(), url)
    if code == 0:
        try:
            formats = json.loads(out).get("formats", [])
        except ValueError:
            formats = []
        if formats:
            selector, summary = pick_medium(formats)
            log.info(summary)
            return selector, summary

    reason = " ".join(_tail(err or out, 300).split()) or f"exit {code}"
    log.warning("format listing failed (%s); falling back to sb0", reason)
    return "sb0", f"format listing failed, using storyboard sb0{_hint(err or out)}"


async def download(url: str, vid: str, selector: str) -> tuple[Path, Path | None]:
    """Run yt-dlp with the given format selector; return (media_file, info_json | None).

    media_file is the .mhtml for storyboards, or the (mp4) media file for real formats.
    """
    _ensure_ytdlp()
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(VIDEOS_DIR / "%(id)s.%(ext)s")

    cmd = [
        YTDLP, "-f", selector,
        "--no-playlist", "--write-info-json",
        "--merge-output-format", "mp4",
        "-o", out_tmpl, *cookie_args(), url,
    ]
    code, out = await run(*cmd)
    if code != 0:
        raise RuntimeError(f"yt-dlp failed (exit {code}):\n{_tail(out)}{_hint(out)}")

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
        data = json.loads(info.read_text())
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", info, exc)
        return None
    formats = data.get("formats", [])
    # prefer the format actually downloaded (info.json records it), fall back to the request
    fmt_id = data.get("format_id") or fmt
    meta = next((f for f in formats if f.get("format_id") == fmt_id), None)
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
    user = update.effective_user
    allowed = os.environ.get("USER")
    if allowed and user.username != allowed:
        await update.message.reply_text(
            f"Sorry, @{user.username}, this bot is private. "
        )
        return
    await update.message.reply_text(
        "Send me a YouTube link and I'll return the storyboard as an mp4."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed = os.environ.get("USER")
    user = update.effective_user
    if allowed and user.username != allowed:
        await update.message.reply_text(
            f"Sorry, @{user.username}, this bot is private. "
        )
        return
    match = YT_RE.search(update.message.text or "")
    if not match:
        await update.message.reply_text("That doesn't look like a YouTube link.")
        return

    url, vid = match.group(0), match.group(1)
    status = await update.message.reply_text("Checking formats…")
    workdir = Path(tempfile.mkdtemp(prefix="sb_"))
    try:
        selector, summary = await resolve_format(url)
        await status.edit_text(f"{summary}\nDownloading…" if summary else "Downloading…")
        media, info = await download(url, vid, selector)

        if media.suffix.lower() == ".mhtml":
            await status.edit_text("Converting storyboard to mp4…")
            n = await asyncio.to_thread(build_frames, media, workdir, info, FORMAT)
            out = workdir / f"{vid}.mp4"
            await frames_to_mp4(workdir, out)
            caption = f"{vid} — {n} storyboard frames @ {FPS:g}fps"
        else:
            out = media  # already a playable media file (e.g. real video)
            caption = f"{vid}\n{summary}" if summary else vid

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
