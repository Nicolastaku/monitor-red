# app.py
from flask import Flask, render_template, jsonify, request, send_file, session, redirect, url_for
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from ping3 import ping
from functools import wraps
from datetime import datetime, timedelta
import json
import os
import socket
import sqlite3
import threading
import time
import logging
import requests
import re
import io
import csv
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ============================================================
# CONFIGURACIÓN
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'monitor.db')

SECRET_KEY = os.getenv('MONITOR_SECRET_KEY', 'cambia-esto-en-produccion')
ADMIN_PASSWORD = os.getenv('MONITOR_ADMIN_PASSWORD', 'admin')

PING_INTERVAL = max(1, int(os.getenv('MONITOR_PING_INTERVAL', '10')))
HISTORY_INTERVAL = max(1, int(os.getenv('MONITOR_HISTORY_INTERVAL', '30')))
HISTORY_RETENTION_DAYS = int(os.getenv('MONITOR_HISTORY_DAYS', '30'))
EVENTS_RETENTION_DAYS = int(os.getenv('MONITOR_EVENTS_DAYS', '30'))
AUDIT_RETENTION_DAYS = int(os.getenv('MONITOR_AUDIT_DAYS', '60'))
PROVIDER_CACHE_TTL = 60 * 60 * 24 * 7
CLEANUP_INTERVAL = 3600
IP_REGEX = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(DATA_DIR, 'monitor.log')),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
CORS(app, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ============================================================
# BASE DE DATOS
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r['name'] == column for r in rows)

def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                group_name TEXT DEFAULT 'default',
                provider TEXT DEFAULT 'Desconocido',
                provider_updated_at TEXT,
                comments TEXT DEFAULT '',
                threshold INTEGER DEFAULT 100,
                alive INTEGER DEFAULT 0,
                last_latency REAL,
                last_check TEXT,
                maintenance INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                latency REAL,
                alive INTEGER NOT NULL,
                FOREIGN KEY (device_ip) REFERENCES devices(ip) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_history_ip_ts ON history(device_ip, timestamp);
            CREATE INDEX IF NOT EXISTS idx_history_ts ON history(timestamp);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                device_ip TEXT,
                device_name TEXT,
                event_type TEXT NOT NULL,
                latency REAL,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_ip ON events(device_ip);

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_ip TEXT,
                action TEXT NOT NULL,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);

            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT DEFAULT '#58a6ff'
            );

            CREATE TABLE IF NOT EXISTS users (
                ip TEXT PRIMARY KEY,
                username TEXT,
                first_seen TEXT,
                last_seen TEXT,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')
        if not column_exists(conn, 'devices', 'hidden'):
            conn.execute("ALTER TABLE devices ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.execute("INSERT OR IGNORE INTO groups (id, name, color) VALUES ('default', 'Por defecto', '#58a6ff')")
    log.info("Base de datos inicializada en %s", DB_PATH)

# ============================================================
# HELPERS
# ============================================================
def now_iso():
    return datetime.now().isoformat()

def valid_ip(ip):
    m = IP_REGEX.match(ip or '')
    if not m:
        return False
    return all(0 <= int(g) <= 255 for g in m.groups())

def log_audit(action, user_ip, details=""):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, user_ip, action, details) VALUES (?, ?, ?, ?)",
                (now_iso(), user_ip, action, details)
            )
    except Exception as e:
        log.error("Error en audit: %s", e)

def save_event(event_type, device_ip=None, device_name=None, latency=None, details=None):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO events (timestamp, device_ip, device_name, event_type, latency, details) VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), device_ip, device_name, event_type, latency, details)
            )
    except Exception as e:
        log.error("Error guardando evento: %s", e)

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else default

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

# ============================================================
# PING Y PROVEEDORES
# ============================================================
def ping_ip(ip, timeout=2, retries=1):
    for attempt in range(retries + 1):
        try:
            r = ping(ip, timeout=timeout)
            if r is not None:
                return round(r * 1000, 2)
            return None
        except Exception:
            if attempt < retries:
                time.sleep(0.1)
                continue
            return None
    return None

PROVIDER_MAP = {
    'claro': 'Claro', 'comcel': 'Claro', 'une': 'UNE', 'epm': 'EPM',
    'etb': 'ETB', 'movistar': 'Movistar', 'telefonica': 'Movistar',
    'tigo': 'Tigo', 'google': 'Google', 'cloudflare': 'Cloudflare',
    'amazon': 'Amazon AWS', 'microsoft': 'Microsoft Azure',
    'akamai': 'Akamai', 'facebook': 'Meta', 'apple': 'Apple',
    'netflix': 'Netflix', 'verizon': 'Verizon', 'comcast': 'Comcast',
}

def clean_provider_name(provider):
    if not provider:
        return 'Desconocido'
    p = provider.lower()
    for key, val in PROVIDER_MAP.items():
        if key in p:
            return val
    p = re.sub(r'^AS\d+\s+', '', provider)
    if '(' in p:
        p = p.split('(')[0].strip()
    if ',' in p:
        p = p.split(',')[0].strip()
    return p.strip() or 'Desconocido'

def is_local_ip(ip):
    return (ip.startswith('192.168.') or ip.startswith('10.') or
            ip.startswith('172.16.') or ip.startswith('127.') or
            ip.startswith('172.17.') or ip.startswith('172.18.') or
            ip.startswith('172.19.') or ip.startswith('172.2') or
            ip.startswith('172.30.') or ip.startswith('172.31.'))

def fetch_provider_from_api(ip):
    if is_local_ip(ip):
        return 'Red Local', False
    try:
        r = requests.get(f'http://ip-api.com/json/{ip}?fields=status,isp,org', timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get('status') == 'success':
                return clean_provider_name(data.get('isp') or data.get('org')), True
    except Exception:
        pass
    return 'Desconocido', False

def get_provider(ip):
    with get_db() as conn:
        row = conn.execute(
            "SELECT provider, provider_updated_at FROM devices WHERE ip = ?", (ip,)
        ).fetchone()
        if row and row['provider'] and row['provider'] != 'Desconocido' and row['provider_updated_at']:
            try:
                updated = datetime.fromisoformat(row['provider_updated_at'])
                if (datetime.now() - updated).total_seconds() < PROVIDER_CACHE_TTL:
                    return row['provider']
            except Exception:
                pass
    provider, ok = fetch_provider_from_api(ip)
    with get_db() as conn:
        if ok:
            conn.execute(
                "UPDATE devices SET provider = ?, provider_updated_at = ? WHERE ip = ?",
                (provider, now_iso(), ip)
            )
        else:
            conn.execute(
                "UPDATE devices SET provider = ? WHERE ip = ?",
                (provider, ip)
            )
    return provider

def get_whois(ip):
    try:
        r = requests.get(
            f'http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as',
            timeout=5
        )
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'success':
                return {
                    'country': d.get('country', 'Desconocido'),
                    'region': d.get('regionName', 'Desconocido'),
                    'city': d.get('city', 'Desconocido'),
                    'isp': d.get('isp', 'Desconocido'),
                    'org': d.get('org', 'Desconocido'),
                    'as': d.get('as', 'Desconocido'),
                }
    except Exception:
        pass
    return None

# ============================================================
# WORKER DE PINGS
# ============================================================
_worker_stop = threading.Event()
_worker_thread = None
_last_state = {}
_last_history_save = {}

def ping_worker():
    log.info("Ping worker iniciado (ping=%ss, history=%ss)", PING_INTERVAL, HISTORY_INTERVAL)
    last_cleanup = 0
    while not _worker_stop.is_set():
        try:
            with get_db() as conn:
                devices = conn.execute(
                    "SELECT ip, name, threshold, maintenance FROM devices"
                ).fetchall()

            current_ips = {d['ip'] for d in devices}
            for ip in list(_last_state.keys()):
                if ip not in current_ips:
                    del _last_state[ip]
            for ip in list(_last_history_save.keys()):
                if ip not in current_ips:
                    del _last_history_save[ip]

            now = time.time()
            for dev in devices:
                ip = dev['ip']
                latency = ping_ip(ip)
                alive = latency is not None
                prev = _last_state.get(ip)

                with get_db() as conn:
                    conn.execute(
                        "UPDATE devices SET alive = ?, last_latency = ?, last_check = ? WHERE ip = ?",
                        (1 if alive else 0, latency, datetime.now().strftime('%H:%M:%S'), ip)
                    )

                last_save = _last_history_save.get(ip, 0)
                if (now - last_save) >= HISTORY_INTERVAL:
                    with get_db() as conn:
                        conn.execute(
                            "INSERT INTO history (device_ip, timestamp, latency, alive) VALUES (?, ?, ?, ?)",
                            (ip, now_iso(), latency, 1 if alive else 0)
                        )
                    _last_history_save[ip] = now

                if prev is not None and prev != alive:
                    if alive:
                        save_event('UP', ip, dev['name'], latency, 'Dispositivo recuperado')
                        log.info("🟢 %s (%s) recuperado", dev['name'], ip)
                    else:
                        save_event('DOWN', ip, dev['name'], None, 'Dispositivo caído')
                        log.info("🔴 %s (%s) caído", dev['name'], ip)
                    socketio.emit('device_update', {
                        'ip': ip,
                        'alive': alive,
                        'last_latency': latency,
                        'last_check': datetime.now().strftime('%H:%M:%S'),
                    })

                if alive and latency and latency > (dev['threshold'] or 100):
                    if not dev['maintenance']:
                        save_event('HIGH_LATENCY', ip, dev['name'], latency,
                                   f'Latencia {latency}ms > {dev["threshold"]}ms')

                _last_state[ip] = alive

            if (now - last_cleanup) >= CLEANUP_INTERVAL:
                cleanup_old_data()
                last_cleanup = now

        except Exception as e:
            log.exception("Error en ping_worker: %s", e)
        _worker_stop.wait(PING_INTERVAL)
    log.info("Ping worker detenido")

def cleanup_old_data():
    now = datetime.now()
    with get_db() as conn:
        cutoff_h = (now - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
        deleted = conn.execute("DELETE FROM history WHERE timestamp < ?", (cutoff_h,)).rowcount
        if deleted > 0:
            log.info("🧹 Limpiados %d registros de history", deleted)

        cutoff_e = (now - timedelta(days=EVENTS_RETENTION_DAYS)).isoformat()
        deleted = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff_e,)).rowcount
        if deleted > 0:
            log.info("🧹 Limpiados %d registros de events", deleted)

        cutoff_a = (now - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
        deleted = conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff_a,)).rowcount
        if deleted > 0:
            log.info("🧹 Limpiados %d registros de audit_log", deleted)

def cleanup_old_history():
    cleanup_old_data()

def start_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=ping_worker, daemon=True)
    _worker_thread.start()

# ============================================================
# AUTENTICACIÓN
# ============================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return f(*args, **kwargs)
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'No autorizado'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        error = 'Contraseña incorrecta'
    return f'''<!DOCTYPE html>
    <html><head><title>Login - Monitor</title>
    <style>
    body {{ font-family: 'Inter', sans-serif; background:#0d1117; color:#e6edf3;
           display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
    .box {{ background:#161b22; padding:40px; border-radius:6px;
            border:1px solid #21262d; width:340px; }}
    h1 {{ color:#e6edf3; font-size:16px; font-weight:600; margin-bottom:24px;
          text-align:center; letter-spacing:0.06em; font-family:'JetBrains Mono', monospace;
          text-transform:uppercase; }}
    h1::before {{ content: '◆ '; color:#58a6ff; }}
    input {{ width:100%; padding:10px 14px; border:1px solid #21262d; border-radius:4px;
             font-size:13px; box-sizing:border-box; margin-bottom:14px; font-family:inherit;
             background:#0d1117; color:#e6edf3; }}
    input:focus {{ outline:none; border-color:#58a6ff; box-shadow:0 0 0 3px rgba(88,166,255,0.15); }}
    button {{ width:100%; padding:11px; background:#58a6ff; color:#0d1117; border:none;
              border-radius:4px; font-size:13px; font-weight:600; cursor:pointer;
              transition:all 0.15s; font-family:inherit; }}
    button:hover {{ background:#79c0ff; }}
    .error {{ color:#f85149; font-size:12px; margin-bottom:12px; text-align:center;
              font-family:'JetBrains Mono', monospace; }}
    </style></head>
    <body><div class="box">
    <h1>Monitor de Red</h1>
    {f'<div class="error">{error}</div>' if error else ''}
    <form method="post">
    <input type="password" name="password" placeholder="Contraseña" autofocus required>
    <button type="submit">Entrar</button>
    </form></div></body></html>'''

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ============================================================
# RUTAS
# ============================================================
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/health', methods=['GET'])
def api_health():
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM devices").fetchone()
    return jsonify({
        'status': 'ok',
        'devices': row['n'],
        'time': now_iso(),
        'uptime_interval': PING_INTERVAL,
        'history_interval': HISTORY_INTERVAL
    })

# ---------- DEVICES ----------
@app.route('/api/devices', methods=['GET'])
@login_required
def api_get_devices():
    with get_db() as conn:
        rows = conn.execute('''
            SELECT ip, name, group_name, provider, comments, threshold,
                   alive, last_latency, last_check, maintenance
            FROM devices ORDER BY name
        ''').fetchall()
    devices = []
    for r in rows:
        d = dict(r)
        d['alive'] = bool(d['alive'])
        d['maintenance'] = bool(d['maintenance'])
        devices.append(d)
    return jsonify(devices)

@app.route('/api/devices', methods=['POST'])
@login_required
def api_add_device():
    data = request.get_json() or {}
    ip = (data.get('ip') or '').strip()
    name = (data.get('name') or ip).strip()
    group = data.get('group', 'default')
    threshold = int(data.get('threshold', 100))
    if not ip:
        return jsonify({'error': 'IP requerida'}), 400
    if not valid_ip(ip):
        return jsonify({'error': 'Formato de IP inválido'}), 400
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO devices (ip, name, group_name, threshold) VALUES (?, ?, ?, ?)",
                (ip, name, group, threshold)
            )
        provider = get_provider(ip)
        log_audit('AGREGAR_IP', request.remote_addr, f'{ip} - {name}')
        socketio.emit('devices_changed', {})
        return jsonify({'ip': ip, 'name': name, 'provider': provider}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'IP ya existe'}), 400

@app.route('/api/devices/<ip>', methods=['DELETE'])
@login_required
def api_delete_device(ip):
    with get_db() as conn:
        conn.execute("DELETE FROM devices WHERE ip = ?", (ip,))
    log_audit('ELIMINAR_IP', request.remote_addr, ip)
    socketio.emit('devices_changed', {})
    return jsonify({'message': 'Eliminado'})

@app.route('/api/devices/<ip>', methods=['PUT'])
@login_required
def api_update_device(ip):
    data = request.get_json() or {}
    fields, values = [], []
    for key, col in [('name', 'name'), ('comments', 'comments'),
                     ('group', 'group_name'), ('threshold', 'threshold'),
                     ('maintenance', 'maintenance')]:
        if key in data:
            fields.append(f"{col} = ?")
            values.append(data[key])
    if not fields:
        return jsonify({'error': 'Sin campos'}), 400
    values.append(ip)
    with get_db() as conn:
        conn.execute(f"UPDATE devices SET {', '.join(fields)} WHERE ip = ?", values)
    log_audit('EDITAR_IP', request.remote_addr, ip)
    socketio.emit('devices_changed', {})
    return jsonify({'message': 'Actualizado'})

@app.route('/api/ping/<ip>', methods=['GET'])
@login_required
def api_ping_single(ip):
    latency = ping_ip(ip)
    return jsonify({'ip': ip, 'alive': latency is not None, 'latency': latency})

# ---------- STATS ----------
@app.route('/api/stats', methods=['GET'])
@login_required
def api_stats():
    with get_db() as conn:
        row = conn.execute('''
            SELECT COUNT(*) as total,
                   SUM(alive) as online,
                   AVG(CASE WHEN alive=1 THEN last_latency END) as avg_latency,
                   MIN(CASE WHEN alive=1 THEN last_latency END) as min_latency,
                   MAX(CASE WHEN alive=1 THEN last_latency END) as max_latency
            FROM devices
        ''').fetchone()
        providers = conn.execute('''
            SELECT provider, COUNT(*) as total,
                   SUM(alive) as online,
                   AVG(CASE WHEN alive=1 THEN last_latency END) as avg
            FROM devices GROUP BY provider
        ''').fetchall()
    total = row['total'] or 0
    online = row['online'] or 0
    provider_stats = {}
    for p in providers:
        provider_stats[p['provider'] or 'Desconocido'] = {
            'total': p['total'],
            'online': p['online'] or 0,
            'avg': round(p['avg'], 1) if p['avg'] else 0,
        }
    return jsonify({
        'total': total,
        'online': online,
        'offline': total - online,
        'avg_latency': round(row['avg_latency'], 2) if row['avg_latency'] else 0,
        'min_latency': row['min_latency'] or 0,
        'max_latency': row['max_latency'] or 0,
        'uptime': round((online / total * 100), 1) if total else 0,
        'providers': provider_stats,
    })

@app.route('/api/uptime/<ip>', methods=['GET'])
@login_required
def api_uptime(ip):
    with get_db() as conn:
        result = {}
        for name, days in [('1d', 1), ('7d', 7), ('30d', 30)]:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            row = conn.execute('''
                SELECT COUNT(*) as total, SUM(alive) as online
                FROM history WHERE device_ip = ? AND timestamp >= ?
            ''', (ip, cutoff)).fetchone()
            total = row['total'] or 0
            online = row['online'] or 0
            result[name] = round(online / total * 100, 1) if total else 0
    return jsonify(result)

@app.route('/api/ranking', methods=['GET'])
@login_required
def api_ranking():
    with get_db() as conn:
        rows = conn.execute('''
            SELECT d.ip, d.name, d.provider, d.alive,
                   AVG(h.latency) as avg_latency,
                   MIN(h.latency) as min_latency,
                   MAX(h.latency) as max_latency,
                   COUNT(h.id) as samples,
                   SUM(h.alive) as online
            FROM devices d
            LEFT JOIN history h ON h.device_ip = d.ip AND h.latency IS NOT NULL
            GROUP BY d.ip
            ORDER BY 
                CASE WHEN AVG(h.latency) IS NULL THEN 1 ELSE 0 END,
                AVG(h.latency) ASC
        ''').fetchall()
    ranking = []
    for r in rows:
        ranking.append({
            'ip': r['ip'], 'name': r['name'], 'provider': r['provider'],
            'alive': bool(r['alive']),
            'avg_latency': round(r['avg_latency'], 2) if r['avg_latency'] else None,
            'min_latency': r['min_latency'] if r['min_latency'] else None,
            'max_latency': r['max_latency'] if r['max_latency'] else None,
            'samples': r['samples'] or 0,
            'uptime_7d': round((r['online'] or 0) / r['samples'] * 100, 1) if r['samples'] else 0,
        })
    return jsonify(ranking)

# ---------- HISTORY ----------
@app.route('/api/history/<ip>', methods=['GET'])
@login_required
def api_history(ip):
    hours = int(request.args.get('hours', 24))

    max_points = 500
    if hours <= 2:
        bucket_min = 0
    else:
        total_minutes = hours * 60
        bucket_min = max(1, total_minutes // max_points)
        for candidate in [1, 2, 5, 10, 15, 30, 60, 120, 240, 1440]:
            if bucket_min <= candidate:
                bucket_min = candidate
                break

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    with get_db() as conn:
        if bucket_min == 0:
            rows = conn.execute('''
                SELECT timestamp, latency, alive FROM history
                WHERE device_ip = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            ''', (ip, cutoff)).fetchall()
            data = [{'timestamp': r['timestamp'], 'latency': r['latency'], 'alive': bool(r['alive'])} for r in rows]
        else:
            rows = conn.execute('''
                SELECT 
                    MIN(timestamp) as timestamp,
                    AVG(CASE WHEN alive=1 THEN latency END) as latency,
                    AVG(alive) as alive_ratio
                FROM history
                WHERE device_ip = ? AND timestamp >= ?
                GROUP BY CAST(strftime('%s', timestamp) AS INTEGER) / (? * 60)
                ORDER BY timestamp ASC
            ''', (ip, cutoff, bucket_min)).fetchall()
            data = [{
                'timestamp': r['timestamp'],
                'latency': round(r['latency'], 2) if r['latency'] else None,
                'alive': (r['alive_ratio'] or 0) >= 0.5
            } for r in rows]
    return jsonify(data)

# ---------- EVENTS ----------
@app.route('/api/events', methods=['GET'])
@login_required
def api_events():
    limit = int(request.args.get('limit', 100))
    device_ip = request.args.get('ip')
    event_type = request.args.get('type')

    query = '''SELECT timestamp, device_ip, device_name, event_type, latency, details
               FROM events WHERE 1=1'''
    params = []
    if device_ip:
        query += " AND device_ip = ?"
        params.append(device_ip)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------- AUDIT ----------
@app.route('/api/audit', methods=['GET'])
@login_required
def api_audit():
    limit = int(request.args.get('limit', 100))
    with get_db() as conn:
        rows = conn.execute('''
            SELECT timestamp, user_ip, action, details
            FROM audit_log ORDER BY timestamp DESC LIMIT ?
        ''', (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------- WHOIS ----------
@app.route('/api/whois/<ip>', methods=['GET'])
@login_required
def api_whois(ip):
    cache_key = f'whois:{ip}'
    cached = get_setting(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            if data.get('_ts'):
                ts = datetime.fromisoformat(data['_ts'])
                if (datetime.now() - ts).total_seconds() < 86400:
                    data.pop('_ts', None)
                    return jsonify(data)
        except Exception:
            pass
    w = get_whois(ip)
    if w:
        w_with_ts = dict(w)
        w_with_ts['_ts'] = now_iso()
        set_setting(cache_key, json.dumps(w_with_ts))
        return jsonify(w)
    return jsonify({'error': 'No se pudo obtener'}), 404

# ---------- USERS ----------
@app.route('/api/users', methods=['GET'])
@login_required
def api_users():
    cutoff = (datetime.now() - timedelta(seconds=30)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ip, username, first_seen, last_seen FROM users WHERE last_seen >= ?",
            (cutoff,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------- EXPORT ----------
@app.route('/api/export/excel', methods=['GET'])
@login_required
def api_export_excel():
    with get_db() as conn:
        devices = conn.execute('''
            SELECT ip, name, group_name, provider, comments,
                   alive, last_latency, last_check, threshold
            FROM devices ORDER BY name
        ''').fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Dispositivos"
    headers = ['IP', 'Nombre', 'Grupo', 'Proveedor', 'Estado',
               'Latencia (ms)', 'Última prueba', 'Umbral', 'Comentarios']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill(start_color="58a6ff", end_color="58a6ff", fill_type="solid")
        c.alignment = Alignment(horizontal="center")
    for row, d in enumerate(devices, 2):
        ws.cell(row=row, column=1, value=d['ip'])
        ws.cell(row=row, column=2, value=d['name'])
        ws.cell(row=row, column=3, value=d['group_name'])
        ws.cell(row=row, column=4, value=d['provider'])
        ws.cell(row=row, column=5, value='En línea' if d['alive'] else 'Caído')
        ws.cell(row=row, column=6, value=d['last_latency'])
        ws.cell(row=row, column=7, value=d['last_check'])
        ws.cell(row=row, column=8, value=d['threshold'])
        ws.cell(row=row, column=9, value=d['comments'])
    for col in range(1, 10):
        ws.column_dimensions[chr(64 + col)].width = 18

    ws_ev = wb.create_sheet("Eventos")
    ws_ev.append(['Timestamp', 'IP', 'Nombre', 'Tipo', 'Latencia', 'Detalles'])
    with get_db() as conn:
        events = conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT 1000"
        ).fetchall()
    for e in events:
        ws_ev.append([e['timestamp'], e['device_ip'], e['device_name'],
                      e['event_type'], e['latency'], e['details']])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    log_audit('EXPORTAR_EXCEL', request.remote_addr, filename)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/api/export/csv', methods=['GET'])
@login_required
def api_export_csv():
    with get_db() as conn:
        devices = conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['IP', 'Nombre', 'Grupo', 'Proveedor', 'Estado',
                     'Latencia', 'Última prueba', 'Comentarios'])
    for d in devices:
        writer.writerow([d['ip'], d['name'], d['group_name'], d['provider'],
                         'En línea' if d['alive'] else 'Caído',
                         d['last_latency'], d['last_check'], d['comments']])
    mem = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    filename = f"monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(mem, as_attachment=True, download_name=filename, mimetype='text/csv')

# ---------- SETTINGS ----------
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_set_settings():
    data = request.get_json() or {}
    for k, v in data.items():
        set_setting(k, v)
    return jsonify({'message': 'Guardado'})

# ============================================================
# SOCKET.IO
# ============================================================
_users_cache = {'ts': 0, 'data': []}
_users_cache_lock = threading.Lock()

def _active_users_cached():
    with _users_cache_lock:
        now = time.time()
        if (now - _users_cache['ts']) < 5:
            return _users_cache['data']
    cutoff = (datetime.now() - timedelta(seconds=30)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ip, username FROM users WHERE last_seen >= ?", (cutoff,)
        ).fetchall()
    result = [dict(r) for r in rows]
    with _users_cache_lock:
        _users_cache['ts'] = now
        _users_cache['data'] = result
    return result

_last_users_set = set()

def _emit_users_if_changed():
    global _last_users_set
    users = _active_users_cached()
    current_set = frozenset(u['ip'] for u in users)
    if current_set != _last_users_set:
        _last_users_set = current_set
        socketio.emit('users_update', users, broadcast=True)

@socketio.on('connect')
def handle_connect():
    ip = request.remote_addr
    try:
        hostname = socket.gethostbyaddr(ip)[0].split('.')[0]
    except Exception:
        hostname = f'Usuario_{ip.replace(".", "_")}'
    with get_db() as conn:
        conn.execute('''
            INSERT INTO users (ip, username, first_seen, last_seen, active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(ip) DO UPDATE SET last_seen = excluded.last_seen, active = 1
        ''', (ip, hostname, now_iso(), now_iso()))
    _users_cache['ts'] = 0
    _emit_users_if_changed()
    log.info("🔌 Cliente conectado: %s", ip)

@socketio.on('disconnect')
def handle_disconnect():
    ip = request.remote_addr
    with get_db() as conn:
        conn.execute("UPDATE users SET active = 0 WHERE ip = ?", (ip,))
    _users_cache['ts'] = 0
    _emit_users_if_changed()

@socketio.on('heartbeat')
def handle_heartbeat():
    ip = request.remote_addr
    with get_db() as conn:
        conn.execute("UPDATE users SET last_seen = ? WHERE ip = ?", (now_iso(), ip))
    _users_cache['ts'] = 0

@socketio.on('ask_altair')
def handle_altair(data):
    query = (data.get('query') or '').strip()
    if not query:
        return
    response = process_altair(query)
    emit('altair_response', {'query': query, 'response': response})
    log_audit('ALTAIR_QUERY', request.remote_addr, query[:80])

def _active_users():
    return _active_users_cached()

# ============================================================
# ALTAIR
# ============================================================
def process_altair(query):
    q = query.lower()
    with get_db() as conn:
        stats = conn.execute('''
            SELECT COUNT(*) as total, SUM(alive) as online
            FROM devices
        ''').fetchone()
        total = stats['total'] or 0
        online = stats['online'] or 0

    if any(w in q for w in ['hola', 'saludo', 'hey']):
        return "¡Hola! Soy Altair. Pregúntame sobre el estado de la red."

    if 'estado' in q or 'red' in q or 'cómo está' in q:
        if total == 0:
            return "No hay dispositivos monitoreados."
        return f"📊 {online}/{total} dispositivos en línea ({round(online/total*100,1)}% uptime)."

    if 'caído' in q or 'caida' in q or 'problema' in q:
        with get_db() as conn:
            downs = conn.execute(
                "SELECT name, ip FROM devices WHERE alive = 0"
            ).fetchall()
        if not downs:
            return "✅ No hay dispositivos caídos."
        return "⚠️ Caídos: " + ", ".join(f"{d['name']} ({d['ip']})" for d in downs)

    if 'latencia' in q:
        with get_db() as conn:
            row = conn.execute(
                "SELECT AVG(last_latency) as avg, MIN(last_latency) as mn, MAX(last_latency) as mx "
                "FROM devices WHERE alive = 1"
            ).fetchone()
        if not row['avg']:
            return "Sin datos de latencia."
        return f"📈 Latencia: prom {round(row['avg'],1)}ms · mín {row['mn']}ms · máx {row['mx']}ms"

    if 'incidente' in q or 'evento' in q:
        with get_db() as conn:
            events = conn.execute(
                "SELECT timestamp, device_name, event_type FROM events "
                "ORDER BY timestamp DESC LIMIT 10"
            ).fetchall()
        if not events:
            return "Sin eventos recientes."
        lines = [f"• {e['timestamp'][11:16]} {e['event_type']} {e['device_name']}" for e in events]
        return "📋 Últimos eventos:\n" + "\n".join(lines)

    if 'proveedor' in q:
        m = re.search(r'\d+\.\d+\.\d+\.\d+', query)
        if m:
            ip = m.group()
            with get_db() as conn:
                row = conn.execute(
                    "SELECT provider FROM devices WHERE ip = ?", (ip,)
                ).fetchone()
            if row:
                return f"🌐 {ip} → {row['provider']}"
            return f"No tengo {ip} en la BD."
        return "Ej: 'proveedor de 8.8.8.8'"

    if 'whois' in q:
        m = re.search(r'\d+\.\d+\.\d+\.\d+', query)
        if m:
            w = get_whois(m.group())
            if w:
                return f"📋 {m.group()}:\n• País: {w['country']}\n• ISP: {w['isp']}\n• AS: {w['as']}"
            return "No se pudo obtener."
        return "Ej: 'whois de 8.8.8.8'"

    if 'ayuda' in q or 'help' in q or 'comandos' in q:
        return ("📋 Comandos:\n"
                "• estado de la red\n"
                "• dispositivos caídos\n"
                "• latencia\n"
                "• incidentes\n"
                "• proveedor de [IP]\n"
                "• whois de [IP]")

    return f"No entendí: '{query}'. Escribe 'ayuda'."

# ============================================================
# ARRANQUE
# ============================================================
if __name__ == '__main__':
    init_db()
    start_worker()
    local_ip = '127.0.0.1'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("=" * 60)
    print("MONITOR DE RED")
    print("=" * 60)
    print(f"Datos:      {DATA_DIR}")
    print(f"Local:      http://127.0.0.1:5001")
    print(f"Red:        http://{local_ip}:5001")
    print(f"Password:   {'(sin auth)' if not ADMIN_PASSWORD else '(configurado)'}")
    print(f"Ping:       cada {PING_INTERVAL}s")
    print(f"Historial:  cada {HISTORY_INTERVAL}s ({HISTORY_RETENTION_DAYS} días)")
    print(f"Eventos:    {EVENTS_RETENTION_DAYS} días")
    print(f"Auditoría:  {AUDIT_RETENTION_DAYS} días")
    print("=" * 60)
    socketio.run(app, debug=False, host='0.0.0.0', port=5001)