from flask import Flask, request, jsonify, send_file
import os
import uuid
import threading
import subprocess

app = Flask(__name__)

UPLOAD_FOLDER = "downloads"
OUTPUT_FOLDER = "converted"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

def convert_video(task_id, input_url):
    input_path = os.path.join(UPLOAD_FOLDER, f"{task_id}.input")
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")

    # Скачиваем видео по ссылке
    subprocess.run(["curl", "-L", input_url, "-o", input_path], check=True)

    # Конвертируем в mp4 с флагом faststart
    subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ], check=True)

@app.route("/convert", methods=["POST"])
def start_conversion():
    data = request.get_json()
    input_url = data.get("url")
    if not input_url:
        return jsonify({"error": "Missing URL"}), 400

    task_id = str(uuid.uuid4())
    threading.Thread(target=convert_video, args=(task_id, input_url)).start()
    return jsonify({"status": "started", "task_id": task_id})

@app.route("/result/<task_id>", methods=["GET"])
def get_result(task_id):
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")

    if not os.path.exists(output_path):
        return jsonify({"status": "processing"})

    if request.args.get("raw") == "true":
        return send_file(output_path, mimetype="video/mp4", as_attachment=False)

        return jsonify({
            "status": "done",
            "url": f"https://from-mov-in-mp4.onrender.com/result/{task_id}?raw=true"
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=False, host="0.0.0.0", port=port)
