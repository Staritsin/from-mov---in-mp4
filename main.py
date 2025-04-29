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
    input_ext = input_url.split('.')[-1].split('?')[0].lower()
    input_filename = os.path.join(UPLOAD_FOLDER, f"{task_id}.{input_ext}")
    output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    result_path = os.path.join(TASKS_FOLDER, f"{task_id}.json")

    # Step 1: Download file
    subprocess.run(["curl", "-L", input_url, "-o", input_filename])

    # Step 2: Convert to .mp4 using ffmpeg
    subprocess.run([
        "ffmpeg", "-y", "-i", input_filename,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac", output_filename
    ])

    # Step 3: Save result info as JSON (manually)
    with open(result_path, "w") as f:
        json.dump({
            "status": "done",
            "url": f"/result/{task_id}"
        }, f)

@app.route("/convert", methods=["POST"])
def start_conversion():
    data = request.get_json()
    input_url = data.get("url")
    if not input_url:
        return jsonify({"error": "Missing URL"}), 400

    task_id = str(uuid.uuid4())
    threading.Thread(target=convert_video, args=(task_id, input_url)).start()

    return jsonify({"status": "started", "task_id": task_id})

@app.route("/status/<task_id>", methods=["GET"])
def check_status(task_id):
    result_path = os.path.join(TASKS_FOLDER, f"{task_id}.json")
    if os.path.exists(result_path):
        with open(result_path) as f:
            return jsonify(json.load(f))
    else:
        return jsonify({"status": "processing"})

@app.route("/result/<task_id>", methods=["GET"])
def get_result(task_id):
    output_file = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    if not os.path.exists(output_file):
        return jsonify({"error": "File not ready"}), 404
    return send_from_directory(OUTPUT_FOLDER, f"{task_id}.mp4")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
