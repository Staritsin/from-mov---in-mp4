
from flask import Flask, request, jsonify
import requests
import subprocess
import uuid
import os

app = Flask(__name__)
UPLOAD_FOLDER = "downloads"
OUTPUT_FOLDER = "converted"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

@app.route("/convert", methods=["POST"])
def convert_video():
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        file_ext = url.split('.')[-1]
        input_filename = f"{uuid.uuid4()}.{file_ext}"
        input_path = os.path.join(UPLOAD_FOLDER, input_filename)

        # Скачиваем видео
        r = requests.get(url)
        with open(input_path, "wb") as f:
            f.write(r.content)

        # Конвертируем в mp4
        output_filename = f"{uuid.uuid4()}.mp4"
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)

        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            output_path
        ]
        subprocess.run(cmd, check=True)

        return jsonify({"mp4_url": f"/converted/{output_filename}"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/converted/<filename>")
def serve_file(filename):
    return app.send_static_file(os.path.join("converted", filename))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
