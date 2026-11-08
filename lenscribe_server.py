#!/usr/bin/env python3
"""Lenscribe local server.  Setup (once):  py -m pip install -U yt-dlp
Run:  py lenscribe_server.py   then open  http://localhost:8000
Optional: ffmpeg on PATH (merges HD video+audio), LENS_COOKIES=chrome (helps Instagram/private-ish links)."""
import glob, hashlib, hmac, importlib.util, ipaddress, json, os, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(tempfile.gettempdir(), 'lenscribe'); os.makedirs(TMP, exist_ok=True)
PORT = int(os.environ.get('PORT', 8000))
TOKEN = secrets.token_urlsafe(24)          # per-run secret; only the page we serve knows it
SEM = threading.BoundedSemaphore(2)        # at most 2 heavy yt-dlp jobs at once
FF = shutil.which('ffmpeg')
def find_js():
    ext = '.exe' if os.name == 'nt' else ''
    for c in (shutil.which('deno'), os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Links', 'deno.exe'),
              os.path.join(os.path.expanduser('~'), '.deno', 'bin', 'deno' + ext)):
        if c and os.path.isfile(c): return 'deno', c
    n = shutil.which('node')
    return ('node', n) if n else (None, None)
JS_NAME, JS_PATH = find_js()
YT = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-warnings', '-q', '--max-filesize', '2G']
if os.environ.get('LENS_REMOTE_EJS'): YT += ['--remote-components', 'ejs:github']   # off by default: fetches code from GitHub
if JS_NAME: YT += ['--js-runtimes', f'{JS_NAME}:{JS_PATH}']
if os.environ.get('LENS_COOKIES'): YT += ['--cookies-from-browser', os.environ['LENS_COOKIES']]

def run(args, timeout=600):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)  # nosec B603 - fixed argv list, no shell; the URL goes after '--' and was validated

def last_err(r, default):
    lines = (r.stderr or '').strip().splitlines()
    return lines[-1] if lines else default

def key(s): return hashlib.sha1(s.encode(), usedforsecurity=False).hexdigest()[:12]

def diag():
    r = run(YT[:3] + ['--version'], 30)
    return {'ytdlp': r.stdout.strip() or ('missing: ' + last_err(r, '?')), 'ffmpeg': bool(FF), 'js': JS_NAME, 'python': sys.version.split()[0], 'ejs': importlib.util.find_spec('yt_dlp_ejs') is not None}

def info(u):
    r = run(YT + ['-J', '--', u], 120)
    if r.returncode: raise RuntimeError(last_err(r, 'yt-dlp could not read this link'))
    d = json.loads(r.stdout)
    return {'title': d.get('title'), 'uploader': d.get('uploader'), 'duration': d.get('duration'),
            'thumbnail': d.get('thumbnail'), 'site': d.get('extractor_key'),
            'has_subs': bool(d.get('subtitles') or d.get('automatic_captions'))}

def media_files(prefix):
    return [f for f in glob.glob(prefix + '.*') if not f.endswith(('.part', '.vtt', '.json', '.ytdl'))]

def download(u, q):
    q = re.sub(r'\D', '', q) or '720'
    out = os.path.join(TMP, key(u + q))
    if media_files(out): return media_files(out)[0]
    fmt = f'bv*[height<={q}]+ba/b[height<={q}]/b' if FF else f'b[ext=mp4][height<={q}]/b[height<={q}]/b'
    r = run(YT + ['-f', fmt, '-o', out + '.%(ext)s'] + (['--merge-output-format', 'mp4'] if FF else []) + ['--', u], 1800)
    f = media_files(out)
    if not f: raise RuntimeError(last_err(r, 'download failed'))
    return f[0]

def vtt(t):
    out, last = [], ''
    for b in re.split(r'\n\n+', t):
        m = re.search(r'(?:(\d+):)?(\d+):(\d+)[.,]\d+\s+-->', b)
        if not m: continue
        x = ' '.join(l for l in b.split('\n') if '-->' not in l and not l.startswith(('WEBVTT', 'Kind:', 'Language:')) and not l.strip().isdigit())
        x = re.sub(r'<[^>]+>', '', x).strip()
        if not x or x == last or last.endswith(x): continue
        last = x
        out.append(f"[{int(m.group(1) or 0) * 60 + int(m.group(2))}:{m.group(3)}] {x}")
    return '\n'.join(out)

def groq(path, k):
    b = '----lens'; name = os.path.basename(path); body = b''
    for n, v in (('model', 'whisper-large-v3-turbo'), ('response_format', 'verbose_json')):
        body += f'--{b}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n').encode() + open(path, 'rb').read() + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request('https://api.groq.com/openai/v1/audio/transcriptions', body,
        {'Authorization': 'Bearer ' + k, 'Content-Type': 'multipart/form-data; boundary=' + b, 'User-Agent': 'Mozilla/5.0'})
    d = json.load(urllib.request.urlopen(req, timeout=300))  # nosec B310 - constant https://api.groq.com URL
    return '\n'.join(f"[{int(s['start']) // 60}:{int(s['start']) % 60:02d}] {s['text'].strip()}" for s in d.get('segments', [])) or d.get('text', '')

def transcript(u, gk):
    d = os.path.join(TMP, 't' + key(u)); os.makedirs(d, exist_ok=True)
    if not glob.glob(d + '/*.vtt'):
        run(YT + ['--skip-download', '--write-subs', '--write-auto-subs', '--sub-langs', 'en.*,en,hi.*', '--sub-format', 'vtt', '-o', d + '/s', '--', u], 240)
    f = sorted(glob.glob(d + '/*.vtt'))
    if f:
        t = vtt(open(f[0], encoding='utf8', errors='ignore').read())
        if t: return {'text': t, 'source': 'captions'}
    if not gk: return {'text': '', 'note': 'No captions on this video. Add a Groq key in Lenscribe Settings to transcribe the audio.'}
    if not glob.glob(d + '/a.*'):
        run(YT + ['-f', 'ba[filesize<24M]/ba[filesize_approx<24M]/worst', '-o', d + '/a.%(ext)s', '--', u], 900)
    a = [x for x in glob.glob(d + '/a.*') if not x.endswith('.part')]
    if not a: return {'text': '', 'note': 'Could not fetch the audio.'}
    if os.path.getsize(a[0]) > 25e6: return {'text': '', 'note': 'Audio is over Groq\'s 25 MB limit.'}
    return {'text': groq(a[0], gk), 'source': 'groq-whisper'}

LOCAL_HOSTS = {f'localhost:{PORT}', f'127.0.0.1:{PORT}'}   # blocks DNS-rebinding
URL_RE = re.compile(r'^https?://[^\s\x00-\x1f]{3,2000}$')
HEAVY = {'/api/info', '/api/transcript', '/api/prepare', '/api/download'}

def public_url(u):
    """Only public http(s) links: blocks file://, option-like input, and LAN/localhost targets (SSRF)."""
    if not URL_RE.match(u): return False
    if os.environ.get('LENS_ALLOW_LOCAL'): return True
    try:
        for a in socket.getaddrinfo(urllib.parse.urlparse(u).hostname or '', None):
            ip = ipaddress.ip_address(a[4][0].split('%')[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return False
    except Exception:
        return False
    return True

def cleanup(max_age=7200):
    now = time.time()
    for n in os.listdir(TMP):
        p = os.path.join(TMP, n)
        try:
            if now - os.path.getmtime(p) > max_age: shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError: pass

class H(BaseHTTPRequestHandler):
    server_version = 'Lenscribe'; sys_version = ''
    def log_message(self, *a): pass
    def _hdr(self):
        for k, v in (('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'), ('Cache-Control', 'no-store'),
                     ('Content-Security-Policy', "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")):
            self.send_header(k, v)
    def send_json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b)))
        self._hdr(); self.end_headers(); self.wfile.write(b)
    def authorized(self, q, api):
        if self.headers.get('Host', '') not in LOCAL_HOSTS: return False
        if not api: return True
        if self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'): return False
        tok = self.headers.get('X-Lens-Token') or (q.get('t') or [''])[0]
        return hmac.compare_digest(tok.encode(), TOKEN.encode())
    def do_GET(self):
        p = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(p.query); api = p.path.startswith('/api/')
        if not self.authorized(q, api): return self.send_json(403, {'error': 'forbidden'})
        try:
            if p.path in ('/', '/index.html', '/lenscribe.html'):
                b = open(os.path.join(HERE, 'lenscribe.html'), 'rb').read().replace(b'__LENS_TOKEN__', TOKEN.encode())
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(b)))
                self._hdr(); self.end_headers(); self.wfile.write(b); return
            if p.path == '/api/ping': return self.send_json(200, {'ok': True, 'ffmpeg': bool(FF)})
            if p.path == '/api/diag': return self.send_json(200, diag())
            if p.path not in HEAVY: return self.send_json(404, {'error': 'not found'})
            u = (q.get('url') or [''])[0]
            if not public_url(u): return self.send_json(400, {'error': 'That link is not allowed. Use a public http(s) video link.'})
            if not SEM.acquire(timeout=600): return self.send_json(503, {'error': 'Server is busy, try again.'})
            try:
                cleanup()
                if p.path == '/api/info': return self.send_json(200, info(u))
                if p.path == '/api/transcript':
                    gk = self.headers.get('X-Groq-Key') or os.environ.get('GROQ_API_KEY')
                    return self.send_json(200, transcript(u, gk if gk and re.match(r'^[\w-]{10,200}$', gk) else None))
                f = download(u, (q.get('q') or ['720'])[0])
            finally:
                SEM.release()
            size = os.path.getsize(f)
            if p.path == '/api/prepare': return self.send_json(200, {'ok': True, 'size': size})
            start, end, code = 0, size - 1, 200
            m = re.match(r'^bytes=(\d*)-(\d*)$', self.headers.get('Range') or '')
            if m and (m.group(1) or m.group(2)):
                if m.group(1): start, end = int(m.group(1)), (int(m.group(2)) if m.group(2) else size - 1)
                else: start = max(0, size - int(m.group(2)))
                end = min(end, size - 1); code = 206
                if start > end: return self.send_json(416, {'error': 'bad range'})
            ext = re.sub(r'[^A-Za-z0-9.]', '', os.path.splitext(f)[1])[:6]
            self.send_response(code); self.send_header('Content-Type', 'video/mp4' if ext == '.mp4' else 'application/octet-stream')
            self.send_header('Accept-Ranges', 'bytes'); self.send_header('Content-Length', str(end - start + 1))
            if code == 206: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Disposition', f'attachment; filename="lenscribe_{key(u)}{ext}"'); self._hdr(); self.end_headers()
            with open(f, 'rb') as fh:
                fh.seek(start); left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk: break
                    self.wfile.write(chunk); left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try: self.send_json(500, {'error': str(e)[:300]})
            except Exception: pass

if __name__ == '__main__':
    cleanup()
    print(f'Lenscribe running: http://localhost:{PORT}   ffmpeg: {"yes" if FF else "no"}   JS runtime for YouTube: {JS_NAME or "NONE (install Deno)"}')
    threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
