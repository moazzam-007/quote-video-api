import os
import subprocess
import requests
import asyncio
import edge_tts
import shutil
import json
import random
import base64

# ── FFmpeg Settings ───────────────────────────────────────────────────────────
FFMPEG_TIMEOUT = 600
THREADS        = "1"
FADE_DURATION  = 0.5   # 0.5s fade to black at start/end of clips
BLACK_INTRO    = 1.0   # 1.0s black intro segment
PRE_VOICE_DELAY  = 1.0
POST_VOICE_DELAY = 0.8
TTS_VOICE      = "en-US-AndrewNeural"
RESOLUTION     = "720:1280"
FPS            = 30
SAMPLE_RATE    = 44100
# ─────────────────────────────────────────────────────────────────────────────

def run_ffmpeg(cmd):
    try:
        subprocess.run(
            cmd, check=True, timeout=FFMPEG_TIMEOUT,
            capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg failed: {e.stderr[-800:]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg timed out")

def get_audio_duration(mp3_path):
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

def save_base64_images(images_b64, work_dir):
    for i, b64 in enumerate(images_b64):
        out_path = os.path.join(work_dir, f"img_{i}.jpg")
        try:
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            img_data = base64.b64decode(b64)
            with open(out_path, "wb") as f:
                f.write(img_data)
        except Exception as e:
            raise RuntimeError(f"Failed to decode and save base64 image {i}: {e}")

async def _tts_async(text, voice, output_path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)

def generate_tts_for_each(quotes, work_dir):
    results = []
    for i, quote in enumerate(quotes):
        tts_path = os.path.join(work_dir, f"tts_{i}.mp3")
        asyncio.run(_tts_async(quote, TTS_VOICE, tts_path))
        duration = get_audio_duration(tts_path)
        results.append((tts_path, duration))
    return results

def generate_black_intro(work_dir):
    """Generate 1-second black intro with silence matching the exact segment format."""
    out = os.path.join(work_dir, "intro.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={RESOLUTION.replace(':', 'x')}:d={BLACK_INTRO}:r={FPS}",
        "-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
        "-t", str(BLACK_INTRO), "-shortest", "-threads", THREADS,
        out
    ]
    run_ffmpeg(cmd)
    return out

def create_image_segment(img_path, tts_path, tts_dur, work_dir, index):
    """Create a 720p segment with fade in/out and perfectly timed TTS audio."""
    out = os.path.join(work_dir, f"seg_{index}.mp4")
    total_dur = PRE_VOICE_DELAY + tts_dur + POST_VOICE_DELAY
    fade_out_start = total_dur - FADE_DURATION
    
    vf = (
        f"scale={RESOLUTION}:force_original_aspect_ratio=decrease,"
        f"pad={RESOLUTION}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fade=t=in:st=0:d={FADE_DURATION},"
        f"fade=t=out:st={fade_out_start}:d={FADE_DURATION},"
        f"format=yuv420p"
    )
    
    delay_ms = int(PRE_VOICE_DELAY * 1000)
    af = (
        f"adelay={delay_ms}|{delay_ms},apad,atrim=0:{total_dur},"
        f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(total_dur), "-i", img_path,
        "-i", tts_path,
        "-vf", vf, "-af", af,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-r", str(FPS), "-threads", THREADS,
        out
    ]
    run_ffmpeg(cmd)
    return out

def get_random_bgm():
    """Pick a random BGM file from bgm/ directory if it exists."""
    bgm_dir = os.path.join(os.getcwd(), "bgm")
    if os.path.isdir(bgm_dir):
        files = [f for f in os.listdir(bgm_dir) if f.endswith(('.mp3', '.wav', '.m4a'))]
        if files:
            return os.path.join(bgm_dir, random.choice(files))
    return None

def concat_and_mix_bgm(segments, work_dir):
    """Concat all segments instantly, then mix BGM if available."""
    concat_txt = os.path.join(work_dir, "concat.txt")
    with open(concat_txt, "w") as f:
        for seg in segments:
            f.write(f"file '{os.path.abspath(seg).replace(chr(92), '/')}'\n")
    
    concat_out = os.path.join(work_dir, "concat_output.mp4")
    concat_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-c", "copy", concat_out
    ]
    run_ffmpeg(concat_cmd)
    
    bgm_file = get_random_bgm()
    if not bgm_file:
        return concat_out
    
    final_out = os.path.join(work_dir, "final.mp4")
    mix_cmd = [
        "ffmpeg", "-y", 
        "-i", concat_out, 
        "-stream_loop", "-1", "-i", bgm_file,
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.3[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        final_out
    ]
    try:
        run_ffmpeg(mix_cmd)
        return final_out
    except Exception as e:
        print(f"Warning: BGM mixing failed, using video without BGM. {e}", flush=True)
        return concat_out

def upload_to_tmpfiles(file_path):
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
        if "tmpfiles.org/" in url and "tmpfiles.org/dl/" not in url:
            url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        return url
    except Exception as e:
        raise RuntimeError(f"Upload failed: {e}")

def process_video_job(job_id, images_b64, quotes):
    work_dir = os.path.join("/tmp", job_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        n = len(images_b64)
        print(f"[{job_id}] Starting job with {n} images", flush=True)

        print(f"[{job_id}] Step 1: Saving base64 images...", flush=True)
        save_base64_images(images_b64, work_dir)

        print(f"[{job_id}] Step 2: Generating TTS...", flush=True)
        tts_results = generate_tts_for_each(quotes, work_dir)

        print(f"[{job_id}] Step 3: Building intro & segments with fade...", flush=True)
        segments = []
        intro_seg = generate_black_intro(work_dir)
        segments.append(intro_seg)
        
        total_duration = BLACK_INTRO
        for i in range(n):
            img_path = os.path.join(work_dir, f"img_{i}.jpg")
            tts_path, tts_dur = tts_results[i]
            seg = create_image_segment(img_path, tts_path, tts_dur, work_dir, i)
            segments.append(seg)
            total_duration += (PRE_VOICE_DELAY + tts_dur + POST_VOICE_DELAY)

        print(f"[{job_id}] Step 4: Concatenating and Mixing BGM...", flush=True)
        final_path = concat_and_mix_bgm(segments, work_dir)

        print(f"[{job_id}] Step 5: Uploading to tmpfiles...", flush=True)
        video_url = upload_to_tmpfiles(final_path)

        print(f"[{job_id}] ✅ Done! URL: {video_url}", flush=True)

        return {
            "success": True,
            "video_url": video_url,
            "duration_seconds": round(total_duration, 2)
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass
