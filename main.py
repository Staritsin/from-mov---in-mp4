from flask import Flask, request, jsonify, send_file, abort
import os, uuid, subprocess, requests, math, threading

app = Flask(__name__)

# Временные каталоги (подходит для Render Free/Starter)
UPLOAD_FOLDER = "/tmp/downloads"
OUTPUT_FOLDER = "/tmp/converted"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


@app.get("/health")
def health():
    return "ok", 200


def abs_base_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto", "https")
    return f"{proto}://{request.host}"


def download_to(path: str, url: str) -> None:
    """
    Качаем исходник. Если источник не доступен — возвращаем понятную 400,
    а не обрушиваем процесс 500-кой.
    """
    app.logger.info(f"[download] GET {url}")
    r = requests.get(url, stream=True, timeout=120)
    if r.status_code != 200:
        app.logger.error(f"[download] {r.status_code} for {url}")
        abort(400, description=f"Source URL not reachable: {url} (status {r.status_code})")
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)


def probe_duration_sec(path: str) -> float:
    """Достаём длительность через ffprobe, 0.0 если не получилось."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            text=True
        ).strip()
        return float(out)
    except Exception:
        return 0.0


def convert_copy(src: str, dst: str) -> None:
    # -threads 1, чтобы на маленьких инстансах не ловить OOM/таймауты
    cmd = [
        "ffmpeg", "-y", "-threads", "1", "-i", src,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        dst
    ]
    subprocess.check_call(cmd)


def convert_downscale(src: str, dst: str, max_width: int, target_mb: int,
                      audio_kbps: int = 96, floor_video_kbps: int = 300) -> None:
    """
    Ужимаем в ~target_mb, ограничиваем ширину до max_width.
    Считаем общий битрейт из длительности и держим CBR (maxrate/bufsize).
    """
    dur = probe_duration_sec(src)
    if dur <= 0:
        # Запасной вариант — CRF при неизвестной длительности
        cmd = [
            "ffmpeg", "-y", "-threads", "1", "-i", src,
            "-vf", f"scale='if(gt(iw,{max_width}),{max_width},iw)':'-2'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", dst
        ]
        subprocess.check_call(cmd)
        return

    total_kbps = max(1, math.floor((target_mb * 8192) / dur))  # MB -> Mbit -> kbit/s
    video_kbps = max(floor_video_kbps, total_kbps - audio_kbps)

    cmd = [
        "ffmpeg", "-y", "-threads", "1", "-i", src,
        "-vf", f"scale='if(gt(iw,{max_width}),{max_width},iw)':'-2'",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart", dst
    ]
    subprocess.check_call(cmd)


def make_url(task_id: str) -> str:
    return f"{abs_base_url()}/result/{task_id}?raw=true"


def handle_pipeline(src_url: str, mode: str, target_mb: int, max_width: int,
                    audio_kbps: int, task_id: str | None = None) -> dict:
    """
    Полная обработка: скачали -> конверт/даунскейл -> вернули mp4_url.
    Можно передать готовый task_id (для /enqueue).
    """
    if task_id is None:
        task_id = str(uuid.uuid4())

    src = os.path.join(UPLOAD_FOLDER, f"{task_id}.src")
    dst = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")

    download_to(src, src_url)

    if mode == "copy":
        convert_copy(src, dst)
    elif mode == "downscale":
        convert_downscale(src, dst, max_width=max_width, target_mb=target_mb, audio_kbps=audio_kbps)
    else:  # auto
        size_mb = max(1, os.path.getsize(src) // (1024 * 1024))
        if size_mb > 20:
            convert_downscale(src, dst, max_width=max_width, target_mb=target_mb, audio_kbps=audio_kbps)
        else:
            convert_copy(src, dst)

    try:
        os.remove(src)
    except Exception:
        pass

    return {"status": "done", "task_id": task_id, "mp4_url": make_url(task_id)}


# ------------ СИНХРОННЫЕ ЭНДПОИНТЫ (можно оставить для совместимости) ------------
@app.post("/convert")
def convert_endpoint():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url or not url.startswith("http"):
        return jsonify({"error": "Missing or invalid url"}), 400
    res = handle_pipeline(url, mode="copy", target_mb=19, max_width=720, audio_kbps=96)
    return jsonify(res), 200


@app.post("/downscale")
def downscale_endpoint():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    target_mb = int(data.get("target_mb", 19))
    max_width = int(data.get("max_width", 720))
    audio_kbps = int(data.get("audio_kbps", 96))
    if not url or not url.startswith("http"):
        return jsonify({"error": "Missing or invalid url"}), 400
    res = handle_pipeline(url, mode="downscale", target_mb=target_mb,
                          max_width=max_width, audio_kbps=audio_kbps)
    return jsonify(res), 200


@app.post("/smart")
def smart_endpoint():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    target_mb = int(data.get("target_mb", 19))
    max_width = int(data.get("max_width", 720))
    audio_kbps = int(data.get("audio_kbps", 96))
    if not url or not url.startswith("http"):
        return jsonify({"error": "Missing or invalid url"}), 400
    app.logger.info(f"/smart url={url}")
    res = handle_pipeline(url, mode="auto", target_mb=target_mb,
                          max_width=max_width, audio_kbps=audio_kbps)
    return jsonify(res), 200


# ------------------------- АСИНХРОННЫЙ ЭНДПОИНТ -------------------------
@app.post("/enqueue")
def enqueue():
    """
    Запускает обработку в фоне и сразу возвращает task_id.
    JSON:
    {
      "url": "https://...",
      "target_mb": 19,
      "max_width": 720,
      "audio_kbps": 96,
      "mode": "auto" | "copy" | "downscale"
    }
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    target_mb = int(data.get("target_mb", 19))
    max_width = int(data.get("max_width", 720))
    audio_kbps = int(data.get("audio_kbps", 96))
    mode = data.get("mode", "auto")

    if not url or not url.startswith("http"):
        return jsonify({"error": "Missing or invalid url"}), 400

    task_id = str(uuid.uuid4())

    def worker():
        try:
            app.logger.info(f"[enqueue] start task={task_id} url={url}")
            handle_pipeline(url, mode=mode, target_mb=target_mb,
                            max_width=max_width, audio_kbps=audio_kbps, task_id=task_id)
            app.logger.info(f"[enqueue] done task={task_id}")
        except Exception as e:
            app.logger.exception(f"[enqueue] fail task={task_id}: {e}")

    threading.Thread(target=worker, daemon=True).start()

    return jsonify({
        "status": "queued",
        "task_id": task_id,
        "result": f"{abs_base_url()}/result/{task_id}"
    }), 202


@app.get("/result/<task_id>")
def get_result(task_id):
    dst = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    if not os.path.exists(dst):
        return jsonify({"status": "processing"}), 200
    if request.args.get("raw") == "true":
        return send_file(dst, mimetype="video/mp4", as_attachment=False)
    size_kb = os.path.getsize(dst) // 1024
    return jsonify({"status": "done", "fileSizeKB": size_kb, "mp4_url": make_url(task_id)}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
