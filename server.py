#!/usr/bin/env python3
"""Paste a video link, get vertical clips.

Setup (once):   pip install yt-dlp        and install ffmpeg (https://ffmpeg.org)
Run:            python server.py          then open http://localhost:8000
Only use videos you own or have permission to clip.
Optional: set YT_BROWSER=chrome (or firefox, edge) if YouTube asks for a sign-in check.
"""
import array, json, os, re, shutil, subprocess, sys, threading, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'clips')
JOBS = {}
FF = shutil.which('ffmpeg')
if not FF:
    try:
        import imageio_ffmpeg
        FF = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit('ffmpeg not found. Install it from https://ffmpeg.org and try again.')


def fetch(url, d):
    """Download the video into folder d and return its path."""
    cmd = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '-f', 'bv*[height<=720]+ba/b[height<=720]/b',
           '--merge-output-format', 'mp4', '-o', os.path.join(d, 'src.%(ext)s')]
    if os.environ.get('YT_BROWSER'):
        cmd += ['--cookies-from-browser', os.environ['YT_BROWSER']]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    src = next((os.path.join(d, f) for f in os.listdir(d) if f.startswith('src.')), None)
    if r.returncode or not src:
        raise RuntimeError((r.stderr.strip().splitlines() or ['Download failed.'])[-1][:300])
    return src


def duration(src):
    r = subprocess.run([FF, '-i', src], capture_output=True, text=True)
    m = re.search(r'Duration: (\d+):(\d+):(\d+)', r.stderr)
    if not m:
        raise RuntimeError('Could not read the video length.')
    h, mi, s = map(int, m.groups())
    return h * 3600 + mi * 60 + s


def energy(src):
    """Loudness per second of audio (higher = more energetic)."""
    r = subprocess.run([FF, '-v', 'error', '-i', src, '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'],
                       capture_output=True)
    a = array.array('h')
    a.frombytes(r.stdout[:len(r.stdout) // 2 * 2])
    return [sum(x * x for x in a[i:i + 8000:4]) for i in range(0, len(a) - 7999, 8000)]


def pick(e, L, N):
    sc = [sum(e[s:s + L]) for s in range(len(e) - L + 1)]
    out = []
    for _ in range(N):
        c = [(v, s) for s, v in enumerate(sc) if all(abs(s - p) >= L for p in out)]
        if not c:
            break
        out.append(max(c)[1])
    return sorted(out)


def cut(src, s, L, dst):
    vf = "crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)',scale=720:1280,setsar=1"
    subprocess.run([FF, '-y', '-v', 'error', '-ss', str(s), '-i', src, '-t', str(L), '-vf', vf,
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast', '-crf', '23', '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', '+faststart', dst], check=True)


def work(j, url, L, N):
    J, d = JOBS[j], os.path.join(OUT, j)
    os.makedirs(d, exist_ok=True)
    try:
        J.update(msg='Downloading the video…', p=.05)
        src = fetch(url, d)
        J.update(msg='Finding the best moments…', p=.3)
        e = energy(src)
        D = len(e) or duration(src)
        L = max(5, min(L, D))
        N = min(N, max(1, D // L))
        starts = pick(e, L, N) if len(e) >= L else [k * (D - L) // max(1, N - 1) for k in range(N)]
        for i, s in enumerate(starts):
            J.update(msg=f'Cutting clip {i + 1} of {len(starts)}…', p=.35 + .65 * i / len(starts))
            cut(src, s, L, os.path.join(d, f'clip-{i + 1}.mp4'))
            J['clips'].append({'s': s, 'l': L, 'url': f'/clips/{j}/clip-{i + 1}.mp4'})
        os.remove(src)
        J.update(st='done', p=1, msg=f'Done. {len(starts)} clips are ready to download.')
    except Exception as x:
        J.update(st='err', msg=str(x) or 'Something went wrong.')


class H(BaseHTTPRequestHandler):
    def reply(s, code, body, ct='application/json', extra=None):
        s.send_response(code)
        s.send_header('Content-Type', ct)
        s.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            s.send_header(k, v)
        s.end_headers()
        s.wfile.write(body)

    def do_GET(s):
        u = urlparse(s.path)
        if u.path in ('/', '/index.html'):
            return s.reply(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
        if u.path == '/api/ping':
            return s.reply(200, b'{"ok":true}')
        if u.path == '/api/job':
            J = JOBS.get(parse_qs(u.query).get('id', [''])[0])
            return s.reply(200 if J else 404, json.dumps(J or {}).encode())
        m = re.fullmatch(r'/clips/([a-f0-9]{8})/(clip-\d+\.mp4)', u.path)
        if m and os.path.exists(f := os.path.join(OUT, *m.groups())):
            return s.reply(200, open(f, 'rb').read(), 'video/mp4', {'Content-Disposition': f'inline; filename="{m[2]}"'})
        s.reply(404, b'{}')

    def do_POST(s):
        if s.path != '/api/start':
            return s.reply(404, b'{}')
        b = json.loads(s.rfile.read(int(s.headers.get('Content-Length', 0))) or b'{}')
        url = str(b.get('url', ''))
        if not re.match(r'https?://', url):
            return s.reply(400, b'{"msg":"Enter a valid link."}')
        j = uuid.uuid4().hex[:8]
        JOBS[j] = {'st': 'work', 'msg': 'Starting…', 'p': 0, 'clips': []}
        threading.Thread(target=work, args=(j, url, int(b.get('len', 30)), int(b.get('n', 5))), daemon=True).start()
        s.reply(200, json.dumps({'id': j}).encode())

    def log_message(s, *a):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    print(f'Open http://localhost:{port}  (Ctrl+C to stop)')
    ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
