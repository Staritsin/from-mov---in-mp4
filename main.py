from flask import Flask, request, jsonify, send_file
import os
import uuid
import subprocess
import requests

app = Flask(__name__)

UPLOAD_FOLDER = "/tmp/downloads"
OUTPUT_FOLDER = "/tmp/converted"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

@app.get("/health")
def health():
    return "ok", 200

def abs_base_url():
    proto = request.headers.get("X-Forwarded-Proto", "https")
    host = request.host
    return f"{proto}://{host}"

def download_to(path: str, url: str):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

def convert_video_file(input_path: str, output_path: str):
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.check_call(cmd)

@app.post("/convert")
def start_conversion():
    """
    JSON: {"url": "https://..."}  ->  {"status":"started","task_id":"..."}
    Результат см. GET /result/<task_id>
    """
    data = request.get_json(silent=True) or {}
    input_url = data.get("url")
    if not input_url or not input_url.startswith("http"):
        return jsonify({"error": "Missing or invalid url"}), 400

    task_id = str(uuid.uuid4())
    input_path = os.path.join(UPLOAD_FOLDER, f"{task_id}.src")
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")

    try:
        download_to(input_path, input_url)
        convert_video_file(input_path, output_path)
    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass

    # Можно вернуть сразу готовый URL (без опроса)
    mp4_url = f"{abs_base_url()}/result/{task_id}?raw=true"
    return jsonify({"status": "done", "task_id": task_id, "mp4_url": mp4_url}), 200

@app.get("/result/<task_id>")
def get_result(task_id):
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    if not os.path.exists(output_path):
        return jsonify({"status": "processing"}), 200

    if request.args.get("raw") == "true":
        return send_file(output_path, mimetype="video/mp4", as_attachment=False)

    mp4_url = f"{abs_base_url()}/result/{task_id}?raw=true"
    size_kb = os.path.getsize(output_path) // 1024
    return jsonify({"status": "done", "fileSizeKB": size_kb, "mp4_url": mp4_url}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
