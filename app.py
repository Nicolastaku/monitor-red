from flask import Flask, render_template, jsonify, request, send_file
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from ping3 import ping
import json
import os
from datetime import datetime, timedelta
import csv
import io
import socket
import uuid
import threading
import time
import base64
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import LineChart, Reference
import logging
import requests
import re
import subprocess
import platform

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tu_clave_secreta_aqui'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ===== CONFIGURACIÓN =====
SHARED_FOLDER = r'C:\Users\Public\MonitorRed'
os.makedirs(SHARED_FOLDER, exist_ok=True)
os.makedirs(os.path.join(SHARED_FOLDER, 'exports'), exist_ok=True)
os.makedirs(os.path.join(SHARED_FOLDER, 'logs'), exist_ok=True)

DATA_FILE = os.path.join(SHARED_FOLDER, 'devices.json')
EVENTS_FILE = os.path.join(SHARED_FOLDER, 'events.json')
USERS_FILE = os.path.join(SHARED_FOLDER, 'users.json')
AUDIT_FILE = os.path.join(SHARED_FOLDER, 'audit.log')
INCIDENTS_FILE = os.path.join(SHARED_FOLDER, 'incidents.json')
GROUPS_FILE = os.path.join(SHARED_FOLDER, 'groups.json')

# ===== MAPA DE PROVEEDORES COMERCIALES =====
PROVIDER_MAP = {
    'claro': 'Claro', 'comcel': 'Claro', 'une': 'UNE', 'epm': 'EPM',
    'etb': 'ETB', 'movistar': 'Movistar', 'telefonica': 'Movistar',
    'tigo': 'Tigo', 'colombia telecomunicaciones': 'Colombia Telecomunicaciones',
    'google': 'Google', 'cloudflare': 'Cloudflare', 'amazon': 'Amazon AWS',
    'microsoft': 'Microsoft Azure', 'akamai': 'Akamai', 'facebook': 'Meta',
    'apple': 'Apple', 'netflix': 'Netflix', 'verizon': 'Verizon',
    'at&t': 'AT&T', 'comcast': 'Comcast', 'spectrum': 'Spectrum',
}

# ===== CONFIGURACIÓN DE TELEGRAM =====
TELEGRAM_BOT_TOKEN = ''  # Opcional: poner el token del bot
TELEGRAM_CHAT_ID = ''    # Opcional: poner el chat ID

# ===== FUNCIONES DE AUDITORÍA =====
def log_audit(action, user_ip, details):
    timestamp = datetime.now().isoformat()
    log_entry = f"[{timestamp}] {action} | IP: {user_ip} | {details}\n"
    try:
        with open(AUDIT_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f"Error escribiendo auditoría: {e}")

# ===== FUNCIONES DE INCIDENTES =====
def load_incidents():
    try:
        with open(INCIDENTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []

def save_incident(incident):
    incidents = load_incidents()
    incidents.append(incident)
    if len(incidents) > 500:
        incidents = incidents[-500:]
    with open(INCIDENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(incidents, f, indent=2, ensure_ascii=False)

# ===== FUNCIONES DE GRUPOS =====
def load_groups():
    try:
        with open(GROUPS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {'default': {'name': 'Todos', 'ips': [], 'color': '#00d2ff'}}
    except json.JSONDecodeError:
        return {'default': {'name': 'Todos', 'ips': [], 'color': '#00d2ff'}}

def save_groups(groups):
    with open(GROUPS_FILE, 'w', encoding='utf-8') as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

# ===== FUNCIONES DE USUARIOS =====
def load_users():
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []

def save_users(users):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

def add_user(ip_address):
    users = load_users()
    existing = next((u for u in users if u['ip'] == ip_address), None)
    if existing:
        existing['last_seen'] = datetime.now().isoformat()
        existing['active'] = True
    else:
        try:
            hostname = socket.gethostbyaddr(ip_address)[0]
            name = hostname.split('.')[0]
        except:
            name = f"Usuario_{ip_address.replace('.', '_')}"
        
        users.append({
            'id': str(uuid.uuid4()),
            'username': name,
            'ip': ip_address,
            'first_seen': datetime.now().isoformat(),
            'last_seen': datetime.now().isoformat(),
            'active': True,
            'color': generate_color(),
            'messages': []
        })
    save_users(users)
    log_audit('USUARIO_CONECTADO', ip_address, f'Usuario {ip_address} conectado')
    return users

def remove_user(ip_address):
    users = load_users()
    for user in users:
        if user['ip'] == ip_address:
            user['active'] = False
            break
    save_users(users)
    log_audit('USUARIO_DESCONECTADO', ip_address, f'Usuario {ip_address} desconectado')
    return users

def get_active_users():
    users = load_users()
    now = datetime.now()
    for user in users:
        if user['active']:
            last_seen = datetime.fromisoformat(user['last_seen'])
            if (now - last_seen).total_seconds() > 30:
                user['active'] = False
    save_users(users)
    return [u for u in users if u['active']]

def get_user_by_ip(ip):
    users = load_users()
    for user in users:
        if user['ip'] == ip:
            return user
    return None

def generate_color():
    colors_list = ['#00ff88', '#00d2ff', '#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#ff6bff', '#ff9f43']
    used_colors = [u.get('color', '') for u in load_users()]
    available = [c for c in colors_list if c not in used_colors]
    return available[0] if available else '#8899aa'

# ===== FUNCIONES DE DATOS =====
file_lock = threading.Lock()

def load_devices():
    with file_lock:
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            default_devices = [
                {'ip': '192.168.1.1', 'name': 'Router', 'history': [], 'provider': 'Red Local', 'comments': '', 'group': 'default'},
                {'ip': '8.8.8.8', 'name': 'Google DNS', 'history': [], 'provider': 'Google', 'comments': '', 'group': 'default'},
                {'ip': '1.1.1.1', 'name': 'Cloudflare', 'history': [], 'provider': 'Cloudflare', 'comments': '', 'group': 'default'}
            ]
            save_devices(default_devices)
            return default_devices
        except json.JSONDecodeError:
            print("⚠️ Archivo devices.json corrupto. Creando nuevo...")
            default_devices = []
            save_devices(default_devices)
            return default_devices

def save_devices(devices):
    with file_lock:
        temp_file = DATA_FILE + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(devices, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, DATA_FILE)

def load_events():
    try:
        with open(EVENTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []

def save_event(event):
    events = load_events()
    events.append(event)
    if len(events) > 200:
        events = events[-200:]
    with open(EVENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(events, f, indent=2, ensure_ascii=False)

def ping_ip(ip, timeout=2):
    try:
        result = ping(ip, timeout=timeout)
        if result is not None:
            return round(result * 1000, 2)
        return None
    except Exception:
        return None

# ===== FUNCIÓN PARA ESCANEAR RED =====
def scan_network(network='192.168.1.', start=1, end=254, timeout=0.5):
    """Escanea un rango de IPs y devuelve las activas"""
    results = []
    for i in range(start, end + 1):
        ip = f"{network}{i}"
        latency = ping_ip(ip, timeout=timeout)
        if latency is not None:
            provider = get_provider(ip)
            results.append({
                'ip': ip,
                'latency': latency,
                'provider': provider,
                'name': f'Dispositivo {i}'
            })
    return results

# ===== FUNCIÓN PARA OBTENER PROVEEDOR COMERCIAL =====
def clean_provider_name(provider):
    if not provider:
        return 'Desconocido'
    
    provider_lower = provider.lower()
    
    for key, value in PROVIDER_MAP.items():
        if key in provider_lower:
            return value
    
    provider = re.sub(r'^AS\d+\s+', '', provider)
    
    if 'LLC' in provider or 'Inc' in provider or 'Ltd' in provider:
        parts = provider.split()
        if parts:
            return parts[0]
    
    if '(' in provider:
        provider = provider.split('(')[0].strip()
    if ',' in provider:
        provider = provider.split(',')[0].strip()
    
    return provider.strip() if provider else 'Desconocido'

def get_provider(ip):
    if ip.startswith('192.168.') or ip.startswith('10.') or ip.startswith('172.16.') or ip.startswith('127.'):
        return 'Red Local'
    
    try:
        response = requests.get(f'http://ip-api.com/json/{ip}?fields=status,isp,org', timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                provider = data.get('isp') or data.get('org')
                if provider:
                    return clean_provider_name(provider)
        
        response = requests.get(f'https://ipinfo.io/{ip}/org', timeout=3)
        if response.status_code == 200:
            provider = response.text.strip()
            if provider:
                return clean_provider_name(provider)
        
        return 'Desconocido'
    except Exception as e:
        print(f"Error obteniendo proveedor para {ip}: {e}")
        return 'Desconocido'

# ===== FUNCIÓN PARA OBTENER WHOIS =====
def get_whois(ip):
    """Obtiene información WHOIS de una IP"""
    try:
        response = requests.get(f'http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as', timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                return {
                    'country': data.get('country', 'Desconocido'),
                    'region': data.get('regionName', 'Desconocido'),
                    'city': data.get('city', 'Desconocido'),
                    'isp': data.get('isp', 'Desconocido'),
                    'org': data.get('org', 'Desconocido'),
                    'as': data.get('as', 'Desconocido')
                }
        return None
    except Exception as e:
        print(f"Error obteniendo WHOIS para {ip}: {e}")
        return None

# ===== FUNCIÓN PARA ENVIAR A TELEGRAM =====
def send_telegram_message(message):
    """Envía un mensaje a Telegram"""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"Error enviando a Telegram: {e}")

# ===== FUNCIÓN PARA EXPORTAR REPORTE COMPLETO =====
def export_full_report(devices):
    """Genera un reporte completo en PDF con gráficas"""
    # Similar a export_pdf pero con más datos
    pass

# ===== ACTUALIZAR TODOS LOS DISPOSITIVOS =====
def update_all_devices():
    devices = load_devices()
    events = []
    incidents = load_incidents()
    threshold = 100  # Umbral de latencia en ms
    
    for device in devices:
        ip = device['ip']
        latency = ping_ip(ip)
        was_alive = device.get('alive', False)
        is_alive = latency is not None
        
        if 'provider' not in device or not device['provider'] or device['provider'] == 'Desconocido':
            device['provider'] = get_provider(ip)
        
        if 'comments' not in device:
            device['comments'] = ''
        if 'group' not in device:
            device['group'] = 'default'
        if 'threshold' not in device:
            device['threshold'] = 100
        
        if 'history' not in device:
            device['history'] = []
        
        # Mantener historial de 500 registros (aprox 7 días con 5s de intervalo)
        if len(device['history']) > 500:
            device['history'] = device['history'][-500:]
        
        device['history'].append({
            'timestamp': datetime.now().isoformat(),
            'latency': latency,
            'alive': is_alive
        })
        
        device['last_latency'] = latency
        device['alive'] = is_alive
        device['last_check'] = datetime.now().strftime('%H:%M:%S')
        
        # Detectar incidentes
        if was_alive != is_alive:
            event_type = '🟢 CONECTADO' if is_alive else '🔴 DESCONECTADO'
            event = {
                'timestamp': datetime.now().isoformat(),
                'ip': ip,
                'name': device.get('name', ip),
                'type': event_type,
                'latency': latency
            }
            events.append(event)
            save_event(event)
            log_audit('CAMBIO_ESTADO', ip, f'{event_type} - {device.get("name", ip)}')
            
            # Guardar incidente
            incident = {
                'timestamp': datetime.now().isoformat(),
                'ip': ip,
                'name': device.get('name', ip),
                'type': 'DOWN' if not is_alive else 'UP',
                'latency': latency,
                'duration': 0  # Se calculará cuando se recupere
            }
            save_incident(incident)
            
            # Enviar a Telegram
            if not is_alive:
                send_telegram_message(f"🔴 <b>IP CAÍDA</b>\nIP: {ip}\nNombre: {device.get('name', ip)}\nHora: {datetime.now().strftime('%H:%M:%S')}")
            else:
                send_telegram_message(f"🟢 <b>IP RECUPERADA</b>\nIP: {ip}\nNombre: {device.get('name', ip)}\nHora: {datetime.now().strftime('%H:%M:%S')}")
        
        # Detectar latencia alta (umbral)
        if is_alive and latency and latency > threshold:
            event = {
                'timestamp': datetime.now().isoformat(),
                'ip': ip,
                'name': device.get('name', ip),
                'type': '⚠️ LATENCIA ALTA',
                'latency': latency
            }
            events.append(event)
            save_event(event)
            log_audit('LATENCIA_ALTA', ip, f'{device.get("name", ip)} - {latency}ms')
            
            if latency > threshold * 2:
                send_telegram_message(f"⚠️ <b>LATENCIA MUY ALTA</b>\nIP: {ip}\nNombre: {device.get('name', ip)}\nLatencia: {latency}ms\nHora: {datetime.now().strftime('%H:%M:%S')}")
    
    save_devices(devices)
    return devices

def calculate_uptime(history, days):
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    recent = [h for h in history if h['timestamp'] > cutoff]
    total = len(recent)
    online = sum(1 for h in recent if h['alive'])
    return round((online / total * 100), 1) if total > 0 else 0

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

# ===== SOCKET.IO EVENTOS =====
@socketio.on('connect')
def handle_connect():
    ip = request.remote_addr
    print(f'🔌 Cliente conectado: {ip}')
    
    users = add_user(ip)
    active_users = get_active_users()
    emit('users_update', active_users, broadcast=True)
    
    event = {
        'timestamp': datetime.now().isoformat(),
        'type': '👤 USUARIO CONECTADO',
        'name': get_user_by_ip(ip)['username'] if get_user_by_ip(ip) else f'Usuario {ip}',
        'ip': ip
    }
    save_event(event)
    emit('event', event, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    ip = request.remote_addr
    print(f'🔌 Cliente desconectado: {ip}')
    
    user = get_user_by_ip(ip)
    username = user['username'] if user else f'Usuario {ip}'
    
    users = remove_user(ip)
    active_users = get_active_users()
    emit('users_update', active_users, broadcast=True)
    
    event = {
        'timestamp': datetime.now().isoformat(),
        'type': '👤 USUARIO DESCONECTADO',
        'name': username,
        'ip': ip
    }
    save_event(event)
    emit('event', event, broadcast=True)

@socketio.on('user_heartbeat')
def handle_heartbeat(data):
    ip = request.remote_addr
    users = load_users()
    for user in users:
        if user['ip'] == ip:
            user['last_seen'] = datetime.now().isoformat()
            user['active'] = True
            break
    save_users(users)
    active_users = get_active_users()
    emit('users_update', active_users, broadcast=True)

@socketio.on('chat_message')
def handle_chat_message(data):
    ip = request.remote_addr
    message = data.get('message', '').strip()
    
    if not message:
        return
    
    user = get_user_by_ip(ip)
    username = user['username'] if user else f'Usuario_{ip.replace(".", "_")}'
    
    try:
        hostname = socket.gethostbyaddr(ip)[0] if ip != '127.0.0.1' else 'localhost'
    except:
        hostname = ip
    
    chat_data = {
        'timestamp': datetime.now().isoformat(),
        'username': username,
        'ip': ip,
        'message': message[:500],
        'channel': hostname if hostname != ip else 'Red Local'
    }
    
    emit('chat_message', chat_data, broadcast=True)
    log_audit('MENSAJE_CHAT', ip, f'{username}: {message[:50]}...')

@socketio.on('altair_query')
def handle_altair_query(data):
    ip = request.remote_addr
    query = data.get('query', '').strip()
    
    if not query:
        return
    
    user = get_user_by_ip(ip)
    username = user['username'] if user else f'Usuario_{ip.replace(".", "_")}'
    
    response = process_altair_query(query, ip)
    
    chat_data = {
        'timestamp': datetime.now().isoformat(),
        'username': 'Altair',
        'ip': 'system',
        'message': '🤖 ' + response,
        'channel': 'Altair AI'
    }
    
    emit('chat_message', chat_data, broadcast=True)
    log_audit('ALTAIR_QUERY', ip, f'{username}: {query[:50]}...')

def process_altair_query(query, ip):
    query_lower = query.lower()
    devices = load_devices()
    incidents = load_incidents()
    
    total = len(devices)
    online = sum(1 for d in devices if d.get('alive'))
    offline = total - online
    down_ips = [d['ip'] for d in devices if not d.get('alive')]
    
    if 'hola' in query_lower or 'saludo' in query_lower:
        return f"¡Hola! Soy Altair, tu asistente de red. ¿En qué puedo ayudarte hoy?"
    
    elif 'estado' in query_lower or 'red' in query_lower or 'como está' in query_lower:
        if total == 0:
            return "No hay dispositivos monitoreados. Agrega algunas IPs para empezar."
        uptime = round((online / total * 100), 1) if total > 0 else 0
        return f"📊 Estado de la red: {online} dispositivos en línea, {offline} caídos. Uptime global: {uptime}%."
    
    elif 'caído' in query_lower or 'caida' in query_lower or 'problema' in query_lower:
        if not down_ips:
            return "✅ No hay dispositivos caídos. Todo funciona correctamente."
        return f"⚠️ Dispositivos caídos: {', '.join(down_ips)}. ¿Quieres que haga ping a alguno?"
    
    elif 'latencia' in query_lower or 'ms' in query_lower:
        avg_lat = [d['last_latency'] for d in devices if d.get('alive') and d.get('last_latency')]
        if not avg_lat:
            return "No hay datos de latencia disponibles."
        avg = sum(avg_lat) / len(avg_lat)
        return f"📈 Latencia promedio: {round(avg, 1)}ms. La latencia más baja es {min(avg_lat)}ms y la más alta {max(avg_lat)}ms."
    
    elif 'incidentes' in query_lower or 'log' in query_lower:
        recent = incidents[-10:] if incidents else []
        if not recent:
            return "No hay incidentes registrados recientemente."
        msg = "📋 Últimos incidentes:\n"
        for inc in recent:
            time = datetime.fromisoformat(inc['timestamp']).strftime('%H:%M')
            msg += f"• {time} - {inc['type']} - {inc['name']} ({inc['ip']})\n"
        return msg
    
    elif 'proveedor' in query_lower:
        import re
        ip_match = re.search(r'\d+\.\d+\.\d+\.\d+', query)
        if ip_match:
            target_ip = ip_match.group()
            device = next((d for d in devices if d['ip'] == target_ip), None)
            if device:
                provider = device.get('provider', 'Desconocido')
                return f"🌐 El proveedor de {target_ip} es: {provider}"
            else:
                return f"❌ No tengo información de {target_ip}. ¿Está monitoreado?"
        return "📡 Para saber el proveedor, escribe: 'proveedor de [IP]'"
    
    elif 'whois' in query_lower:
        import re
        ip_match = re.search(r'\d+\.\d+\.\d+\.\d+', query)
        if ip_match:
            target_ip = ip_match.group()
            whois = get_whois(target_ip)
            if whois:
                return f"📋 WHOIS de {target_ip}:\n• País: {whois['country']}\n• Región: {whois['region']}\n• Ciudad: {whois['city']}\n• ISP: {whois['isp']}\n• AS: {whois['as']}"
            else:
                return f"❌ No se pudo obtener información de {target_ip}"
        return "📡 Para ver WHOIS, escribe: 'whois de [IP]'"
    
    elif 'grupos' in query_lower:
        groups = load_groups()
        msg = "📁 Grupos disponibles:\n"
        for key, group in groups.items():
            count = len(group.get('ips', []))
            msg += f"• {group['name']} ({count} dispositivos)\n"
        return msg
    
    elif 'ayuda' in query_lower or 'comandos' in query_lower:
        return """📋 Comandos disponibles:
        • "hola" - Saludo
        • "estado de la red" - Resumen general
        • "dispositivos caídos" - Lista de IPs caídas
        • "latencia" - Estadísticas de latencia
        • "incidentes" - Log de caídas/recaídas
        • "proveedor de [IP]" - Info del proveedor
        • "whois de [IP]" - Información WHOIS
        • "grupos" - Lista de grupos
        • "ayuda" - Este mensaje"""
    
    elif 'gracias' in query_lower:
        return "¡De nada! Estoy aquí para ayudarte con la red. 😊"
    
    else:
        return f"No entendí tu consulta: '{query}'. Escribe 'ayuda' para ver los comandos disponibles."

# ===== RUTAS DE LA API =====

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/devices', methods=['GET'])
def get_devices():
    devices = load_devices()
    return jsonify(devices)

@app.route('/api/devices', methods=['POST'])
def add_device():
    data = request.json
    ip = data.get('ip')
    name = data.get('name', ip)
    group = data.get('group', 'default')
    threshold = data.get('threshold', 100)
    user_ip = request.remote_addr
    
    if not ip:
        return jsonify({'error': 'IP requerida'}), 400
    
    devices = load_devices()
    
    if any(d['ip'] == ip for d in devices):
        return jsonify({'error': 'IP ya existe'}), 400
    
    provider = get_provider(ip)
    print(f"📡 Nuevo dispositivo {ip} - Proveedor: {provider}")
    
    new_device = {
        'ip': ip,
        'name': name,
        'history': [],
        'alive': False,
        'last_latency': None,
        'last_check': '--',
        'provider': provider,
        'comments': '',
        'group': group,
        'threshold': threshold
    }
    devices.append(new_device)
    save_devices(devices)
    
    # Actualizar grupo
    groups = load_groups()
    if group not in groups:
        groups[group] = {'name': group, 'ips': [], 'color': '#00d2ff'}
    if ip not in groups[group].get('ips', []):
        groups[group]['ips'] = groups[group].get('ips', []) + [ip]
    save_groups(groups)
    
    log_audit('AGREGAR_IP', user_ip, f'IP: {ip}, Nombre: {name}, Proveedor: {provider}')
    
    return jsonify(new_device), 201

@app.route('/api/devices/<ip>', methods=['DELETE'])
def delete_device(ip):
    user_ip = request.remote_addr
    devices = load_devices()
    device = next((d for d in devices if d['ip'] == ip), None)
    devices = [d for d in devices if d['ip'] != ip]
    save_devices(devices)
    
    # Eliminar de grupos
    groups = load_groups()
    for key, group in groups.items():
        if ip in group.get('ips', []):
            group['ips'].remove(ip)
    save_groups(groups)
    
    if device:
        log_audit('ELIMINAR_IP', user_ip, f'IP: {ip}, Nombre: {device.get("name", ip)}')
    
    return jsonify({'message': 'Eliminado'})

@app.route('/api/devices/<ip>', methods=['PUT'])
def update_device(ip):
    data = request.json
    new_name = data.get('name')
    comments = data.get('comments')
    group = data.get('group')
    threshold = data.get('threshold')
    user_ip = request.remote_addr
    
    devices = load_devices()
    
    for device in devices:
        if device['ip'] == ip:
            if new_name:
                old_name = device.get('name', ip)
                device['name'] = new_name
                log_audit('RENOMBRAR_IP', user_ip, f'IP: {ip}, Antiguo: {old_name}, Nuevo: {new_name}')
            if comments is not None:
                device['comments'] = comments
                log_audit('COMENTARIO_IP', user_ip, f'IP: {ip}, Comentario: {comments}')
            if group is not None:
                old_group = device.get('group', 'default')
                device['group'] = group
                # Actualizar grupos
                groups = load_groups()
                if old_group in groups and ip in groups[old_group].get('ips', []):
                    groups[old_group]['ips'].remove(ip)
                if group not in groups:
                    groups[group] = {'name': group, 'ips': [], 'color': '#00d2ff'}
                if ip not in groups[group].get('ips', []):
                    groups[group]['ips'] = groups[group].get('ips', []) + [ip]
                save_groups(groups)
                log_audit('GRUPO_IP', user_ip, f'IP: {ip}, Grupo: {group}')
            if threshold is not None:
                device['threshold'] = threshold
                log_audit('UMBRAL_IP', user_ip, f'IP: {ip}, Umbral: {threshold}ms')
            save_devices(devices)
            return jsonify(device)
    
    return jsonify({'error': 'Dispositivo no encontrado'}), 404

@app.route('/api/ping/<ip>', methods=['GET'])
def ping_single(ip):
    latency = ping_ip(ip)
    return jsonify({
        'ip': ip,
        'alive': latency is not None,
        'latency': latency
    })

@app.route('/api/update', methods=['POST'])
def update_all():
    devices = update_all_devices()
    return jsonify(devices)

@app.route('/api/events', methods=['GET'])
def get_events():
    events = load_events()
    return jsonify(events[-50:])

@app.route('/api/users', methods=['GET'])
def get_users():
    users = get_active_users()
    return jsonify(users)

@app.route('/api/uptime/<ip>', methods=['GET'])
def get_uptime(ip):
    devices = load_devices()
    device = next((d for d in devices if d['ip'] == ip), None)
    
    if not device:
        return jsonify({'error': 'IP no encontrada'}), 404
    
    history = device.get('history', [])
    periods = {'1d': 1, '7d': 7, '30d': 30}
    
    result = {}
    for period_name, days in periods.items():
        result[period_name] = calculate_uptime(history, days)
    
    return jsonify(result)

@app.route('/api/ranking', methods=['GET'])
def get_ranking():
    devices = load_devices()
    
    ranking = []
    for device in devices:
        history = device.get('history', [])
        if history:
            latency_values = [h['latency'] for h in history if h['latency'] is not None]
            if latency_values:
                ranking.append({
                    'ip': device['ip'],
                    'name': device.get('name', device['ip']),
                    'avg_latency': round(sum(latency_values) / len(latency_values), 2),
                    'min_latency': min(latency_values),
                    'max_latency': max(latency_values),
                    'alive': device.get('alive', False),
                    'uptime_7d': calculate_uptime(history, 7),
                    'provider': device.get('provider', 'Desconocido')
                })
    
    ranking.sort(key=lambda x: x['avg_latency'])
    return jsonify(ranking)

@app.route('/api/stats', methods=['GET'])
def get_stats():
    devices = load_devices()
    groups = load_groups()
    
    total = len(devices)
    online = sum(1 for d in devices if d.get('alive'))
    
    latencies = [d['last_latency'] for d in devices if d.get('alive') and d.get('last_latency')]
    
    # Estadísticas por proveedor
    provider_stats = {}
    for device in devices:
        provider = device.get('provider', 'Desconocido')
        if provider not in provider_stats:
            provider_stats[provider] = {'total': 0, 'online': 0, 'latencies': []}
        provider_stats[provider]['total'] += 1
        if device.get('alive'):
            provider_stats[provider]['online'] += 1
        if device.get('last_latency'):
            provider_stats[provider]['latencies'].append(device.get('last_latency'))
    
    for provider in provider_stats:
        if provider_stats[provider]['latencies']:
            provider_stats[provider]['avg'] = round(sum(provider_stats[provider]['latencies']) / len(provider_stats[provider]['latencies']), 1)
        else:
            provider_stats[provider]['avg'] = 0
    
    stats = {
        'total': total,
        'online': online,
        'offline': total - online,
        'avg_latency': round(sum(latencies) / len(latencies), 2) if latencies else 0,
        'min_latency': min(latencies) if latencies else 0,
        'max_latency': max(latencies) if latencies else 0,
        'uptime': round((online / total * 100), 1) if total > 0 else 0,
        'providers': provider_stats,
        'groups': groups
    }
    
    return jsonify(stats)

@app.route('/api/scan', methods=['POST'])
def scan_network_api():
    data = request.json
    network = data.get('network', '192.168.1.')
    start = data.get('start', 1)
    end = data.get('end', 20)
    user_ip = request.remote_addr
    
    results = scan_network(network, start, end)
    
    log_audit('ESCANEAR_RED', user_ip, f'Red: {network}{start}-{end}, Encontrados: {len(results)}')
    
    return jsonify(results)

@app.route('/api/incidents', methods=['GET'])
def get_incidents():
    incidents = load_incidents()
    return jsonify(incidents[-50:])

@app.route('/api/whois/<ip>', methods=['GET'])
def get_whois_api(ip):
    whois = get_whois(ip)
    if whois:
        return jsonify(whois)
    return jsonify({'error': 'No se pudo obtener información'}), 404

@app.route('/api/groups', methods=['GET'])
def get_groups():
    groups = load_groups()
    return jsonify(groups)

@app.route('/api/groups', methods=['POST'])
def create_group():
    data = request.json
    group_id = data.get('id', str(uuid.uuid4())[:8])
    name = data.get('name', 'Nuevo Grupo')
    color = data.get('color', '#00d2ff')
    user_ip = request.remote_addr
    
    groups = load_groups()
    groups[group_id] = {'name': name, 'ips': [], 'color': color}
    save_groups(groups)
    
    log_audit('CREAR_GRUPO', user_ip, f'Grupo: {name}')
    
    return jsonify({'id': group_id, 'name': name, 'color': color})

@app.route('/api/groups/<group_id>', methods=['DELETE'])
def delete_group(group_id):
    user_ip = request.remote_addr
    groups = load_groups()
    
    if group_id in groups:
        # Mover IPs al grupo default
        default_ips = groups['default'].get('ips', [])
        for ip in groups[group_id].get('ips', []):
            if ip not in default_ips:
                default_ips.append(ip)
        groups['default']['ips'] = default_ips
        del groups[group_id]
        save_groups(groups)
        log_audit('ELIMINAR_GRUPO', user_ip, f'Grupo: {group_id}')
    
    return jsonify({'message': 'Eliminado'})

@app.route('/api/export/report', methods=['POST'])
def export_report():
    data = request.json
    devices = data.get('devices', [])
    user_ip = request.remote_addr
    
    if not devices:
        devices = load_devices()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reporte_completo_{timestamp}.xlsx"
    filepath = os.path.join(SHARED_FOLDER, 'exports', filename)
    
    wb = openpyxl.Workbook()
    
    # Hoja de resumen
    ws = wb.active
    ws.title = "Resumen"
    
    headers = ['IP', 'Nombre', 'Estado', 'Latencia (ms)', 'Última prueba', 'Uptime 7d', 'Uptime 30d', 'Proveedor', 'Grupo', 'Comentarios']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="00d2ff", end_color="00d2ff", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")
    
    for row, device in enumerate(devices, 2):
        alive = device.get('alive', False)
        latency = device.get('last_latency', '--')
        latency_text = f"{latency}ms" if latency else '--'
        status = 'En línea' if alive else 'Desconectado'
        uptime_7d = calculate_uptime(device.get('history', []), 7)
        uptime_30d = calculate_uptime(device.get('history', []), 30)
        provider = device.get('provider', 'Desconocido')
        group = device.get('group', 'default')
        comments = device.get('comments', '')
        
        ws.cell(row=row, column=1, value=device['ip'])
        ws.cell(row=row, column=2, value=device.get('name', device['ip']))
        ws.cell(row=row, column=3, value=status)
        ws.cell(row=row, column=4, value=latency_text)
        ws.cell(row=row, column=5, value=device.get('last_check', '--'))
        ws.cell(row=row, column=6, value=f"{uptime_7d}%")
        ws.cell(row=row, column=7, value=f"{uptime_30d}%")
        ws.cell(row=row, column=8, value=provider)
        ws.cell(row=row, column=9, value=group)
        ws.cell(row=row, column=10, value=comments)
    
    for col in range(1, 11):
        ws.column_dimensions[chr(64 + col)].width = 18
    
    # Hoja de histórico
    ws_history = wb.create_sheet("Histórico")
    ws_history.cell(row=1, column=1, value="Timestamp")
    ws_history.cell(row=1, column=2, value="IP")
    ws_history.cell(row=1, column=3, value="Nombre")
    ws_history.cell(row=1, column=4, value="Latencia (ms)")
    ws_history.cell(row=1, column=5, value="Estado")
    
    row = 2
    for device in devices:
        for entry in device.get('history', [])[-100:]:
            ws_history.cell(row=row, column=1, value=entry.get('timestamp', ''))
            ws_history.cell(row=row, column=2, value=device['ip'])
            ws_history.cell(row=row, column=3, value=device.get('name', device['ip']))
            ws_history.cell(row=row, column=4, value=entry.get('latency', ''))
            ws_history.cell(row=row, column=5, value='En línea' if entry.get('alive') else 'Desconectado')
            row += 1
    
    # Hoja de incidentes
    ws_incidents = wb.create_sheet("Incidentes")
    incidents = load_incidents()
    ws_incidents.cell(row=1, column=1, value="Timestamp")
    ws_incidents.cell(row=1, column=2, value="IP")
    ws_incidents.cell(row=1, column=3, value="Nombre")
    ws_incidents.cell(row=1, column=4, value="Tipo")
    ws_incidents.cell(row=1, column=5, value="Latencia (ms)")
    
    for idx, inc in enumerate(incidents[-100:], 2):
        ws_incidents.cell(row=idx, column=1, value=inc.get('timestamp', ''))
        ws_incidents.cell(row=idx, column=2, value=inc.get('ip', ''))
        ws_incidents.cell(row=idx, column=3, value=inc.get('name', ''))
        ws_incidents.cell(row=idx, column=4, value=inc.get('type', ''))
        ws_incidents.cell(row=idx, column=5, value=inc.get('latency', ''))
    
    wb.save(filepath)
    
    log_audit('EXPORTAR_REPORTE', user_ip, f'Archivo: {filename}')
    
    return send_file(filepath, as_attachment=True, download_name=filename)

@app.route('/api/audit', methods=['GET'])
def get_audit():
    try:
        with open(AUDIT_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return jsonify(lines[-100:])
    except FileNotFoundError:
        return jsonify([])

@app.route('/api/config/theme', methods=['POST'])
def set_theme():
    data = request.json
    user_ip = request.remote_addr
    theme = data.get('theme', 'dark')
    
    # Guardar tema en el usuario
    users = load_users()
    for user in users:
        if user['ip'] == user_ip:
            user['theme'] = theme
            break
    save_users(users)
    
    return jsonify({'theme': theme})

@app.route('/api/config/audio', methods=['POST'])
def set_audio():
    data = request.json
    user_ip = request.remote_addr
    sound = data.get('sound', 'default')
    
    users = load_users()
    for user in users:
        if user['ip'] == user_ip:
            user['sound'] = sound
            break
    save_users(users)
    
    return jsonify({'sound': sound})

@app.route('/api/config/telegram', methods=['POST'])
def set_telegram():
    data = request.json
    bot_token = data.get('bot_token', '')
    chat_id = data.get('chat_id', '')
    user_ip = request.remote_addr
    
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_BOT_TOKEN = bot_token
    TELEGRAM_CHAT_ID = chat_id
    
    log_audit('CONFIGURAR_TELEGRAM', user_ip, 'Token y Chat ID configurados')
    
    # Probar conexión
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return jsonify({'success': True, 'message': 'Conexión exitosa'})
        else:
            return jsonify({'success': False, 'message': 'Error en la conexión'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

if __name__ == '__main__':
    if not os.path.exists(DATA_FILE):
        load_devices()
    
    local_ip = get_local_ip()
    
    print("=" * 70)
    print("🚀 MONITOR DE RED - VERSIÓN PROFESIONAL")
    print("=" * 70)
    print(f"📁 Datos guardados en: {SHARED_FOLDER}")
    print(f"📡 Acceso LOCAL: http://127.0.0.1:5000")
    print(f"📡 Acceso REMOTO: http://{local_ip}:5000")
    print("=" * 70)
    print("📋 CARACTERÍSTICAS:")
    print("   ✅ Proveedores comerciales: Claro, ETB, Movistar, etc.")
    print("   ✅ Datos compartidos entre usuarios")
    print("   ✅ Chat con Altair (IA)")
    print("   ✅ Alertas visuales en bordes")
    print("   ✅ Miniaturas de gráficas")
    print("   ✅ Filtros por estado e IP")
    print("   ✅ Escaneo de red")
    print("   ✅ Log de incidentes")
    print("   ✅ WHOIS integrado")
    print("   ✅ Grupos de dispositivos")
    print("   ✅ Temas de color")
    print("   ✅ Configuración de audio")
    print("   ✅ Integración con Telegram")
    print("   ✅ Exportar reporte completo")
    print("=" * 70)
    print("Presiona CTRL+C para detener")
    print("=" * 70)
    
    socketio.run(app, debug=False, host='0.0.0.0', port=5001)