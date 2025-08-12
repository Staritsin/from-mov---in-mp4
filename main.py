from flask import Flask, request, jsonify, send_file
import os, uuid, subprocess, requests, math

app = Flask(__name__)

UPLOAD_DIR = "/tmp/downloads"
OUTPUT_DIR = "/tmp/converted"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

OUT_W = 1080   # ширина выходного видео
OUT_H = 1920   # высота выходного видео

def _download(url: str, dst: str):
    with requests.get(url, stream=True, timeout=300, headers={"User-Agent": "mov2mp4-9x16"}) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

def _probe_duration_sec(path: str) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            text=True
        ).strip()
        return float(out)
    except Exception:
        return 0.0

def _vf_9x16_crop():
    """
    Обрезка под 9:16 по центру + масштаб до 1080x1920.
    """
    # crop динамически под исходный аспект, затем scale до OUT_W x OUT_H
    crop = (
        "crop="
        "w='if(gte(iw/ih,9/16),ih*9/16,iw)':"
        "h='if(gte(iw/ih,9/16),ih,iw*16/9)':"
        "x='(iw-ow)/2':"
        "y='(ih-oh)/2'"
    )
    scale = f"scale={OUT_W}:{OUT_H}"
    return f"{crop},{scale}"

def _vf_9x16_pad(max_width_before_pad=None):
    """
    Масштаб по длинной стороне + паддинг до 1080x1920 (без обрезки).
    max_width_before_pad — опционально ограничить ширину перед паддингом (обычно не нужно).
    """
    # Сначала масштабируем так, чтобы уложиться внутрь 1080x1920, затем добавляем поля.
    # (после scale переменные iw/ih — уже новые размеры)
    scale = (
        f"scale='if(gte(iw/ih,9/16),{OUT_W},-2)':'if(gte(iw/ih,9/16),-2,{OUT_H})'"
    )
    pad = f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
    if max_width_before_pad and isinstance(max_width_before_pad, int):
        # при желании можно предварительно ограничить ширину
        pre = f"scale='if(gt(iw,{max_width_before_pad}),{max_width_before_pad},iw)':'-2'"
        return f"{pre},{scale},{pad}"
    return f"{scale},{pad}"

def _encode_ffmpeg(src: str, dst: str, vf: str, mode: str, crf: int, target_mb: int, audio_kbps: int):
    """
    mode: 'crf' | 'target'
    """
    if mode == "target":
        dur = _probe_duration_sec(src)
        if dur > 0:
            total_kbps = max(1, math.floor((target_mb * 8192) / dur))
            video_kbps = max(300, total_kbps - audio_kbps)
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps*2}k",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                "-movflags", "+faststart",
                dst
            ]
        else:
            # если длительность не прочиталась — используем CRF 28 как fallback
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                "-movflags", "+faststart",
                dst
            ]
    else:
        # CRF-режим
        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            dst
        ]
    subprocess.check_call(cmd)

@app.post("/convert")
def convert():
    """
    JSON:
    {
      "url": "https://.../video.mov",     # обязательный
      "mode": "crf" | "target",           # сжатие: по качеству (CRF) или под размер; по умолчанию "crf"
      "crf": 23,                          # для mode=crf (18..28), по умолчанию 23
      "target_mb": 19,                    # для mode=target, желаемый размер
      "audio_kbps": 96,                   # аудио битрейт
      "aspect_mode": "crop" | "pad"       # приведение к 9:16: обрезать или паддить; по умолчанию "crop"
    }
    Ответ всегда 1080x1920 (9:16).
    """
    data = request.get_json(silent=True) or {}

    url = data.get("url")
    if not url or not str(url).startswith("http"):
        return jsonify({"error": "Missing or invalid 'url'"}), 400

    mode = (data.get("mode") or "crf").lower()
    crf = int(data.get("crf", 23))
    target_mb = int(data.get("target_mb", 19))
    audio_kbps = int(data.get("audio_kbps", 96))
    aspect_mode = (data.get("aspect_mode") or "crop").lower()  # "crop" | "pad"

    task_id = str(uuid.uuid4())
    src = os.path.join(UPLOAD_DIR, f"{task_id}.src")
    dst = os.path.join(OUTPUT_DIR, f"{task_id}.mp4")

    # Видеофильтр под 9:16
    vf = _vf_9x16_crop() if aspect_mode == "crop" else _vf_9x16_pad()

    try:
        _download(url, src)
        _encode_ffmpeg(src, dst, vf=vf, mode=mode, crf=crf, target_mb=target_mb, audio_kbps=audio_kbps)
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg failed: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(src):
            try:
                os.remove(src)
            except Exception:
                pass

    size_mb = os.path.getsize(dst) / (1024 * 1024)
    file_url = f"{request.scheme}://{request.host}/file/{task_id}.mp4"
    return jsonify({
        "status": "done",
        "width": OUT_W,
        "height": OUT_H,
        "aspect": "9:16",
        "aspect_mode": aspect_mode,
        "mode": mode,
        "result_mb": round(size_mb, 2),
        "mp4_url": file_url
    }), 200

@app.get("/file/<task_id>.mp4")
def get_file(task_id):
    path = os.path.join(OUTPUT_DIR, f"{task_id}.mp4")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, mimetype="video/mp4", as_attachment=False, download_name=f"{task_id}.mp4")

@app.get("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
