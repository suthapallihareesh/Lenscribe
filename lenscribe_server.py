#!/usr/bin/env python3
"""Lenscribe local server.  Setup (once):  py -m pip install -U yt-dlp
Run:  py lenscribe_server.py   then open  http://localhost:8000
Optional: ffmpeg on PATH (merges HD video+audio), LENS_COOKIES=chrome (helps Instagram/private-ish links)."""
import glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(tempfile.gettempdir(), 'lenscribe'); os.makedirs(TMP, exist_ok=True)
PORT = int(os.environ.get('PORT', 8000))
FF = shutil.which('ffmpeg')
YT = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-warnings', '-q']
if os.environ.get('LENS_COOKIES'): YT += ['--cookies-from-browser', os.environ['LENS_COOKIES']]

def run(args, timeout=600):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def last_err(r, default):
    lines = (r.stderr or '').strip().splitlines()
    return lines[-1] if lines else default

def key(s): return hashlib.sha1(s.encode()).hexdigest()[:12]

def info(u):
    r = run(YT + ['-J', u], 120)
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
    r = run(YT + ['-f', fmt, '-o', out + '.%(ext)s'] + (['--merge-output-format', 'mp4'] if FF else []) + [u], 1800)
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
    d = json.load(urllib.request.urlopen(req, timeout=300))
    return '\n'.join(f"[{int(s['start']) // 60}:{int(s['start']) % 60:02d}] {s['text'].strip()}" for s in d.get('segments', [])) or d.get('text', '')

def transcript(u, gk):
    d = os.path.join(TMP, 't' + key(u)); os.makedirs(d, exist_ok=True)
    if not glob.glob(d + '/*.vtt'):
        run(YT + ['--skip-download', '--write-subs', '--write-auto-subs', '--sub-langs', 'en.*,en,hi.*', '--sub-format', 'vtt', '-o', d + '/s', u], 240)
    f = sorted(glob.glob(d + '/*.vtt'))
    if f:
        t = vtt(open(f[0], encoding='utf8', errors='ignore').read())
        if t: return {'text': t, 'source': 'captions'}
    if not gk: return {'text': '', 'note': 'No captions on this video. Add a Groq key in Lenscribe Settings to transcribe the audio.'}
    if not glob.glob(d + '/a.*'):
        run(YT + ['-f', 'ba[filesize<24M]/ba[filesize_approx<24M]/worst', '-o', d + '/a.%(ext)s', u], 900)
    a = [x for x in glob.glob(d + '/a.*') if not x.endswith('.part')]
    if not a: return {'text': '', 'note': 'Could not fetch the audio.'}
    if os.path.getsize(a[0]) > 25e6: return {'text': '', 'note': 'Audio is over Groq\'s 25 MB limit.'}
    return {'text': groq(a[0], gk), 'source': 'groq-whisper'}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(p.query); u = (q.get('url') or [''])[0]
        try:
            if p.path in ('/', '/index.html', '/lenscribe.html'):
                b = open(os.path.join(HERE, 'lenscribe.html'), 'rb').read()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b); return
            if p.path == '/api/ping': return self.send_json(200, {'ok': True, 'ffmpeg': bool(FF)})
            if not re.match(r'https?://', u): return self.send_json(400, {'error': 'bad url'})
            if p.path == '/api/info': return self.send_json(200, info(u))
            if p.path == '/api/transcript':
                return self.send_json(200, transcript(u, self.headers.get('X-Groq-Key') or os.environ.get('GROQ_API_KEY')))
            if p.path == '/api/download':
                f = download(u, (q.get('q') or ['720'])[0])
                self.send_response(200); self.send_header('Content-Type', 'video/mp4' if f.endswith('mp4') else 'application/octet-stream')
                self.send_header('Content-Length', str(os.path.getsize(f)))
                self.send_header('Content-Disposition', f'attachment; filename="lenscribe_{key(u)}{os.path.splitext(f)[1]}"'); self.end_headers()
                with open(f, 'rb') as fh: shutil.copyfileobj(fh, self.wfile)
                return
            self.send_json(404, {'error': 'not found'})
        except Exception as e:
            try: self.send_json(500, {'error': str(e)})
            except Exception: pass

if __name__ == '__main__':
    print(f'Lenscribe running: http://localhost:{PORT}   (ffmpeg: {"yes" if FF else "no, HD merge and frames from long videos limited"})')
    threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
