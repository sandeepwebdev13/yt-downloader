import os
import uuid
import json
import threading
from flask import Flask, render_template, request, jsonify, Response, send_file
import yt_dlp

app = Flask(__name__)
app.config['DOWNLOAD_FOLDER'] = 'downloads'
os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)

# In-memory store for download progress and metadata
downloads = {}
progress_hooks = {}

def progress_hook(d):
    """yt-dlp progress hook, updates global state."""
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
    """Background thread to handle the actual download."""
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
            else:
                if not filename.endswith('.mp4'):
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
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            formats = []
            seen_heights = set()
            for f in info.get('formats', []):
                height = f.get('height')
                if height and height not in seen_heights and f.get('vcodec') != 'none':
                    formats.append({
                        'id': f"{height}p",
                        'label': f"{height}p",
                        'height': height
                    })
                    seen_heights.add(height)

            formats.sort(key=lambda x: x['height'])
            formats.append({'id': 'audio', 'label': 'Audio (MP3)'})

            return jsonify({
                'title': info.get('title', 'Unknown'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration', 0),
                'formats': formats
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def start_download():
    data = request.get_json()
    url = data.get('url')
    format_choice = data.get('format')
    if not url or not format_choice:
        return jsonify({'error': 'URL and format are required'}), 400

    download_id = str(uuid.uuid4())
    downloads[download_id] = {
        'status': 'starting',
        'progress': 0,
        'filename': None,
        'error': None,
    }
    progress_hooks[download_id] = {'status': 'starting', 'percent': 0}

    thread = threading.Thread(target=download_worker, args=(download_id, url, format_choice))
    thread.start()

    return jsonify({'download_id': download_id})

@app.route('/progress/<download_id>')
def progress_stream(download_id):
    def generate():
        if download_id not in downloads:
            yield f"data: {json.dumps({'error': 'Invalid download ID'})}\n\n"
            return

        last_percent = -1
        while True:
            hook_data = progress_hooks.get(download_id, {})
            status = hook_data.get('status', downloads[download_id]['status'])
            percent = hook_data.get('percent', downloads[download_id]['progress'])

            if percent != last_percent or status in ('completed', 'error'):
                yield f"data: {json.dumps({'status': status, 'percent': percent, 'error': hook_data.get('error')})}\n\n"
                last_percent = percent

            if status in ('completed', 'error'):
                break

            import time
            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream')

@app.route('/file/<download_id>')
def serve_file(download_id):
    if download_id not in downloads or downloads[download_id]['status'] != 'completed':
        return jsonify({'error': 'File not ready'}), 404

    filepath = downloads[download_id]['filepath']
    title = downloads[download_id]['title']
    ext = os.path.splitext(filepath)[1]
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
    download_name = f"{safe_title}{ext}"

    def cleanup():
        try:
            os.remove(filepath)
            del downloads[download_id]
            del progress_hooks[download_id]
        except:
            pass

    return send_file(
        filepath,
        as_attachment=True,
        download_name=download_name,
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
