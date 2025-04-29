from flask import Flask, request, jsonify, send_file
import os
import uuid
import subprocess

app = Flask(__name__)

UPLOAD_FOLDER = "downloads"
OUTPUT_FOLDER = "converted"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Кэш задач
task_status = {}

def convert_video(task_id, input_url):
    try:
        input_path = os.path.join(UPLOAD_FOLDER, f"{task_id}.mov")
        output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
        task_status[task_id] = "processing"

        # Скачиваем файл
        subprocess.run(["curl", "-L", input_url, "-o", input_path], check=True)

        # Конвертация
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac",
            "-movflags", "+faststart",
            output_path
        ], check=True)

        task_status[task_id] = "done"

    except Exception as e:
        task_status[task_id] = "error"

@app.route("/convert", methods=["POST"])
def start_conversion():
    data = request.get_json()
    input_url = data.get("url")
    if not input_url:
        return jsonify({"error": "Missing URL"}), 400

    task_id = str(uuid.uuid4())
    task_status[task_id] = "queued"

    from threading import Thread
    Thread(target=convert_video, args=(task_id, input_url)).start()

    return jsonify({"status": "started", "task_id": task_id})

@app.route("/result/<task_id>", methods=["GET"])
def get_result(task_id):
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id}.mp4")
    status = task_status.get(task_id, "processing")

    # Вернуть готовый mp4
    if request.args.get("raw") == "true":
        if os.path.exists(output_path):
            return send_file(output_path, mimetype="video/mp4", as_attachment=False)
        else:
            return "File not ready", 404

    # Вернуть статус
    if os.path.exists(output_path):
        return jsonify({
            "status": "done",
            "url": f"/result/{task_id}?raw=true",
            "fileSize": os.path.getsize(output_path)
        })
    else:
        return jsonify({"status": status})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=False, host="0.0.0.0", port=port)
