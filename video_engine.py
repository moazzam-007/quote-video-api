import os
import subprocess
import requests
import asyncio
import edge_tts
import shutil
import json
import time

# ── FFmpeg Settings ───────────────────────────────────────────────────────────
FFMPEG_TIMEOUT = 60   # 60s max per ffmpeg call
THREADS        = "1"   # RAM constraint on Render free tier
FADE_DURATION  = 0.5   # xfade dissolve duration between images (seconds)
BLACK_INTRO    = 0.5   # black screen at start (seconds)
TTS_VOICE      = "en-US-AndrewNeural"
# ─────────────────────────────────────────────────────────────────────────────


def run_ffmpeg(cmd):
    """Run an FFmpeg command. Raises RuntimeError on failure or timeout."""
    try:
        result = subprocess.run(
            cmd, check=True, timeout=FFMPEG_TIMEOUT,
            capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg failed: {e.stderr[-800:]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg timed out")


def get_audio_duration(mp3_path):
    """Use ffprobe to get the duration of an MP3 file in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", mp3_path
    ]
    try:
        out = subprocess.check_output(cmd, timeout=15, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        return float(data["format"]["duration"])
    except Exception as e:
        raise RuntimeError(f"ffprobe failed on {mp3_path}: {e}")


def download_images(image_urls, work_dir):
    """Download each image URL to work_dir as img_0.jpg, img_1.jpg, ..."""
    # Browser-like headers required — Instagram CDN blocks plain Python requests
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    }
    for i, url in enumerate(image_urls):
        success = False
        last_error = None
        for attempt in range(2):
            try:
                resp = requests.get(url, timeout=20, headers=headers)
                resp.raise_for_status()
                out_path = os.path.join(work_dir, f"img_{i}.jpg")
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                success = True
                break
            except Exception as e:
                last_error = e
                time.sleep(1)
        if not success:
            raise RuntimeError(f"Failed to download image {i} ({url}): {last_error}")



async def _tts_async(text, voice, output_path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def generate_tts_for_each(quotes, work_dir):
    """
    Generate a separate TTS mp3 for each quote.
    Returns list of (tts_path, duration_seconds).
    """
    results = []
    for i, quote in enumerate(quotes):
        tts_path = os.path.join(work_dir, f"tts_{i}.mp3")
        asyncio.run(_tts_async(quote, TTS_VOICE, tts_path))
        duration = get_audio_duration(tts_path)
        results.append((tts_path, duration))
    return results


def create_black_clip(work_dir, duration, name="black_intro.mp4"):
    """Create a silent black video clip of given duration at 1080x1920."""
    out = os.path.join(work_dir, name)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=30:d={duration}",
        "-vf", "setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p", "-an", "-threads", THREADS,
        out
    ]
    run_ffmpeg(cmd)
    return out


def create_image_segment(img_path, duration, work_dir, index):
    """
    Convert a still image to a silent video with given duration.
    Uses decrease+pad so text at edges is never cropped.
    """
    out = os.path.join(work_dir, f"raw_{index}.mp4")
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", img_path,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-t", str(duration), "-pix_fmt", "yuv420p",
        "-vf", vf, "-r", "30", "-an", "-threads", THREADS,
        out
    ]
    run_ffmpeg(cmd)
    return out


def merge_video_audio(video_path, audio_path, work_dir, index):
    """Mux a silent video segment with its TTS audio track."""
    out = os.path.join(work_dir, f"seg_{index}.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path, "-i", audio_path,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-shortest", "-threads", THREADS,
        out
    ]
    run_ffmpeg(cmd)
    return out


def xfade_two(clip_a, clip_b, duration_a, work_dir, out_name, fade=FADE_DURATION):
    """
    Apply xfade dissolve between two clips.
    offset = duration_a - fade_duration (where fade starts in clip_a timeline).
    """
    out = os.path.join(work_dir, out_name)
    offset = max(0.0, duration_a - fade)
    cmd = [
        "ffmpeg", "-y",
        "-i", clip_a, "-i", clip_b,
        "-filter_complex",
        (
            f"[0:a]atrim=end={duration_a - fade}[a0];"
            f"[0:v][1:v]xfade=transition=dissolve:duration={fade}:offset={offset},format=yuv420p[v];"
            f"[a0][1:a]concat=n=2:v=0:a=1[a]"
        ),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-pix_fmt", "yuv420p", "-threads", THREADS,
        out
    ]
    run_ffmpeg(cmd)
    return out


def chain_segments_with_xfade(segments, durations, work_dir):
    """
    Sequentially xfade all segments 2-at-a-time.
    segments: list of mp4 paths (each has video + audio)
    durations: list of float durations (seconds) for each segment
    Returns path to the final chained video.
    """
    if len(segments) == 1:
        return segments[0]

    current = segments[0]
    current_duration = durations[0]

    for i in range(1, len(segments)):
        out_name = f"chained_{i}.mp4"
        current = xfade_two(
            current, segments[i],
            current_duration, work_dir, out_name
        )
        # New duration accounts for the overlap removed by xfade
        current_duration = current_duration + durations[i] - FADE_DURATION

    return current


def prepend_black_intro(black_clip, main_video, work_dir):
    """
    Concatenate black intro + main video using ffmpeg concat demuxer.
    Black intro has no audio, so we create a silent audio track for it.
    """
    # Add silent audio to black clip so concat works cleanly
    black_with_audio = os.path.join(work_dir, "black_intro_audio.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", black_clip,
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-t", str(BLACK_INTRO), "-shortest", "-threads", THREADS,
        black_with_audio
    ]
    run_ffmpeg(cmd)

    # Write concat list
    concat_list = os.path.join(work_dir, "concat.txt")
    with open(concat_list, "w") as f:
        f.write(f"file '{black_with_audio}'\n")
        f.write(f"file '{main_video}'\n")

    final = os.path.join(work_dir, "final.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy",
        final
    ]
    run_ffmpeg(cmd)
    return final


def upload_to_tmpfiles(file_path):
    """Upload final.mp4 to tmpfiles.org and return the dl/ URL."""
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": f},
                timeout=120
            )
            resp.raise_for_status()
        data = resp.json()
        url = data.get("data", {}).get("url", "")
        # Convert view URL → direct download URL
        if "tmpfiles.org/" in url and "tmpfiles.org/dl/" not in url:
            url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        return url
    except Exception as e:
        raise RuntimeError(f"Upload failed: {e}")


def process_video_job(job_id, image_urls, quotes):
    """
    Main orchestration:
      image_urls: list of str (4-7)
      quotes:     list of str — one quote per image (same length as image_urls)
    Returns dict with success/video_url/duration_seconds or error.
    """
    work_dir = os.path.join("/tmp", job_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        n = len(image_urls)

        # Step 1 — Download all images
        download_images(image_urls, work_dir)

        # Step 2 — Generate TTS for each quote
        tts_results = generate_tts_for_each(quotes, work_dir)
        # tts_results = [(tts_path, duration), ...]

        # Step 3 — Build each segment: image → raw video → merge with TTS
        segments  = []
        durations = []
        for i in range(n):
            img_path          = os.path.join(work_dir, f"img_{i}.jpg")
            tts_path, tts_dur = tts_results[i]

            raw_vid = create_image_segment(img_path, tts_dur, work_dir, i)
            seg     = merge_video_audio(raw_vid, tts_path, work_dir, i)

            segments.append(seg)
            durations.append(tts_dur)

        # Step 4 — Chain all segments with xfade dissolve between them
        chained = chain_segments_with_xfade(segments, durations, work_dir)

        # Step 5 — Prepend black intro (0.5s)
        black_clip = create_black_clip(work_dir, BLACK_INTRO)
        final_path = prepend_black_intro(black_clip, chained, work_dir)

        # Step 6 — Upload to tmpfiles.org
        video_url = upload_to_tmpfiles(final_path)

        total_duration = round(BLACK_INTRO + sum(durations) - (n - 1) * FADE_DURATION, 2)

        return {
            "success": True,
            "video_url": video_url,
            "duration_seconds": total_duration
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass
