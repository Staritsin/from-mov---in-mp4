from flask import Flask, request, jsonify, send_from_directory
import os
import uuid
import threading
import subprocess
import json

app = Flask(__name__)

UPLOAD_FOLDER = "downloads"
OUTPUT_FOLDER = "converted"
TASKS_FOLDER = "tasks"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(TASKS_FOLDER, exist_ok=True)

def convert_video(task_id, input_url):
    input_ext = input_url.split("?")[0].split("/")[-1].split(".")[-1].lower()
    input_filename = os.path.join(UPLOAD_FOLDER, f"{task_id}.{input_ext}")
    output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    result_path = os.path.join(TASKS_FOLDER, f"{task_id}.json")

    # Step 1: Download file
    curl_result = subprocess.run(["curl", "-L", input_url, "-o", input_filename])
    if curl_result.returncode != 0 or not os.path.exists(input_filename) or os.path.getsize(input_filename) < 1024:
        with open(result_path, "w") as f:
            json.dump({"status": "error", "reason": "Download failed"}, f)
        return

    # Step 2: Convert to mp4
    ffmpeg_result = subprocess.run([
        "ffmpeg", "-y", "-i", input_filename,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac", output_filename
    ])

    if ffmpeg_result.returncode != 0 or not os.path.exists(output_filename):
        with open(result_path, "w") as f:
            json.dump({"status": "error", "reason": "Conversion failed"}, f)
        return

    # Step 3: Save conversion result
    with open(result_path, "w") as f:
        json.dump({"status": "done", "url": f"/result/{task_id}"}, f)

@app.route("/convert", methods=["POST"])
def start_conversion():
    data = request.get_json()
    input_url = data.get("url")
    if not input_url or not input_url.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg")):
        return jsonify({"error": "Missing or unsupported URL"}), 400

    task_id = str(uuid.uuid4())
    threading.Thread(target=convert_video, args=(task_id, input_url)).start()

    return jsonify({"status": "started", "task_id": task_id})

@app.route("/status/<task_id>", methods=["GET"])
def check_status(task_id):
    result_path = os.path.join(TASKS_FOLDER, f"{task_id}.json")
    if not os.path.exists(result_path):
        return jsonify({"status": "processing"})
    with open(result_path) as f:
        return jsonify(json.load(f))

@app.route("/result/<task_id>", methods=["GET"])
def get_result(task_id):
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    if not os.path.exists(output_path):
        return jsonify({"status": "processing"})
    return send_from_directory(OUTPUT_FOLDER, f"{task_id}.mp4", as_attachment=True, download_name="video.mp4")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
