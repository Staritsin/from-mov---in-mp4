# main.py — конвертер в MP4 9:16 для Render
# Поддерживает:
#   1) POST /convert {url,...}       — синхронно, сразу отдаёт mp4_url
#   2) POST /enqueue {url,...}       — асинхронно по URL (202 + status_url)
#   3) POST /enqueue_file (multipart)— асинхронно по бинарю файла
#   4) GET  /status?job_id=...       — статус задачи
#   5) GET  /file/<id>.mp4           — выдача готового видео
#   6) GET  /health                  — "ok"
#
# Сделано без заглушек. Готово под gunicorn на Render.

import os
import uuid
import math
import json
import shlex
import threading
import subprocess
from typing import Dict, Any
from flask import Flask, request, jsonify, send_file, abort, Response

app = Flask(__name__)

# --- Директории ---
UPLOAD_DIR = "/tmp/uploads"
OUTPUT_DIR = "/tmp/converted"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- Ограничение размеров запроса (1 ГБ) ---
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

# --- Глобальные константы видео ---
OUT_W = 1080
OUT_H = 1920

# --- Очередь задач в памяти ---
JOBS: Dict[str, Dict[str, Any]] = {}  # job_id -> {"status":"queued|processing|done|error", "out_url":"/file/<id>.mp4", "error":None}


# ============================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================

def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.after_request
def _disable_cache(resp: Response) -> Response:
    return _no_store(resp)

def _download(url: str, dst: str) -> None:
    """Скачивание по URL на диск."""
    import requests  # локальный импорт, чтобы не тормозить cold start
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

def _vf_9x16_crop() -> str:
    """
    Центровая обрезка под 9:16 + scale до 1080x1920.
    """
    crop = (
        "crop="
        "w='if(gte(iw/ih,9/16),ih*9/16,iw)':"
        "h='if(gte(iw/ih,9/16),ih,iw*16/9)':"
        "x='(iw-ow)/2':"
        "y='(ih-oh)/2'"
    )
    scale = f"scale={OUT_W}:{OUT_H}"
    return f"{crop},{scale}"

def _vf_9x16_pad() -> str:
    """
    Масштаб внутрь 1080x1920 без обрезки + паддинг до 9:16.
    """
    scale = f"scale='if(gte(iw/ih,9/16),{OUT_W},-2)':'if(gte(iw/ih,9/16),-2,{OUT_H})'"
    pad = f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
    return f"{scale},{pad}"

def _encode_ffmpeg(src: str, dst: str, vf: str, mode: str, crf: int, target_mb: int, audio_kbps: int) -> None:
    """
    Кодирование в H.264/AAC 9:16.
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
            # fallback, если длительность не считалась
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                "-movflags", "+faststart",
                dst
            ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            dst
        ]
    subprocess.check_call(cmd)


def _start_job_from_path(src_path: str, opts: Dict[str, Any]) -> str:
    """
    Создаёт задачу из локального файла. Возвращает job_id.
    """
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "out_url": None, "error": None}

    def _worker():
        try:
            JOBS[job_id]["status"] = "processing"
            dst = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")
            aspect_mode = (opts.get("aspect_mode") or "crop").lower()
            vf = _vf_9x16_crop() if aspect_mode == "crop" else _vf_9x16_pad()
            _encode_ffmpeg(
                src=src_path,
                dst=dst,
                vf=vf,
                mode=(opts.get("mode") or "crf").lower(),
                crf=int(opts.get("crf", 23)),
                target_mb=int(opts.get("target_mb", 19)),
                audio_kbps=int(opts.get("audio_kbps", 96)),
            )
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["out_url"] = f"/file/{job_id}.mp4"
        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
        finally:
            try:
                if os.path.exists(src_path):
                    os.remove(src_path)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()
    return job_id


# ============================
# ЭНДПОИНТЫ
# ============================

@app.get("/health")
def health():
    return "ok", 200


@app.post("/convert")
def convert():
    """
    СИНХРОННО: скачивает url и сразу возвращает mp4_url.
    Body JSON:
    {
      "url": "https://.../video.mov",
      "mode": "crf" | "target",
      "crf": 23,
      "target_mb": 19,
      "audio_kbps": 96,
      "aspect_mode": "crop" | "pad"
    }
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url or not str(url).startswith("http"):
        return jsonify({"error": "Missing or invalid 'url'"}), 400

    mode = (data.get("mode") or "crf").lower()
    crf = int(data.get("crf", 23))
    target_mb = int(data.get("target_mb", 19))
    audio_kbps = int(data.get("audio_kbps", 96))
    aspect_mode = (data.get("aspect_mode") or "crop").lower()

    task_id = uuid.uuid4().hex
    src = os.path.join(UPLOAD_DIR, f"{task_id}.src")
    dst = os.path.join(OUTPUT_DIR, f"{task_id}.mp4")
    vf = _vf_9x16_crop() if aspect_mode == "crop" else _vf_9x16_pad()

    try:
        _download(url, src)
        _encode_ffmpeg(src, dst, vf=vf, mode=mode, crf=crf, target_mb=target_mb, audio_kbps=audio_kbps)
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"ffmpeg failed: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            if os.path.exists(src):
                os.remove(src)
        except Exception:
            pass

    size_mb = os.path.getsize(dst) / (1024 * 1024)
    base = f"{request.scheme}://{request.host}"
    return jsonify({
        "status": "done",
        "width": OUT_W,
        "height": OUT_H,
        "aspect": "9:16",
        "aspect_mode": aspect_mode,
        "mode": mode,
        "result_mb": round(size_mb, 2),
        "mp4_url": f"{base}/file/{task_id}.mp4"
    }), 200


@app.post("/enqueue")
def enqueue():
    """
    АСИНХРОННО ПО URL: 202 + status_url/result_url.
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url or not str(url).startswith("http"):
        return jsonify({"error": "Missing or invalid 'url'"}), 400

    opts = {
        "mode": (data.get("mode") or "crf").lower(),
        "crf": int(data.get("crf", 23)),
        "target_mb": int(data.get("target_mb", 19)),
        "audio_kbps": int(data.get("audio_kbps", 96)),
        "aspect_mode": (data.get("aspect_mode") or "crop").lower(),
    }

    # Скачиваем во временный файл и ставим задачу
    tmp = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}.src")
    try:
        _download(url, tmp)
    except Exception as e:
        return jsonify({"error": f"download failed: {e}"}), 400

    job_id = _start_job_from_path(tmp, opts)
    base = f"{request.scheme}://{request.host}"
    return jsonify({
        "status": "queued",
        "job_id": job_id,
        "result_url": f"{base}/file/{job_id}.mp4",
        "status_url": f"{base}/status?job_id={job_id}"
    }), 202


@app.post("/enqueue_file")
def enqueue_file():
    """
    АСИНХРОННО ПО ФАЙЛУ (multipart/form-data):
      - file: бинарь видео
      - opts: JSON-строка с полями как в /enqueue
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file is required"}), 400

    opts_raw = request.form.get("opts", "{}")
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return jsonify({"error": "opts must be JSON"}), 400

    # Нормализуем опции
    opts = {
        "mode": (opts.get("mode") or "crf").lower(),
        "crf": int(opts.get("crf", 23)),
        "target_mb": int(opts.get("target_mb", 19)),
        "audio_kbps": int(opts.get("audio_kbps", 96)),
        "aspect_mode": (opts.get("aspect_mode") or "crop").lower(),
    }

    tmp = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}.src")
    f.save(tmp)

    job_id = _start_job_from_path(tmp, opts)
    base = f"{request.scheme}://{request.host}"
    return jsonify({
        "status": "queued",
        "job_id": job_id,
        "result_url": f"{base}/file/{job_id}.mp4",
        "status_url": f"{base}/status?job_id={job_id}"
    }), 200


@app.get("/status")
def status():
    job_id = request.args.get("job_id")
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    return jsonify({"job_id": job_id, **j}), 200


@app.get("/file/<name>")
def file_out(name: str):
    # name ожидается вида "<job_id>.mp4"
    if not name.endswith(".mp4"):
        return abort(404)
    path = os.path.join(OUTPUT_DIR, name)
    if not os.path.exists(path):
        return abort(404)
    resp = send_file(path, mimetype="video/mp4", as_attachment=False, download_name=name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --- entrypoint ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
