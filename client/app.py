#!/usr/bin/env python3
"""
BlueK9 Client - Bluetooth Detection and Tracking System
A professional-grade Bluetooth surveillance tool for security operations.
"""

import os
import sys
import json
import time
import logging
import sqlite3
import subprocess
import threading
import re
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
import struct

# Configuration
CONFIG = {
    'SECRET_KEY': os.urandom(24).hex(),
    'DATABASE': 'bluek9.db',
    'LOG_FILE': '../logs/bluek9.log',
    'MAPBOX_TOKEN': 'pk.eyJ1Ijoiam1jY3VsbG91Z2g0IiwiYSI6ImNtMGJvOXh3cDBjNncya3B4cDg0MXFuYnUifQ.uDJKnqE9WgkvGXYGLge-NQ',
    'DEFAULT_USER': 'bluek9',
    'DEFAULT_PASS': 'warhammer',
    'SMS_NUMBERS': [],  # Up to 10 US phone numbers
    'SCAN_INTERVAL': 2,  # seconds between scans
}

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = CONFIG['SECRET_KEY']
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(CONFIG['LOG_FILE'], mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('BlueK9')

# Global state
scanning_active = False
scan_thread = None
gps_thread = None
current_location = {'lat': 0.0, 'lon': 0.0, 'accuracy': 0.0}
devices = {}  # BD Address -> device info
targets = {}  # BD Address -> target info
active_radios = {'bluetooth': [], 'wifi': []}
sms_numbers = []
follow_gps = True
log_messages = []


def init_database():
    """Initialize SQLite database for device logging."""
    conn = sqlite3.connect(CONFIG['DATABASE'])
    c = conn.cursor()

    # Devices table - all detected devices
    c.execute('''CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bd_address TEXT NOT NULL,
        device_name TEXT,
        manufacturer TEXT,
        device_type TEXT,
        rssi INTEGER,
        first_seen DATETIME,
        last_seen DATETIME,
        system_lat REAL,
        system_lon REAL,
        emitter_lat REAL,
        emitter_lon REAL,
        emitter_accuracy REAL,
        is_target INTEGER DEFAULT 0,
        raw_data TEXT
    )''')

    # Targets table - devices of interest
    c.execute('''CREATE TABLE IF NOT EXISTS targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bd_address TEXT UNIQUE NOT NULL,
        alias TEXT,
        notes TEXT,
        priority INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # SMS numbers table
    c.execute('''CREATE TABLE IF NOT EXISTS sms_numbers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone_number TEXT UNIQUE NOT NULL,
        active INTEGER DEFAULT 1
    )''')

    # RSSI history for geolocation
    c.execute('''CREATE TABLE IF NOT EXISTS rssi_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bd_address TEXT NOT NULL,
        rssi INTEGER,
        system_lat REAL,
        system_lon REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(CONFIG['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn


def add_log(message, level='INFO'):
    """Add a log message and broadcast to clients."""
    timestamp = datetime.now().strftime('%H:%M:%S')
    log_entry = {'time': timestamp, 'level': level, 'message': message}
    log_messages.append(log_entry)
    if len(log_messages) > 500:
        log_messages.pop(0)
    socketio.emit('log_update', log_entry)
    if level == 'ERROR':
        logger.error(message)
    elif level == 'WARNING':
        logger.warning(message)
    else:
        logger.info(message)


def login_required(f):
    """Decorator for routes requiring authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ==================== BLUETOOTH SCANNING ====================

def get_manufacturer(bd_address):
    """Look up manufacturer from OUI database."""
    oui = bd_address.upper().replace(':', '')[:6]
    # Common Bluetooth OUIs
    oui_db = {
        '000000': 'Unknown',
        '001A7D': 'cyber-blue(HK)Ltd',
        '0025DB': 'Apple, Inc.',
        '002608': 'Apple, Inc.',
        '3C5AB4': 'Google, Inc.',
        '40F3AE': 'Samsung Electronics',
        '5C5948': 'Apple, Inc.',
        '7C6DF8': 'Apple, Inc.',
        '8C8590': 'Apple, Inc.',
        '94E979': 'Apple, Inc.',
        'A860B6': 'Apple, Inc.',
        'ACDE48': 'Apple, Inc.',
        'B8E856': 'Apple, Inc.',
        'D0817A': 'Samsung Electronics',
        'DC2B2A': 'Apple, Inc.',
        'F0D1A9': 'Apple, Inc.',
        'F4F5D8': 'Google, Inc.',
        '00158D': 'Sena Technologies',
        '000272': 'CC&C Technologies',
        'B8D61A': 'LG Electronics',
        '2C54CF': 'LG Electronics',
        '0017C9': 'Samsung Electronics',
        '002567': 'Samsung Electronics',
    }
    return oui_db.get(oui, f'OUI:{oui}')


def parse_hcitool_scan(output, scan_type='classic'):
    """Parse hcitool scan output."""
    devices_found = []
    lines = output.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line or 'Scanning' in line or 'Inquiry' in line:
            continue

        # Classic format: "XX:XX:XX:XX:XX:XX    Device Name"
        # LE format: "XX:XX:XX:XX:XX:XX (type) or with extra info"
        match = re.match(r'([0-9A-Fa-f:]{17})\s+(.*)', line)
        if match:
            bd_addr = match.group(1).upper()
            name = match.group(2).strip() or 'Unknown'
            devices_found.append({
                'bd_address': bd_addr,
                'device_name': name,
                'device_type': scan_type,
                'manufacturer': get_manufacturer(bd_addr)
            })

    return devices_found


def parse_bluetoothctl_scan(output):
    """Parse bluetoothctl scan output for devices."""
    devices_found = []
    # Pattern: [NEW] Device XX:XX:XX:XX:XX:XX Name
    pattern = r'\[(?:NEW|CHG)\]\s+Device\s+([0-9A-Fa-f:]{17})\s+(.*)'

    for match in re.finditer(pattern, output):
        bd_addr = match.group(1).upper()
        name = match.group(2).strip() or 'Unknown'
        devices_found.append({
            'bd_address': bd_addr,
            'device_name': name,
            'device_type': 'ble',
            'manufacturer': get_manufacturer(bd_addr)
        })

    return devices_found


def scan_classic_bluetooth(interface='hci0'):
    """Scan for Classic Bluetooth devices."""
    try:
        add_log(f"Starting Classic BT scan on {interface}", "INFO")
        # Standard inquiry scan
        result = subprocess.run(
            ['hcitool', '-i', interface, 'scan', '--flush'],
            capture_output=True,
            text=True,
            timeout=15
        )
        devices_found = parse_hcitool_scan(result.stdout, 'classic')
        add_log(f"Classic scan found {len(devices_found)} devices", "INFO")
        return devices_found
    except subprocess.TimeoutExpired:
        add_log("Classic scan timeout", "WARNING")
        return []
    except Exception as e:
        add_log(f"Classic scan error: {str(e)}", "ERROR")
        return []


def scan_ble_devices(interface='hci0'):
    """Scan for Bluetooth Low Energy devices."""
    try:
        add_log(f"Starting BLE scan on {interface}", "INFO")
        # Use btmgmt for LE scanning
        result = subprocess.run(
            ['timeout', '8', 'btmgmt', '-i', interface, 'find', '-l'],
            capture_output=True,
            text=True
        )

        devices_found = []
        # Parse btmgmt output
        pattern = r'dev_found:\s+([0-9A-Fa-f:]{17})\s+type\s+(\w+)\s+rssi\s+(-?\d+)'
        for match in re.finditer(pattern, result.stdout):
            bd_addr = match.group(1).upper()
            rssi = int(match.group(3))
            devices_found.append({
                'bd_address': bd_addr,
                'device_name': 'BLE Device',
                'device_type': 'ble',
                'rssi': rssi,
                'manufacturer': get_manufacturer(bd_addr)
            })

        # Fallback to hcitool lescan parsing
        if not devices_found:
            result = subprocess.run(
                ['timeout', '5', 'hcitool', '-i', interface, 'lescan'],
                capture_output=True,
                text=True
            )
            devices_found = parse_hcitool_scan(result.stdout, 'ble')

        add_log(f"BLE scan found {len(devices_found)} devices", "INFO")
        return devices_found
    except Exception as e:
        add_log(f"BLE scan error: {str(e)}", "ERROR")
        return []


def get_device_rssi(bd_address, interface='hci0'):
    """Get RSSI for a specific device."""
    try:
        result = subprocess.run(
            ['hcitool', '-i', interface, 'rssi', bd_address],
            capture_output=True,
            text=True,
            timeout=5
        )
        match = re.search(r'RSSI return value:\s*(-?\d+)', result.stdout)
        if match:
            return int(match.group(1))
    except:
        pass
    return None


def stimulate_bluetooth_classic(interface='hci0'):
    """
    Stimulate Classic Bluetooth devices to respond.
    Uses multiple inquiry modes to maximize device detection.
    """
    try:
        add_log("Stimulating Classic BT devices...", "INFO")
        devices_found = []

        # Extended Inquiry with different LAPs
        inquiry_laps = ['9e8b33', '9e8b00', '9e8b01']  # GIAC, LIAC, etc.

        for lap in inquiry_laps:
            try:
                # Send HCI inquiry command
                subprocess.run(
                    ['hcitool', '-i', interface, 'cmd', '0x01', '0x0001',
                     lap[4:6], lap[2:4], lap[0:2], '08', '00'],
                    capture_output=True,
                    timeout=5
                )
            except:
                pass

        # Name request to stimulate responses
        result = subprocess.run(
            ['hcitool', '-i', interface, 'scan', '--flush', '--length=8'],
            capture_output=True,
            text=True,
            timeout=20
        )
        devices_found = parse_hcitool_scan(result.stdout, 'classic')

        add_log(f"Classic stimulation found {len(devices_found)} devices", "INFO")
        return devices_found
    except Exception as e:
        add_log(f"Classic stimulation error: {str(e)}", "ERROR")
        return []


def stimulate_ble_devices(interface='hci0'):
    """
    Stimulate BLE devices to respond.
    Uses active scanning with scan requests.
    """
    try:
        add_log("Stimulating BLE devices...", "INFO")
        devices_found = []

        # Enable LE scan with active mode (sends SCAN_REQ)
        subprocess.run(['hciconfig', interface, 'up'], capture_output=True)

        # Set scan parameters for active scanning
        # LE_Set_Scan_Parameters: active scan, 100ms interval, 100ms window
        subprocess.run([
            'hcitool', '-i', interface, 'cmd', '0x08', '0x000B',
            '01',  # Active scanning
            '60', '00',  # Scan interval (96 * 0.625ms = 60ms)
            '30', '00',  # Scan window (48 * 0.625ms = 30ms)
            '00',  # Public address
            '00'   # Accept all
        ], capture_output=True)

        # Run scan
        result = subprocess.run(
            ['timeout', '10', 'btmgmt', '-i', interface, 'find', '-l', '-b'],
            capture_output=True,
            text=True
        )

        pattern = r'dev_found:\s+([0-9A-Fa-f:]{17})\s+type\s+(\w+)\s+rssi\s+(-?\d+)'
        for match in re.finditer(pattern, result.stdout):
            bd_addr = match.group(1).upper()
            rssi = int(match.group(3))
            devices_found.append({
                'bd_address': bd_addr,
                'device_name': 'BLE Device',
                'device_type': 'ble',
                'rssi': rssi,
                'manufacturer': get_manufacturer(bd_addr)
            })

        add_log(f"BLE stimulation found {len(devices_found)} devices", "INFO")
        return devices_found
    except Exception as e:
        add_log(f"BLE stimulation error: {str(e)}", "ERROR")
        return []


def get_device_info(bd_address, interface='hci0'):
    """Get detailed info about a specific device."""
    info = {'bd_address': bd_address}

    try:
        # Try to get device name
        result = subprocess.run(
            ['hcitool', '-i', interface, 'name', bd_address],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.stdout.strip():
            info['device_name'] = result.stdout.strip()

        # Try to get device info
        result = subprocess.run(
            ['hcitool', '-i', interface, 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=10
        )
        info['raw_info'] = result.stdout

        # Parse device class if available
        class_match = re.search(r'Class:\s*(0x[0-9A-Fa-f]+)', result.stdout)
        if class_match:
            info['device_class'] = class_match.group(1)
    except:
        pass

    return info


# ==================== GEOLOCATION ALGORITHM ====================

def calculate_distance_from_rssi(rssi, tx_power=-59):
    """
    Calculate approximate distance from RSSI using log-distance path loss model.
    tx_power: expected RSSI at 1 meter (typically -59 to -65 dBm for BT)
    """
    if rssi >= 0:
        return 0.1

    n = 2.5  # Path loss exponent (2-4 for indoor, 2-3 for outdoor)
    distance = 10 ** ((tx_power - rssi) / (10 * n))
    return min(distance, 1000)  # Cap at 1km


def estimate_emitter_location(bd_address):
    """
    Estimate emitter location using weighted centroid from RSSI history.
    Returns (lat, lon, accuracy_radius) or None.
    """
    conn = get_db()
    c = conn.cursor()

    # Get recent RSSI readings
    c.execute('''
        SELECT rssi, system_lat, system_lon, timestamp
        FROM rssi_history
        WHERE bd_address = ? AND timestamp > datetime('now', '-5 minutes')
        ORDER BY timestamp DESC
        LIMIT 20
    ''', (bd_address,))

    readings = c.fetchall()
    conn.close()

    if len(readings) < 2:
        return None

    # Weighted centroid calculation
    total_weight = 0
    weighted_lat = 0
    weighted_lon = 0
    distances = []

    for reading in readings:
        rssi, lat, lon, _ = reading
        if lat and lon and rssi:
            distance = calculate_distance_from_rssi(rssi)
            weight = 1.0 / (distance + 0.1)  # Inverse distance weighting

            weighted_lat += lat * weight
            weighted_lon += lon * weight
            total_weight += weight
            distances.append(distance)

    if total_weight == 0:
        return None

    est_lat = weighted_lat / total_weight
    est_lon = weighted_lon / total_weight

    # CEP radius (Circular Error Probable) - 50th percentile
    cep_radius = sorted(distances)[len(distances) // 2] if distances else 50

    return (est_lat, est_lon, cep_radius)


def update_device_location(bd_address, rssi):
    """Update device location estimate based on new RSSI reading."""
    global current_location

    if not current_location['lat'] or not current_location['lon']:
        return None

    conn = get_db()
    c = conn.cursor()

    # Store RSSI reading
    c.execute('''
        INSERT INTO rssi_history (bd_address, rssi, system_lat, system_lon)
        VALUES (?, ?, ?, ?)
    ''', (bd_address, rssi, current_location['lat'], current_location['lon']))

    conn.commit()
    conn.close()

    # Calculate new location estimate
    return estimate_emitter_location(bd_address)


# ==================== GPS TRACKING ====================

def get_gps_location():
    """Get GPS location from gpsd or other sources."""
    global current_location

    try:
        # Try gpsd first
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(('localhost', 2947))
        sock.send(b'?WATCH={"enable":true,"json":true}')

        while True:
            data = sock.recv(4096).decode('utf-8')
            for line in data.split('\n'):
                if line.startswith('{"class":"TPV"'):
                    tpv = json.loads(line)
                    if 'lat' in tpv and 'lon' in tpv:
                        current_location = {
                            'lat': tpv['lat'],
                            'lon': tpv['lon'],
                            'accuracy': tpv.get('epx', 10)
                        }
                        return current_location
    except:
        pass

    # Fallback: try to read from a GPS device directly
    try:
        gps_devices = ['/dev/ttyUSB0', '/dev/ttyACM0', '/dev/ttyS0']
        for dev in gps_devices:
            if os.path.exists(dev):
                import serial
                ser = serial.Serial(dev, 9600, timeout=2)
                for _ in range(20):
                    line = ser.readline().decode('utf-8', errors='ignore')
                    if line.startswith('$GPGGA') or line.startswith('$GNGGA'):
                        parts = line.split(',')
                        if len(parts) >= 6 and parts[2] and parts[4]:
                            lat = float(parts[2][:2]) + float(parts[2][2:]) / 60
                            if parts[3] == 'S':
                                lat = -lat
                            lon = float(parts[4][:3]) + float(parts[4][3:]) / 60
                            if parts[5] == 'W':
                                lon = -lon
                            current_location = {'lat': lat, 'lon': lon, 'accuracy': 10}
                            ser.close()
                            return current_location
                ser.close()
    except:
        pass

    return current_location


def gps_update_loop():
    """Background thread for GPS updates."""
    global current_location
    while True:
        try:
            loc = get_gps_location()
            if loc and loc['lat'] != 0:
                socketio.emit('gps_update', loc)
        except Exception as e:
            logger.error(f"GPS error: {e}")
        time.sleep(1)


# ==================== SMS ALERTS ====================

def send_sms_alert(phone_number, message):
    """Send SMS alert using mmcli (ModemManager)."""
    try:
        # Find the modem
        result = subprocess.run(['mmcli', '-L'], capture_output=True, text=True)
        modem_match = re.search(r'/org/freedesktop/ModemManager1/Modem/(\d+)', result.stdout)

        if not modem_match:
            add_log("No modem found for SMS", "ERROR")
            return False

        modem_id = modem_match.group(1)

        # Format US phone number
        if not phone_number.startswith('+'):
            phone_number = '+1' + re.sub(r'\D', '', phone_number)

        # Create and send SMS
        result = subprocess.run([
            'mmcli', '-m', modem_id, '--messaging-create-sms',
            f"text='{message}'", f"number='{phone_number}'"
        ], capture_output=True, text=True)

        sms_match = re.search(r'/org/freedesktop/ModemManager1/SMS/(\d+)', result.stdout)
        if sms_match:
            sms_id = sms_match.group(1)
            subprocess.run(['mmcli', '-s', sms_id, '--send'], capture_output=True)
            add_log(f"SMS sent to {phone_number}", "INFO")
            return True

        add_log(f"Failed to create SMS for {phone_number}", "ERROR")
        return False
    except Exception as e:
        add_log(f"SMS error: {str(e)}", "ERROR")
        return False


def alert_target_found(device):
    """Send alerts when a target is detected."""
    global sms_numbers

    bd_addr = device['bd_address']
    name = device.get('device_name', 'Unknown')

    # Build alert message
    msg = f"BlueK9 ALERT: Target {bd_addr}"
    if name != 'Unknown':
        msg += f" ({name})"
    msg += f" detected!"

    if current_location['lat'] and current_location['lon']:
        msg += f"\nSystem Location: {current_location['lat']:.6f}, {current_location['lon']:.6f}"

    if 'first_seen' in device:
        msg += f"\nFirst seen: {device['first_seen']}"
    if 'last_seen' in device:
        msg += f"\nLast seen: {device['last_seen']}"

    # Send to all configured numbers
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT phone_number FROM sms_numbers WHERE active = 1')
    numbers = [row[0] for row in c.fetchall()]
    conn.close()

    for number in numbers:
        send_sms_alert(number, msg)

    # Emit visual/audio alert
    socketio.emit('target_alert', {
        'device': device,
        'message': msg
    })

    add_log(f"TARGET ALERT: {bd_addr} detected!", "WARNING")


# ==================== SCANNING THREAD ====================

def process_found_device(device_info):
    """Process a found device, update database, check targets."""
    global devices, targets

    bd_addr = device_info['bd_address']
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    # Check if it's a new device or update
    is_new = bd_addr not in devices

    # Update in-memory cache
    if bd_addr in devices:
        devices[bd_addr].update(device_info)
        devices[bd_addr]['last_seen'] = now_str
    else:
        device_info['first_seen'] = now_str
        device_info['last_seen'] = now_str
        devices[bd_addr] = device_info

    # Check if target
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM targets WHERE bd_address = ?', (bd_addr,))
    target = c.fetchone()

    is_target = target is not None
    devices[bd_addr]['is_target'] = is_target

    # Update location estimate if we have RSSI
    rssi = device_info.get('rssi')
    emitter_loc = None
    if rssi:
        emitter_loc = update_device_location(bd_addr, rssi)
        if emitter_loc:
            devices[bd_addr]['emitter_lat'] = emitter_loc[0]
            devices[bd_addr]['emitter_lon'] = emitter_loc[1]
            devices[bd_addr]['emitter_accuracy'] = emitter_loc[2]

    # Log to database
    c.execute('''
        INSERT INTO devices (bd_address, device_name, manufacturer, device_type, rssi,
                           first_seen, last_seen, system_lat, system_lon,
                           emitter_lat, emitter_lon, emitter_accuracy, is_target, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        bd_addr,
        device_info.get('device_name'),
        device_info.get('manufacturer'),
        device_info.get('device_type'),
        rssi,
        devices[bd_addr].get('first_seen'),
        now_str,
        current_location.get('lat'),
        current_location.get('lon'),
        emitter_loc[0] if emitter_loc else None,
        emitter_loc[1] if emitter_loc else None,
        emitter_loc[2] if emitter_loc else None,
        1 if is_target else 0,
        json.dumps(device_info)
    ))
    conn.commit()
    conn.close()

    # Send update to UI
    socketio.emit('device_update', devices[bd_addr])

    # Alert if target
    if is_target and is_new:
        alert_target_found(devices[bd_addr])

    return devices[bd_addr]


def scan_loop():
    """Main scanning loop."""
    global scanning_active, active_radios

    add_log("Scanning started", "INFO")

    while scanning_active:
        try:
            # Get active Bluetooth interfaces
            bt_interfaces = active_radios.get('bluetooth', ['hci0'])

            for iface in bt_interfaces:
                if not scanning_active:
                    break

                # Classic scan
                classic_devices = scan_classic_bluetooth(iface)
                for dev in classic_devices:
                    process_found_device(dev)

                # BLE scan
                ble_devices = scan_ble_devices(iface)
                for dev in ble_devices:
                    process_found_device(dev)

            time.sleep(CONFIG['SCAN_INTERVAL'])
        except Exception as e:
            add_log(f"Scan loop error: {str(e)}", "ERROR")
            time.sleep(2)

    add_log("Scanning stopped", "INFO")


# ==================== RADIO MANAGEMENT ====================

def get_available_radios():
    """Get list of available Bluetooth and WiFi radios."""
    radios = {'bluetooth': [], 'wifi': []}

    # Bluetooth radios
    try:
        result = subprocess.run(['hciconfig', '-a'], capture_output=True, text=True)
        for match in re.finditer(r'^(hci\d+):', result.stdout, re.MULTILINE):
            iface = match.group(1)
            # Get more details
            detail = subprocess.run(['hciconfig', iface], capture_output=True, text=True)
            is_up = 'UP RUNNING' in detail.stdout
            bd_addr_match = re.search(r'BD Address:\s*([0-9A-Fa-f:]+)', detail.stdout)
            bd_addr = bd_addr_match.group(1) if bd_addr_match else 'Unknown'

            radios['bluetooth'].append({
                'interface': iface,
                'bd_address': bd_addr,
                'status': 'up' if is_up else 'down',
                'type': 'bluetooth'
            })
    except Exception as e:
        logger.error(f"Error getting BT radios: {e}")

    # WiFi radios
    try:
        result = subprocess.run(['iw', 'dev'], capture_output=True, text=True)
        current_iface = None
        for line in result.stdout.split('\n'):
            if 'Interface' in line:
                current_iface = line.split()[-1]
            elif current_iface and 'addr' in line:
                mac = line.split()[-1]
                radios['wifi'].append({
                    'interface': current_iface,
                    'mac_address': mac,
                    'status': 'up',
                    'type': 'wifi'
                })
                current_iface = None
    except Exception as e:
        logger.error(f"Error getting WiFi radios: {e}")

    return radios


def enable_radio(interface, radio_type='bluetooth'):
    """Enable a radio interface."""
    try:
        if radio_type == 'bluetooth':
            subprocess.run(['hciconfig', interface, 'up'], check=True)
            add_log(f"Enabled Bluetooth radio {interface}", "INFO")
        else:
            subprocess.run(['ip', 'link', 'set', interface, 'up'], check=True)
            add_log(f"Enabled WiFi radio {interface}", "INFO")
        return True
    except Exception as e:
        add_log(f"Failed to enable {interface}: {str(e)}", "ERROR")
        return False


def disable_radio(interface, radio_type='bluetooth'):
    """Disable a radio interface."""
    try:
        if radio_type == 'bluetooth':
            subprocess.run(['hciconfig', interface, 'down'], check=True)
            add_log(f"Disabled Bluetooth radio {interface}", "INFO")
        else:
            subprocess.run(['ip', 'link', 'set', interface, 'down'], check=True)
            add_log(f"Disabled WiFi radio {interface}", "INFO")
        return True
    except Exception as e:
        add_log(f"Failed to disable {interface}: {str(e)}", "ERROR")
        return False


# ==================== FLASK ROUTES ====================

@app.route('/')
def index():
    """Redirect to login or main app."""
    if 'logged_in' in session:
        return redirect(url_for('main'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page."""
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == CONFIG['DEFAULT_USER'] and password == CONFIG['DEFAULT_PASS']:
            session['logged_in'] = True
            session['username'] = username
            add_log(f"User {username} logged in", "INFO")
            return redirect(url_for('main'))
        else:
            error = 'Invalid credentials'
            add_log(f"Failed login attempt for {username}", "WARNING")

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    """Logout."""
    user = session.get('username', 'Unknown')
    session.clear()
    add_log(f"User {user} logged out", "INFO")
    return redirect(url_for('login'))


@app.route('/app')
@login_required
def main():
    """Main application page."""
    return render_template('main.html',
                         mapbox_token=CONFIG['MAPBOX_TOKEN'],
                         username=session.get('username'))


@app.route('/api/config')
@login_required
def get_config():
    """Get client configuration."""
    return jsonify({
        'mapbox_token': CONFIG['MAPBOX_TOKEN'],
        'scan_interval': CONFIG['SCAN_INTERVAL']
    })


@app.route('/api/scan/start', methods=['POST'])
@login_required
def start_scan():
    """Start scanning."""
    global scanning_active, scan_thread

    if not scanning_active:
        scanning_active = True
        scan_thread = threading.Thread(target=scan_loop, daemon=True)
        scan_thread.start()
        return jsonify({'status': 'started'})
    return jsonify({'status': 'already running'})


@app.route('/api/scan/stop', methods=['POST'])
@login_required
def stop_scan():
    """Stop scanning."""
    global scanning_active
    scanning_active = False
    return jsonify({'status': 'stopped'})


@app.route('/api/scan/stimulate', methods=['POST'])
@login_required
def stimulate_scan():
    """Run stimulation scan."""
    data = request.json or {}
    scan_type = data.get('type', 'both')
    interface = data.get('interface', 'hci0')

    devices_found = []
    if scan_type in ['classic', 'both']:
        devices_found.extend(stimulate_bluetooth_classic(interface))
    if scan_type in ['ble', 'both']:
        devices_found.extend(stimulate_ble_devices(interface))

    for dev in devices_found:
        process_found_device(dev)

    return jsonify({'status': 'completed', 'count': len(devices_found)})


@app.route('/api/devices')
@login_required
def get_devices():
    """Get all detected devices."""
    return jsonify(list(devices.values()))


@app.route('/api/devices/clear', methods=['POST'])
@login_required
def clear_devices():
    """Clear current device list."""
    global devices
    devices = {}
    add_log("Device list cleared", "INFO")
    socketio.emit('devices_cleared')
    return jsonify({'status': 'cleared'})


@app.route('/api/device/<bd_address>/info')
@login_required
def device_info(bd_address):
    """Get detailed info for a device."""
    interface = request.args.get('interface', 'hci0')
    info = get_device_info(bd_address, interface)
    return jsonify(info)


@app.route('/api/targets', methods=['GET', 'POST'])
@login_required
def manage_targets():
    """Get or add targets."""
    conn = get_db()
    c = conn.cursor()

    if request.method == 'POST':
        data = request.json
        bd_address = data.get('bd_address', '').upper()
        alias = data.get('alias', '')
        notes = data.get('notes', '')
        priority = data.get('priority', 1)

        if bd_address:
            try:
                c.execute('''
                    INSERT OR REPLACE INTO targets (bd_address, alias, notes, priority)
                    VALUES (?, ?, ?, ?)
                ''', (bd_address, alias, notes, priority))
                conn.commit()
                add_log(f"Target added: {bd_address}", "INFO")

                # Update in-memory if device exists
                if bd_address in devices:
                    devices[bd_address]['is_target'] = True
                    socketio.emit('device_update', devices[bd_address])

            except Exception as e:
                return jsonify({'error': str(e)}), 400

    c.execute('SELECT * FROM targets')
    targets_list = [dict(row) for row in c.fetchall()]
    conn.close()

    return jsonify(targets_list)


@app.route('/api/targets/<bd_address>', methods=['DELETE'])
@login_required
def delete_target(bd_address):
    """Delete a target."""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM targets WHERE bd_address = ?', (bd_address.upper(),))
    conn.commit()
    conn.close()

    add_log(f"Target removed: {bd_address}", "INFO")

    # Update in-memory if device exists
    if bd_address.upper() in devices:
        devices[bd_address.upper()]['is_target'] = False
        socketio.emit('device_update', devices[bd_address.upper()])

    return jsonify({'status': 'deleted'})


@app.route('/api/sms/numbers', methods=['GET', 'POST'])
@login_required
def manage_sms_numbers():
    """Manage SMS alert numbers."""
    conn = get_db()
    c = conn.cursor()

    if request.method == 'POST':
        data = request.json
        phone = data.get('phone_number', '')

        # Clean phone number
        phone = re.sub(r'\D', '', phone)
        if len(phone) == 10:
            phone = '1' + phone
        if len(phone) == 11 and phone[0] == '1':
            phone = '+' + phone

        if phone:
            try:
                c.execute('SELECT COUNT(*) FROM sms_numbers')
                count = c.fetchone()[0]
                if count >= 10:
                    return jsonify({'error': 'Maximum 10 numbers allowed'}), 400

                c.execute('INSERT OR REPLACE INTO sms_numbers (phone_number) VALUES (?)', (phone,))
                conn.commit()
                add_log(f"SMS number added: {phone}", "INFO")
            except Exception as e:
                return jsonify({'error': str(e)}), 400

    c.execute('SELECT * FROM sms_numbers')
    numbers = [dict(row) for row in c.fetchall()]
    conn.close()

    return jsonify(numbers)


@app.route('/api/sms/numbers/<int:number_id>', methods=['DELETE'])
@login_required
def delete_sms_number(number_id):
    """Delete an SMS number."""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM sms_numbers WHERE id = ?', (number_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'deleted'})


@app.route('/api/radios')
@login_required
def get_radios():
    """Get available radios."""
    return jsonify(get_available_radios())


@app.route('/api/radios/active', methods=['GET', 'POST'])
@login_required
def manage_active_radios():
    """Get or set active radios."""
    global active_radios

    if request.method == 'POST':
        data = request.json
        active_radios = {
            'bluetooth': data.get('bluetooth', []),
            'wifi': data.get('wifi', [])
        }
        add_log(f"Active radios updated: {active_radios}", "INFO")

    return jsonify(active_radios)


@app.route('/api/radios/<interface>/enable', methods=['POST'])
@login_required
def enable_radio_route(interface):
    """Enable a radio."""
    radio_type = request.json.get('type', 'bluetooth')
    success = enable_radio(interface, radio_type)
    return jsonify({'status': 'enabled' if success else 'failed'})


@app.route('/api/radios/<interface>/disable', methods=['POST'])
@login_required
def disable_radio_route(interface):
    """Disable a radio."""
    radio_type = request.json.get('type', 'bluetooth')
    success = disable_radio(interface, radio_type)
    return jsonify({'status': 'disabled' if success else 'failed'})


@app.route('/api/gps')
@login_required
def get_gps():
    """Get current GPS location."""
    return jsonify(current_location)


@app.route('/api/gps/follow', methods=['POST'])
@login_required
def set_gps_follow():
    """Toggle GPS follow mode."""
    global follow_gps
    data = request.json
    follow_gps = data.get('follow', True)
    return jsonify({'follow': follow_gps})


@app.route('/api/logs')
@login_required
def get_logs():
    """Get recent log messages."""
    return jsonify(log_messages[-100:])


@app.route('/api/logs/export')
@login_required
def export_logs():
    """Export device logs for analysis."""
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        SELECT bd_address, device_name, manufacturer, device_type, rssi,
               first_seen, last_seen, system_lat, system_lon,
               emitter_lat, emitter_lon, is_target
        FROM devices
        ORDER BY last_seen DESC
    ''')

    devices_log = [dict(row) for row in c.fetchall()]
    conn.close()

    return jsonify(devices_log)


# ==================== WEBSOCKET EVENTS ====================

@socketio.on('connect')
def handle_connect():
    """Handle WebSocket connection."""
    if 'logged_in' not in session:
        return False
    add_log("Client connected", "INFO")
    # Send current state
    emit('devices_list', list(devices.values()))
    emit('gps_update', current_location)


@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnection."""
    add_log("Client disconnected", "INFO")


@socketio.on('request_device_info')
def handle_device_info_request(data):
    """Handle request for device info."""
    bd_address = data.get('bd_address')
    interface = data.get('interface', 'hci0')
    if bd_address:
        info = get_device_info(bd_address, interface)
        emit('device_info', info)


# ==================== MAIN ====================

def main():
    """Main entry point."""
    init_database()

    # Start GPS thread
    global gps_thread
    gps_thread = threading.Thread(target=gps_update_loop, daemon=True)
    gps_thread.start()

    add_log("BlueK9 Client starting...", "INFO")

    # Run the app
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
