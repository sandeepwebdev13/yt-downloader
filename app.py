import os
import sys
import uuid
import json
import threading
import urllib.request
import zipfile
import shutil
from flask import Flask, render_template, request, jsonify, Response, send_file
import yt_dlp

# ---------- AUTO DOWNLOAD FFMPEG (Windows) ----------
def ensure_ffmpeg():
    """Check if ffmpeg is available; if not, download portable version to project folder."""
    # First, check if already in PATH
    if shutil.which("ffmpeg"):
        return True

    # Check if we already have it locally
    local_ffmpeg = os.path.join(os.path.dirname(__file__), "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        os.environ["PATH"] = os.path.dirname(local_ffmpeg) + os.pathsep + os.environ["PATH"]
        return True

    print("FFmpeg not found. Downloading portable version automatically...")
    # Download from gyan.dev (trusted FFmpeg builds)
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    zip_path = os.path.join(os.path.dirname(__file__), "ffmpeg.zip")

    try:
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Find the bin folder inside the extracted zip
            for member in zip_ref.namelist():
                if member.endswith("ffmpeg.exe"):
                    # Extract ffmpeg.exe to project root
                    with zip_ref.open(member) as source, open(local_ffmpeg, "wb") as target:
                        shutil.copyfileobj(source, target)
                    break
        os.remove(zip_path)
        print("FFmpeg downloaded successfully.")
        os.environ["PATH"] = os.path.dirname(local_ffmpeg) + os.pathsep + os.environ["PATH"]
        return True
    except Exception as e:
        print(f"Auto-download failed: {e}")
        print("You can manually place ffmpeg.exe in this folder or install FFmpeg globally.")
        return False

if not ensure_ffmpeg():
    print("\n⚠️  Warning: FFmpeg could not be downloaded. Audio conversion may fail.")
    print("   You can still download videos without audio merging.\n")

# ---------- FLASK APP ----------
app = Flask(__name__)
app.config['DOWNLOAD_FOLDER'] = 'downloads'
os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)

# In‑memory store for download progress
downloads = {}
progress_hooks = {}

def progress_hook(d):
    if d['status'] == 'downloading':
        download_id = d['info_dict'].get('__download_id')
        if download_id:
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total else 0
            progress_hooks[download_id] = {
                'status': 'downloading',
                'percent': round(percent, 1),
                'speed': d.get('_speed_str', ''),
                'eta': d.get('_eta_str', '')
            }
    elif d['status'] == 'finished':
        download_id = d['info_dict'].get('__download_id')
        if download_id:
            progress_hooks[download_id] = {'status': 'processing', 'percent': 100}
            if download_id in downloads:
                downloads[download_id]['status'] = 'processing'
                downloads[download_id]['progress'] = 100

def download_worker(download_id, url, format_choice):
    try:
        ydl_opts = {
            'outtmpl': os.path.join(app.config['DOWNLOAD_FOLDER'], f'{download_id}_%(title)s.%(ext)s'),
            'progress_hooks': [progress_hook],
            'quiet': True,
            'no_warnings': True,
        }

        if format_choice == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            height = format_choice.replace('p', '')
            ydl_opts.update({
                'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]',
                'merge_output_format': 'mp4',
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info['__download_id'] = download_id
            downloads[download_id]['title'] = info.get('title', 'video')
            ydl.process_info(info)

            filename = ydl.prepare_filename(info)
            if format_choice == 'audio':
                filename = filename.rsplit('.', 1)[0] + '.mp3'
            elif not filename.endswith('.mp4'):
                filename += '.mp4'

            downloads[download_id]['status'] = 'completed'
            downloads[download_id]['filename'] = os.path.basename(filename)
            downloads[download_id]['filepath'] = filename
            progress_hooks[download_id] = {'status': 'completed', 'percent': 100}

    except Exception as e:
        downloads[download_id]['status'] = 'error'
        downloads[download_id]['error'] = str(e)
        progress_hooks[download_id] = {'status': 'error', 'error': str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = []
            seen = set()
            for f in info.get('formats', []):
                h = f.get('height')
                if h and h not in seen and f.get('vcodec') != 'none':
                    formats.append({'id': f"{h}p", 'label': f"{h}p", 'height': h})
                    seen.add(h)
            formats.sort(key=lambda x: x['height'])
            formats.append({'id': 'audio', 'label': 'Audio (MP3)'})
            return jsonify({
                'title': info.get('title'),
                'thumbnail': info.get('thumbnail'),
                'duration': info.get('duration', 0),
                'formats': formats
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def start_download():
    data = request.get_json()
    url = data.get('url')
    fmt = data.get('format')
    if not url or not fmt:
        return jsonify({'error': 'URL and format required'}), 400
    download_id = str(uuid.uuid4())
    downloads[download_id] = {'status': 'starting', 'progress': 0}
    progress_hooks[download_id] = {'status': 'starting', 'percent': 0}
    threading.Thread(target=download_worker, args=(download_id, url, fmt)).start()
    return jsonify({'download_id': download_id})

@app.route('/progress/<download_id>')
def progress_stream(download_id):
    def generate():
        if download_id not in downloads:
            yield f"data: {json.dumps({'error': 'Invalid ID'})}\n\n"
            return
        last = -1
        while True:
            hook = progress_hooks.get(download_id, {})
            status = hook.get('status', downloads[download_id]['status'])
            percent = hook.get('percent', 0)
            if percent != last or status in ('completed', 'error'):
                yield f"data: {json.dumps({'status': status, 'percent': percent, 'error': hook.get('error')})}\n\n"
                last = percent
            if status in ('completed', 'error'):
                break
            import time; time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/file/<download_id>')
def serve_file(download_id):
    if download_id not in downloads or downloads[download_id]['status'] != 'completed':
        return jsonify({'error': 'File not ready'}), 404
    path = downloads[download_id]['filepath']
    title = downloads[download_id]['title']
    ext = os.path.splitext(path)[1]
    safe = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
    return send_file(path, as_attachment=True, download_name=f"{safe}{ext}")

if __name__ == '__main__':
    app.run(debug=True, threaded=True)