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
    'DEMO_MODE': False,  # Set to True for testing without BT hardware
    # System Identification
    'SYSTEM_ID': 'BK9-001',  # Unique system identifier
    'SYSTEM_NAME': 'BlueK9 Unit 1',  # Human-readable name
    # GPS Configuration
    'GPS_SOURCE': 'nmea_tcp',  # Options: 'gpsd', 'nmea_tcp', 'serial'
    'NMEA_TCP_HOST': '127.0.0.1',
    'NMEA_TCP_PORT': 10110,
    'GPSD_HOST': '127.0.0.1',
    'GPSD_PORT': 2947,
    'GPS_SERIAL_PORT': '/dev/ttyUSB0',
    'GPS_SERIAL_BAUD': 9600,
    # Alert Configuration
    'SMS_ALERT_INTERVAL': 60,  # Seconds between recurring SMS alerts for same target
}

import random

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
btmon_thread = None
btmon_process = None
geo_thread = None
current_location = {'lat': 0.0, 'lon': 0.0, 'accuracy': 0.0}
devices = {}  # BD Address -> device info
targets = {}  # BD Address -> target info
active_radios = {'bluetooth': [], 'wifi': []}
sms_numbers = []
follow_gps = True
log_messages = []
btmon_rssi_cache = {}  # BD Address -> latest RSSI from btmon


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

    # Users table for authentication
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_login DATETIME,
        created_by TEXT
    )''')

    # User preferences table for UI config
    c.execute('''CREATE TABLE IF NOT EXISTS user_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        config_json TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # System settings table
    c.execute('''CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    # Create default admin user if not exists
    import hashlib
    default_pass_hash = hashlib.sha256(CONFIG['DEFAULT_PASS'].encode()).hexdigest()
    c.execute('''INSERT OR IGNORE INTO users (username, password_hash, is_admin)
                 VALUES (?, ?, 1)''', (CONFIG['DEFAULT_USER'], default_pass_hash))

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


def get_device_type(bd_address):
    """Determine if device is Classic or BLE using multiple methods."""
    bd_address = bd_address.upper()

    # Method 1: Check address type pattern
    # BLE random addresses have first nibble C-F (MSB bits 11xxxxxx)
    first_byte = bd_address[:2]
    try:
        first_nibble = int(first_byte[0], 16)
        # Random address check (first two bits = 11 means >= 0xC)
        if first_nibble >= 0xC:
            return 'ble'
    except ValueError:
        pass

    # Method 2: Try bluetoothctl info
    try:
        result = subprocess.run(
            ['bluetoothctl', 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=3
        )
        output = result.stdout.lower()

        # LE indicators
        if 'addresstype: random' in output:
            return 'ble'
        if 'le only' in output or 'advertising' in output:
            return 'ble'

        # Classic indicators - Class of Device is definitive for Classic
        if 'class:' in output and '0x' in output:
            return 'classic'
        if 'icon: phone' in output or 'icon: audio' in output or 'icon: computer' in output:
            return 'classic'

        # Check for specific UUIDs
        if 'uuid: generic access' in output or 'uuid: generic attribute' in output:
            return 'ble'
        if 'uuid: handsfree' in output or 'uuid: a2dp' in output or 'uuid: hfp' in output:
            return 'classic'
        if 'uuid: headset' in output or 'uuid: audio' in output:
            return 'classic'

    except Exception:
        pass

    # Method 3: Try hcitool for classic info (if responds, likely classic)
    try:
        result = subprocess.run(
            ['hcitool', 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=3
        )
        if result.stdout.strip() and 'class:' in result.stdout.lower():
            return 'classic'
        # If hcitool info succeeded with any output, it's likely classic
        if result.returncode == 0 and result.stdout.strip():
            return 'classic'
    except Exception:
        pass

    return 'unknown'


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


# ==================== DEMO MODE ====================

DEMO_DEVICES = [
    {'name': 'iPhone 15 Pro', 'oui': '8C:85:90', 'type': 'classic'},
    {'name': 'Galaxy S24', 'oui': '40:F3:AE', 'type': 'classic'},
    {'name': 'AirPods Pro', 'oui': 'AC:DE:48', 'type': 'ble'},
    {'name': 'Apple Watch', 'oui': 'D0:81:7A', 'type': 'ble'},
    {'name': 'Pixel 8', 'oui': 'F4:F5:D8', 'type': 'classic'},
    {'name': 'Bose QC45', 'oui': '00:15:8D', 'type': 'classic'},
    {'name': 'Tile Tracker', 'oui': '3C:5A:B4', 'type': 'ble'},
    {'name': 'Fitbit Sense', 'oui': 'B8:D6:1A', 'type': 'ble'},
    {'name': 'MacBook Pro', 'oui': '7C:6D:F8', 'type': 'classic'},
    {'name': 'iPad Air', 'oui': '5C:59:48', 'type': 'classic'},
    {'name': 'Tesla Model 3', 'oui': '00:25:67', 'type': 'ble'},
    {'name': 'Unknown Device', 'oui': '00:1A:7D', 'type': 'ble'},
    {'name': 'JBL Speaker', 'oui': '00:02:72', 'type': 'classic'},
    {'name': 'Sony WH-1000XM5', 'oui': '2C:54:CF', 'type': 'classic'},
    {'name': 'Garmin Watch', 'oui': '00:17:C9', 'type': 'ble'},
]

demo_device_pool = {}  # Persistent demo devices

def generate_demo_bd_address(oui):
    """Generate a random BD address with given OUI."""
    suffix = ':'.join([f'{random.randint(0, 255):02X}' for _ in range(3)])
    return f"{oui}:{suffix}"

def generate_demo_devices(count=3, scan_type='both'):
    """Generate demo devices for testing without real BT hardware."""
    global demo_device_pool, current_location

    devices_found = []

    # Set demo location if not set
    if current_location['lat'] == 0:
        current_location = {
            'lat': CONFIG['DEMO_LOCATION']['lat'],
            'lon': CONFIG['DEMO_LOCATION']['lon'],
            'accuracy': 5.0
        }
        socketio.emit('gps_update', current_location)

    # Randomly select devices to "detect"
    available = [d for d in DEMO_DEVICES if scan_type == 'both' or d['type'] == scan_type]
    selected = random.sample(available, min(count, len(available)))

    for device in selected:
        # Check if we've seen this device before (by name)
        existing = next((bd for bd, d in demo_device_pool.items() if d.get('device_name') == device['name']), None)

        if existing:
            bd_addr = existing
        else:
            bd_addr = generate_demo_bd_address(device['oui'])
            demo_device_pool[bd_addr] = {'device_name': device['name']}

        # Generate realistic RSSI (stronger = closer)
        rssi = random.randint(-85, -45)

        # Generate emitter location with some randomness around system location
        offset_lat = random.uniform(-0.001, 0.001)
        offset_lon = random.uniform(-0.001, 0.001)
        emitter_lat = current_location['lat'] + offset_lat
        emitter_lon = current_location['lon'] + offset_lon

        # CEP accuracy based on RSSI (stronger signal = better accuracy)
        cep = max(10, 100 + rssi)  # -45 dBm = 55m, -85 dBm = 15m

        devices_found.append({
            'bd_address': bd_addr,
            'device_name': device['name'],
            'device_type': device['type'],
            'manufacturer': get_manufacturer(bd_addr),
            'rssi': rssi,
            'emitter_lat': emitter_lat,
            'emitter_lon': emitter_lon,
            'emitter_accuracy': cep,
        })

    return devices_found


def scan_classic_bluetooth(interface='hci0'):
    """
    Unified Bluetooth scan using bluetoothctl scan on.
    This scans BOTH Classic and BLE devices simultaneously.
    """
    # DEMO MODE
    if CONFIG.get('DEMO_MODE'):
        add_log(f"[DEMO] BT scan on {interface}", "INFO")
        time.sleep(1)
        devices_found = generate_demo_devices(random.randint(2, 5), 'mixed')
        add_log(f"[DEMO] Scan found {len(devices_found)} devices", "INFO")
        return devices_found

    devices_found = []
    device_rssi = {}
    seen_addresses = set()

    try:
        add_log(f"Starting unified BT scan on {interface} (Classic + LE)", "INFO")

        # Select the controller and power on
        subprocess.run(['bluetoothctl', 'select', interface], capture_output=True, timeout=5)
        subprocess.run(['bluetoothctl', 'power', 'on'], capture_output=True, timeout=5)

        # Run scan using timeout command - bluetoothctl scan on does BOTH Classic and LE
        proc = subprocess.Popen(
            ['stdbuf', '-oL', 'timeout', '10', 'bluetoothctl', 'scan', 'on'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1  # Line buffered
        )

        # Read output in real-time
        try:
            for line in iter(proc.stdout.readline, ''):
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                # Parse [NEW] Device XX:XX:XX:XX:XX:XX Name
                new_match = re.search(r'\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)', line)
                if new_match:
                    bd_addr = new_match.group(1).upper()
                    name = new_match.group(2).strip() or 'Unknown'
                    if bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        devices_found.append({
                            'bd_address': bd_addr,
                            'device_name': name,
                            'device_type': 'unknown',  # Will be determined after scan
                            'manufacturer': get_manufacturer(bd_addr),
                            'rssi': device_rssi.get(bd_addr)
                        })
                        add_log(f"Found device: {bd_addr} ({name})", "DEBUG")

                # Parse [CHG] Device XX:XX:XX:XX:XX:XX RSSI: -XX
                rssi_match = re.search(r'\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+RSSI:\s*(-?\d+)', line)
                if rssi_match:
                    bd_addr = rssi_match.group(1).upper()
                    rssi = int(rssi_match.group(2))
                    device_rssi[bd_addr] = rssi
                    # Update existing device or add new one
                    found = False
                    for dev in devices_found:
                        if dev['bd_address'] == bd_addr:
                            dev['rssi'] = rssi
                            found = True
                            break
                    if not found and bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        devices_found.append({
                            'bd_address': bd_addr,
                            'device_name': 'Unknown',
                            'device_type': 'unknown',
                            'manufacturer': get_manufacturer(bd_addr),
                            'rssi': rssi
                        })
                        add_log(f"Found device via RSSI: {bd_addr} (RSSI: {rssi})", "DEBUG")

                # Parse [CHG] Device XX:XX:XX:XX:XX:XX Name: XXX
                name_match = re.search(r'\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+Name:\s*(.*)', line)
                if name_match:
                    bd_addr = name_match.group(1).upper()
                    name = name_match.group(2).strip()
                    for dev in devices_found:
                        if dev['bd_address'] == bd_addr and name:
                            dev['device_name'] = name

        except Exception as read_err:
            add_log(f"Error reading scan output: {read_err}", "WARNING")
        finally:
            proc.terminate()
            proc.wait()

        # Determine device type for each found device
        classic_count = 0
        ble_count = 0
        for dev in devices_found:
            dev_type = get_device_type(dev['bd_address'])
            dev['device_type'] = dev_type
            if dev_type == 'classic':
                classic_count += 1
            elif dev_type == 'ble':
                ble_count += 1

        # Also get list of cached devices from bluetoothctl
        result = subprocess.run(
            ['bluetoothctl', 'devices'],
            capture_output=True,
            text=True,
            timeout=5
        )

        # Parse "Device XX:XX:XX:XX:XX:XX Name"
        for line in result.stdout.split('\n'):
            match = re.match(r'Device\s+([0-9A-Fa-f:]{17})\s+(.*)', line.strip())
            if match:
                bd_addr = match.group(1).upper()
                name = match.group(2).strip() or 'Unknown'
                if bd_addr not in seen_addresses:
                    seen_addresses.add(bd_addr)
                    dev_type = get_device_type(bd_addr)
                    devices_found.append({
                        'bd_address': bd_addr,
                        'device_name': name,
                        'device_type': dev_type,
                        'manufacturer': get_manufacturer(bd_addr),
                        'rssi': device_rssi.get(bd_addr)
                    })
                    if dev_type == 'classic':
                        classic_count += 1
                    elif dev_type == 'ble':
                        ble_count += 1

        add_log(f"Scan found {len(devices_found)} devices ({classic_count} Classic, {ble_count} BLE)", "INFO")
        return devices_found

    except subprocess.TimeoutExpired:
        add_log("Scan timeout", "WARNING")
        return devices_found
    except Exception as e:
        add_log(f"Scan error: {str(e)}", "ERROR")
        return devices_found


def scan_ble_devices(interface='hci0'):
    """
    Legacy function - now just returns empty list since unified scan handles BLE.
    Kept for compatibility but not used.
    """
    # Unified scan already handles BLE, skip separate BLE scan
    return []


def get_device_rssi(bd_address, interface='hci0'):
    """
    Get RSSI for a specific device by establishing a connection first.
    Uses l2ping or hcitool name to establish connection, then queries RSSI.
    """
    rssi = None

    try:
        # Method 1: Try direct RSSI query first (if already connected)
        result = subprocess.run(
            ['hcitool', '-i', interface, 'rssi', bd_address],
            capture_output=True,
            text=True,
            timeout=3
        )
        match = re.search(r'RSSI return value:\s*(-?\d+)', result.stdout)
        if match:
            rssi = int(match.group(1))
            add_log(f"Got RSSI {rssi} for {bd_address} (direct)", "DEBUG")
            return rssi
    except:
        pass

    try:
        # Method 2: Establish connection with l2ping, then get RSSI
        # l2ping sends L2CAP echo request which establishes a connection
        l2ping_proc = subprocess.Popen(
            ['l2ping', '-i', interface, '-c', '1', '-t', '2', bd_address],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # While l2ping is running, try to get RSSI
        time.sleep(0.5)

        result = subprocess.run(
            ['hcitool', '-i', interface, 'rssi', bd_address],
            capture_output=True,
            text=True,
            timeout=2
        )
        match = re.search(r'RSSI return value:\s*(-?\d+)', result.stdout)
        if match:
            rssi = int(match.group(1))
            add_log(f"Got RSSI {rssi} for {bd_address} (via l2ping)", "DEBUG")

        l2ping_proc.terminate()

        if rssi:
            return rssi
    except Exception as e:
        add_log(f"l2ping RSSI method failed for {bd_address}: {e}", "DEBUG")

    try:
        # Method 3: Use hcitool name to establish connection
        name_proc = subprocess.Popen(
            ['hcitool', '-i', interface, 'name', bd_address],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        time.sleep(0.3)

        result = subprocess.run(
            ['hcitool', '-i', interface, 'rssi', bd_address],
            capture_output=True,
            text=True,
            timeout=2
        )
        match = re.search(r'RSSI return value:\s*(-?\d+)', result.stdout)
        if match:
            rssi = int(match.group(1))
            add_log(f"Got RSSI {rssi} for {bd_address} (via name)", "DEBUG")

        name_proc.terminate()

        if rssi:
            return rssi
    except Exception as e:
        add_log(f"hcitool name RSSI method failed for {bd_address}: {e}", "DEBUG")

    return rssi


def get_rssi_from_bluetoothctl(bd_address):
    """Get RSSI from bluetoothctl info if available."""
    try:
        result = subprocess.run(
            ['bluetoothctl', 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=3
        )
        match = re.search(r'RSSI:\s*(-?\d+)', result.stdout)
        if match:
            return int(match.group(1))
    except:
        pass
    return None


# ==================== BTMON RSSI MONITORING ====================

def parse_btmon_line(line):
    """Parse a single btmon output line for RSSI and BD Address."""
    global btmon_rssi_cache

    # Pattern for HCI Event with Address and RSSI
    # Example: "        Address: 00:11:22:33:44:55 (Public)"
    # Example: "        RSSI: -65 dBm (0xbf)"

    # We need to track current address being reported
    if not hasattr(parse_btmon_line, 'current_addr'):
        parse_btmon_line.current_addr = None

    line = line.strip()

    # Look for BD Address
    addr_match = re.search(r'Address:\s*([0-9A-Fa-f:]{17})', line)
    if addr_match:
        parse_btmon_line.current_addr = addr_match.group(1).upper()

    # Look for RSSI
    rssi_match = re.search(r'RSSI:\s*(-?\d+)\s*dBm', line)
    if rssi_match and parse_btmon_line.current_addr:
        rssi = int(rssi_match.group(1))
        bd_addr = parse_btmon_line.current_addr
        btmon_rssi_cache[bd_addr] = {
            'rssi': rssi,
            'timestamp': time.time()
        }

        # Update device if it exists
        if bd_addr in devices:
            devices[bd_addr]['rssi'] = rssi
            # Emit update to UI
            socketio.emit('device_update', devices[bd_addr])

        return bd_addr, rssi

    return None, None


def btmon_monitor_loop():
    """Background thread to monitor btmon output for RSSI."""
    global btmon_process, scanning_active

    add_log("Starting btmon RSSI monitor...", "INFO")

    try:
        # Start btmon process
        btmon_process = subprocess.Popen(
            ['btmon', '-T'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        while scanning_active and btmon_process.poll() is None:
            try:
                line = btmon_process.stdout.readline()
                if line:
                    parse_btmon_line(line)
            except Exception as e:
                if scanning_active:
                    add_log(f"btmon read error: {e}", "WARNING")
                break

    except Exception as e:
        add_log(f"btmon monitor error: {e}", "ERROR")
    finally:
        stop_btmon()


def start_btmon():
    """Start btmon monitoring thread."""
    global btmon_thread

    if btmon_thread and btmon_thread.is_alive():
        return

    btmon_thread = threading.Thread(target=btmon_monitor_loop, daemon=True)
    btmon_thread.start()
    add_log("btmon RSSI monitor started", "INFO")


def stop_btmon():
    """Stop btmon monitoring."""
    global btmon_process

    if btmon_process:
        try:
            btmon_process.terminate()
            btmon_process.wait(timeout=2)
        except:
            try:
                btmon_process.kill()
            except:
                pass
        btmon_process = None
        add_log("btmon RSSI monitor stopped", "INFO")


def get_btmon_rssi(bd_address):
    """Get cached RSSI from btmon for a device."""
    bd_address = bd_address.upper()
    cached = btmon_rssi_cache.get(bd_address)
    if cached and (time.time() - cached['timestamp']) < 30:  # Cache valid for 30 seconds
        return cached['rssi']
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
    """Get detailed info about a specific device. Runs hcitool info for up to 10 seconds."""
    info = {'bd_address': bd_address}

    try:
        # Run hcitool info with 10 second timeout
        add_log(f"Running hcitool info {bd_address} (10s timeout)...", "INFO")
        result = subprocess.run(
            ['hcitool', '-i', interface, 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=10
        )
        info['raw_info'] = result.stdout

        # Parse device name if available
        name_match = re.search(r'Device Name:\s*(.+)', result.stdout)
        if name_match:
            info['device_name'] = name_match.group(1).strip()

        # Parse device class if available
        class_match = re.search(r'Class:\s*(0x[0-9A-Fa-f]+)', result.stdout)
        if class_match:
            info['device_class'] = class_match.group(1)

        # Parse manufacturer
        mfr_match = re.search(r'Manufacturer:\s*(.+)', result.stdout)
        if mfr_match:
            info['manufacturer_info'] = mfr_match.group(1).strip()

        add_log(f"hcitool info complete for {bd_address}", "INFO")
    except subprocess.TimeoutExpired:
        add_log(f"hcitool info timeout for {bd_address} (10s)", "WARNING")
        info['raw_info'] = "Timeout after 10 seconds - device not responding"
    except Exception as e:
        add_log(f"hcitool info error for {bd_address}: {e}", "ERROR")
        info['raw_info'] = f"Error: {str(e)}"

    return info


# Global state for continuous name retrieval
name_retrieval_active = {}  # bd_address -> bool


def continuous_name_retrieval(bd_address, interface='hci0'):
    """Background thread to continuously try to get device name."""
    global name_retrieval_active

    add_log(f"Starting continuous name retrieval for {bd_address}", "INFO")
    name_retrieval_active[bd_address] = True
    attempt = 0

    while name_retrieval_active.get(bd_address, False):
        attempt += 1
        try:
            # Try hcitool name
            result = subprocess.run(
                ['hcitool', '-i', interface, 'name', bd_address],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.stdout.strip():
                name = result.stdout.strip()
                add_log(f"Got name for {bd_address}: {name} (attempt {attempt})", "INFO")

                # Update device in memory
                if bd_address in devices:
                    devices[bd_address]['device_name'] = name
                    socketio.emit('device_update', devices[bd_address])

                # Emit name result
                socketio.emit('name_result', {
                    'bd_address': bd_address,
                    'name': name,
                    'attempt': attempt,
                    'status': 'found'
                })
            else:
                socketio.emit('name_result', {
                    'bd_address': bd_address,
                    'name': None,
                    'attempt': attempt,
                    'status': 'no_response'
                })

        except subprocess.TimeoutExpired:
            socketio.emit('name_result', {
                'bd_address': bd_address,
                'name': None,
                'attempt': attempt,
                'status': 'timeout'
            })
        except Exception as e:
            add_log(f"Name retrieval error for {bd_address}: {e}", "WARNING")
            socketio.emit('name_result', {
                'bd_address': bd_address,
                'name': None,
                'attempt': attempt,
                'status': 'error',
                'error': str(e)
            })

        # Wait before next attempt
        time.sleep(2)

    add_log(f"Stopped name retrieval for {bd_address} after {attempt} attempts", "INFO")
    name_retrieval_active.pop(bd_address, None)


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


def continuous_geo_loop():
    """Background thread to continuously calculate geolocation for targets."""
    global scanning_active

    add_log("Starting continuous geolocation calculation...", "INFO")

    while scanning_active:
        try:
            # Get all target addresses
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT bd_address FROM targets')
            target_list = [row[0] for row in c.fetchall()]
            conn.close()

            for bd_address in target_list:
                if not scanning_active:
                    break

                # Get recent RSSI observations for this target
                conn = get_db()
                c = conn.cursor()
                c.execute('''
                    SELECT system_lat, system_lon, rssi, timestamp
                    FROM rssi_history
                    WHERE bd_address = ? AND system_lat IS NOT NULL AND rssi IS NOT NULL
                    ORDER BY timestamp DESC
                    LIMIT 50
                ''', (bd_address,))
                observations = c.fetchall()
                conn.close()

                if len(observations) >= 2:
                    # Calculate geolocation
                    location = calculate_geolocation(observations)

                    if location and bd_address in devices:
                        old_cep = devices[bd_address].get('emitter_accuracy')
                        devices[bd_address]['emitter_lat'] = location['lat']
                        devices[bd_address]['emitter_lon'] = location['lon']
                        devices[bd_address]['emitter_accuracy'] = location['cep']

                        # Emit update to UI
                        socketio.emit('device_update', devices[bd_address])

                        # Log if CEP improved significantly
                        if old_cep is None or abs(old_cep - location['cep']) > 5:
                            add_log(f"Geo update for {bd_address}: CEP={location['cep']}m ({len(observations)} obs)", "INFO")

            # Wait 5 seconds before next calculation cycle
            time.sleep(5)

        except Exception as e:
            add_log(f"Geo calculation error: {e}", "WARNING")
            time.sleep(5)

    add_log("Continuous geolocation calculation stopped", "INFO")


def start_geo_thread():
    """Start the continuous geo calculation thread."""
    global geo_thread, scanning_active

    if geo_thread is None or not geo_thread.is_alive():
        geo_thread = threading.Thread(target=continuous_geo_loop, daemon=True)
        geo_thread.start()


def stop_geo_thread():
    """Stop the geo calculation thread."""
    global geo_thread
    # Thread will stop when scanning_active becomes False
    geo_thread = None


# ==================== GPS TRACKING ====================

def parse_nmea_gga(sentence):
    """Parse NMEA GGA sentence for position."""
    try:
        parts = sentence.split(',')
        if len(parts) < 10:
            return None

        # Check for valid fix
        fix_quality = parts[6]
        if fix_quality == '0':
            return None

        lat_raw = parts[2]
        lat_dir = parts[3]
        lon_raw = parts[4]
        lon_dir = parts[5]

        if not lat_raw or not lon_raw:
            return None

        # Parse latitude (DDMM.MMMM)
        lat_deg = float(lat_raw[:2])
        lat_min = float(lat_raw[2:])
        lat = lat_deg + lat_min / 60.0
        if lat_dir == 'S':
            lat = -lat

        # Parse longitude (DDDMM.MMMM)
        lon_deg = float(lon_raw[:3])
        lon_min = float(lon_raw[3:])
        lon = lon_deg + lon_min / 60.0
        if lon_dir == 'W':
            lon = -lon

        # HDOP for accuracy estimate
        hdop = float(parts[8]) if parts[8] else 1.0
        accuracy = hdop * 5  # Rough accuracy in meters

        return {'lat': lat, 'lon': lon, 'accuracy': accuracy}
    except Exception as e:
        return None


def parse_nmea_rmc(sentence):
    """Parse NMEA RMC sentence for position."""
    try:
        parts = sentence.split(',')
        if len(parts) < 10:
            return None

        # Check for valid status
        status = parts[2]
        if status != 'A':
            return None

        lat_raw = parts[3]
        lat_dir = parts[4]
        lon_raw = parts[5]
        lon_dir = parts[6]

        if not lat_raw or not lon_raw:
            return None

        # Parse latitude
        lat_deg = float(lat_raw[:2])
        lat_min = float(lat_raw[2:])
        lat = lat_deg + lat_min / 60.0
        if lat_dir == 'S':
            lat = -lat

        # Parse longitude
        lon_deg = float(lon_raw[:3])
        lon_min = float(lon_raw[3:])
        lon = lon_deg + lon_min / 60.0
        if lon_dir == 'W':
            lon = -lon

        return {'lat': lat, 'lon': lon, 'accuracy': 10.0}
    except Exception as e:
        return None


def get_gps_from_nmea_tcp():
    """Get GPS location from NMEA TCP stream (port 10110)."""
    global current_location
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((CONFIG['NMEA_TCP_HOST'], CONFIG['NMEA_TCP_PORT']))

        buffer = ''
        for _ in range(50):  # Read up to 50 lines
            data = sock.recv(1024).decode('utf-8', errors='ignore')
            buffer += data

            for line in buffer.split('\n'):
                line = line.strip()
                if line.startswith('$GPGGA') or line.startswith('$GNGGA'):
                    result = parse_nmea_gga(line)
                    if result:
                        sock.close()
                        return result
                elif line.startswith('$GPRMC') or line.startswith('$GNRMC'):
                    result = parse_nmea_rmc(line)
                    if result:
                        sock.close()
                        return result

            # Keep only incomplete line in buffer
            if '\n' in buffer:
                buffer = buffer.split('\n')[-1]

        sock.close()
    except Exception as e:
        add_log(f"NMEA TCP error: {str(e)}", "WARNING")

    return None


def get_gps_from_gpsd():
    """Get GPS location from GPSD."""
    global current_location
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((CONFIG['GPSD_HOST'], CONFIG['GPSD_PORT']))
        sock.send(b'?WATCH={"enable":true,"json":true}')

        buffer = ''
        for _ in range(20):
            data = sock.recv(4096).decode('utf-8', errors='ignore')
            buffer += data

            for line in buffer.split('\n'):
                line = line.strip()
                if line.startswith('{"class":"TPV"'):
                    try:
                        tpv = json.loads(line)
                        if 'lat' in tpv and 'lon' in tpv:
                            sock.close()
                            return {
                                'lat': tpv['lat'],
                                'lon': tpv['lon'],
                                'accuracy': tpv.get('epx', 10)
                            }
                    except:
                        pass

        sock.close()
    except Exception as e:
        add_log(f"GPSD error: {str(e)}", "WARNING")

    return None


def get_gps_from_serial():
    """Get GPS location from serial device."""
    global current_location

    try:
        import serial
        port = CONFIG['GPS_SERIAL_PORT']
        baud = CONFIG['GPS_SERIAL_BAUD']

        if not os.path.exists(port):
            return None

        ser = serial.Serial(port, baud, timeout=2)

        for _ in range(30):
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line.startswith('$GPGGA') or line.startswith('$GNGGA'):
                result = parse_nmea_gga(line)
                if result:
                    ser.close()
                    return result
            elif line.startswith('$GPRMC') or line.startswith('$GNRMC'):
                result = parse_nmea_rmc(line)
                if result:
                    ser.close()
                    return result

        ser.close()
    except Exception as e:
        add_log(f"GPS Serial error: {str(e)}", "WARNING")

    return None


def get_gps_location():
    """Get GPS location based on configured source."""
    global current_location

    gps_source = CONFIG.get('GPS_SOURCE', 'gpsd')

    if gps_source == 'nmea_tcp':
        result = get_gps_from_nmea_tcp()
    elif gps_source == 'gpsd':
        result = get_gps_from_gpsd()
    elif gps_source == 'serial':
        result = get_gps_from_serial()
    else:
        # Try all sources
        result = get_gps_from_nmea_tcp() or get_gps_from_gpsd() or get_gps_from_serial()

    if result:
        current_location = result

    return current_location


def gps_update_loop():
    """Background thread for GPS updates."""
    global current_location
    add_log(f"GPS thread started, source: {CONFIG.get('GPS_SOURCE', 'auto')}", "INFO")

    while True:
        try:
            loc = get_gps_location()
            if loc and loc.get('lat') and loc['lat'] != 0:
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

        # Escape message for shell
        safe_message = message.replace("'", "'\\''")

        # Create SMS using correct mmcli syntax
        create_result = subprocess.run([
            'mmcli', '-m', modem_id,
            f"--messaging-create-sms=text='{safe_message}',number='{phone_number}'"
        ], capture_output=True, text=True)

        add_log(f"mmcli create output: {create_result.stdout} {create_result.stderr}", "DEBUG")

        sms_match = re.search(r'/org/freedesktop/ModemManager1/SMS/(\d+)', create_result.stdout)
        if sms_match:
            sms_id = sms_match.group(1)
            send_result = subprocess.run(
                ['mmcli', '-s', sms_id, '--send'],
                capture_output=True,
                text=True,
                timeout=30
            )
            if send_result.returncode == 0:
                add_log(f"SMS sent to {phone_number}", "INFO")
                return True
            else:
                add_log(f"SMS send failed: {send_result.stderr}", "ERROR")
                return False

        add_log(f"Failed to create SMS for {phone_number}: {create_result.stderr}", "ERROR")
        return False
    except Exception as e:
        add_log(f"SMS error: {str(e)}", "ERROR")
        return False


# Track last SMS alert time per target
target_last_sms_alert = {}

def alert_target_found(device, is_new=True):
    """Send alerts when a target is detected."""
    global sms_numbers, target_last_sms_alert

    bd_addr = device['bd_address']
    name = device.get('device_name', 'Unknown')
    rssi = device.get('rssi', 'N/A')
    now = time.time()

    # Build alert message with system info
    system_id = CONFIG.get('SYSTEM_ID', 'BK9-001')
    system_name = CONFIG.get('SYSTEM_NAME', 'BlueK9')

    msg = f"[{system_id}] TARGET ALERT\n"
    msg += f"Device: {bd_addr}"
    if name and name != 'Unknown':
        msg += f" ({name})"
    msg += f"\nRSSI: {rssi} dBm" if rssi != 'N/A' else ""

    if current_location['lat'] and current_location['lon']:
        msg += f"\nLoc: {current_location['lat']:.6f}, {current_location['lon']:.6f}"
        # Add Google Maps link
        msg += f"\nhttps://maps.google.com/?q={current_location['lat']:.6f},{current_location['lon']:.6f}"

    msg += f"\nTime: {datetime.now().strftime('%H:%M:%S')}"

    # Check if we should send SMS (new target or interval elapsed)
    should_send_sms = False
    last_sms_time = target_last_sms_alert.get(bd_addr, 0)
    sms_interval = CONFIG.get('SMS_ALERT_INTERVAL', 60)

    if is_new or (now - last_sms_time) >= sms_interval:
        should_send_sms = True
        target_last_sms_alert[bd_addr] = now

    # Send SMS to all configured numbers
    if should_send_sms:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT phone_number FROM sms_numbers WHERE active = 1')
        numbers = [row[0] for row in c.fetchall()]
        conn.close()

        for number in numbers:
            send_sms_alert(number, msg)

        # Log recurring SMS alerts
        if not is_new:
            add_log(f"[{system_id}] Recurring SMS sent for {bd_addr} (RSSI: {rssi})", "INFO")

    # Only emit visual/audio alert for new targets (avoid constant beeping)
    if is_new:
        socketio.emit('target_alert', {
            'device': device,
            'message': msg,
            'system_id': system_id,
            'system_name': system_name,
            'location': current_location if current_location['lat'] else None
        })
        loc_str = ""
        if current_location['lat'] and current_location['lon']:
            loc_str = f" @ {current_location['lat']:.6f},{current_location['lon']:.6f}"
        add_log(f"[{system_id}] TARGET ALERT: {bd_addr} detected! RSSI: {rssi}{loc_str}", "WARNING")


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

    # Get RSSI - try multiple methods
    rssi = device_info.get('rssi')

    # Priority 1: Check btmon cache (most reliable real-time RSSI)
    if rssi is None:
        rssi = get_btmon_rssi(bd_addr)
        if rssi:
            add_log(f"btmon RSSI for {bd_addr}: {rssi} dBm", "DEBUG")

    # Priority 2: Try bluetoothctl info
    if rssi is None:
        rssi = get_rssi_from_bluetoothctl(bd_addr)

    # Priority 3: Try active connection methods (Classic BT only)
    if rssi is None and device_info.get('device_type') != 'ble':
        rssi = get_device_rssi(bd_addr)

    if rssi:
        device_info['rssi'] = rssi
        devices[bd_addr]['rssi'] = rssi

    # Update location estimate if we have RSSI
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

    # Alert if target - call for all targets (not just new) to handle recurring SMS
    if is_target:
        alert_target_found(devices[bd_addr], is_new=is_new)

    return devices[bd_addr]


def scan_loop():
    """Main scanning loop."""
    global scanning_active, active_radios

    add_log("Scanning started", "INFO")

    while scanning_active:
        try:
            # Get active Bluetooth interfaces (default to hci0 if none configured)
            bt_interfaces = active_radios.get('bluetooth', [])
            if not bt_interfaces:
                bt_interfaces = ['hci0']  # Default fallback

            for iface in bt_interfaces:
                if not scanning_active:
                    break

                # Unified scan - handles both Classic and BLE with bluetoothctl scan on
                all_devices = scan_classic_bluetooth(iface)
                for dev in all_devices:
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
    import hashlib
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # Authenticate against database
        conn = get_db()
        c = conn.cursor()
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        c.execute('SELECT id, is_admin FROM users WHERE username = ? AND password_hash = ?',
                  (username, password_hash))
        user = c.fetchone()

        if user:
            session['logged_in'] = True
            session['username'] = username
            session['is_admin'] = bool(user['is_admin'])

            # Update last login
            c.execute('UPDATE users SET last_login = datetime("now") WHERE username = ?', (username,))
            conn.commit()
            conn.close()

            add_log(f"User {username} logged in", "INFO")
            return redirect(url_for('main'))
        else:
            conn.close()
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
                         username=session.get('username'),
                         is_admin=session.get('is_admin', False))


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
        # Start btmon for RSSI monitoring
        start_btmon()
        # Start continuous geo calculation
        start_geo_thread()
        # Start scan loop
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
    # Stop btmon
    stop_btmon()
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


@app.route('/api/device/<bd_address>/name')
@login_required
def device_name(bd_address):
    """Get device name using bluetoothctl/hcitool."""
    try:
        # Try bluetoothctl info first
        result = subprocess.run(
            ['bluetoothctl', 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=5
        )
        name_match = re.search(r'Name:\s*(.+)', result.stdout)
        if name_match:
            name = name_match.group(1).strip()
            add_log(f"Got name for {bd_address}: {name}", "INFO")
            return jsonify({'name': name, 'bd_address': bd_address})

        # Try hcitool name as fallback
        result = subprocess.run(
            ['hcitool', 'name', bd_address],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.stdout.strip():
            name = result.stdout.strip()
            add_log(f"Got name for {bd_address}: {name}", "INFO")
            return jsonify({'name': name, 'bd_address': bd_address})

        return jsonify({'name': None, 'bd_address': bd_address, 'error': 'Could not retrieve name'})
    except Exception as e:
        add_log(f"Error getting name for {bd_address}: {e}", "ERROR")
        return jsonify({'name': None, 'bd_address': bd_address, 'error': str(e)})


@app.route('/api/device/<bd_address>/name/start', methods=['POST'])
@login_required
def start_continuous_name(bd_address):
    """Start continuous name retrieval for a device."""
    global name_retrieval_active

    bd_address = bd_address.upper()

    if name_retrieval_active.get(bd_address):
        return jsonify({'status': 'already_running', 'bd_address': bd_address})

    # Start background thread for continuous name retrieval
    thread = threading.Thread(
        target=continuous_name_retrieval,
        args=(bd_address,),
        daemon=True
    )
    thread.start()

    add_log(f"Started continuous name retrieval for {bd_address}", "INFO")
    return jsonify({'status': 'started', 'bd_address': bd_address})


@app.route('/api/device/<bd_address>/name/stop', methods=['POST'])
@login_required
def stop_continuous_name(bd_address):
    """Stop continuous name retrieval for a device."""
    global name_retrieval_active

    bd_address = bd_address.upper()

    if bd_address in name_retrieval_active:
        name_retrieval_active[bd_address] = False
        add_log(f"Stopping name retrieval for {bd_address}", "INFO")
        return jsonify({'status': 'stopped', 'bd_address': bd_address})

    return jsonify({'status': 'not_running', 'bd_address': bd_address})


@app.route('/api/device/<bd_address>/locate', methods=['POST'])
@login_required
def device_locate(bd_address):
    """Calculate device geolocation from RSSI history."""
    bd_address = bd_address.upper()

    # Get RSSI history from database
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT system_lat, system_lon, rssi, timestamp
        FROM rssi_history
        WHERE bd_address = ? AND system_lat IS NOT NULL AND rssi IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 50
    ''', (bd_address,))
    observations = c.fetchall()
    conn.close()

    if len(observations) < 2:
        # Check why we might not have data
        if not current_location.get('lat') or not current_location.get('lon'):
            hint = " (No GPS fix - system location required)"
        else:
            hint = " (Scan with device in range to collect RSSI data)"
        return jsonify({
            'location': None,
            'message': f'Insufficient data: need at least 2 observations, have {len(observations)}{hint}'
        })

    # Calculate geolocation using weighted centroid algorithm
    location = calculate_geolocation(observations)

    if location:
        # Update device in memory
        if bd_address in devices:
            devices[bd_address]['emitter_lat'] = location['lat']
            devices[bd_address]['emitter_lon'] = location['lon']
            devices[bd_address]['emitter_accuracy'] = location['cep']
            socketio.emit('device_update', devices[bd_address])

        return jsonify({'location': location, 'observations': len(observations)})
    else:
        return jsonify({'location': None, 'message': 'Could not calculate location'})


@app.route('/api/device/<bd_address>/geo/reset', methods=['POST'])
@login_required
def reset_device_geo(bd_address):
    """Reset RSSI history for a device to restart geolocation."""
    bd_address = bd_address.upper()

    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM rssi_history WHERE bd_address = ?', (bd_address,))
    deleted = c.rowcount
    conn.commit()
    conn.close()

    # Clear geo data from device
    if bd_address in devices:
        devices[bd_address]['emitter_lat'] = None
        devices[bd_address]['emitter_lon'] = None
        devices[bd_address]['emitter_accuracy'] = None
        socketio.emit('device_update', devices[bd_address])

    add_log(f"Geo reset for {bd_address}: {deleted} observations cleared", "INFO")
    return jsonify({'status': 'reset', 'cleared': deleted})


@app.route('/api/geo/reset_all', methods=['POST'])
@login_required
def reset_all_geo():
    """Reset all RSSI history for all devices."""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM rssi_history')
    deleted = c.rowcount
    conn.commit()
    conn.close()

    # Clear geo data from all devices
    for bd_addr in devices:
        devices[bd_addr]['emitter_lat'] = None
        devices[bd_addr]['emitter_lon'] = None
        devices[bd_addr]['emitter_accuracy'] = None
        socketio.emit('device_update', devices[bd_addr])

    add_log(f"All geo data reset: {deleted} observations cleared", "INFO")
    return jsonify({'status': 'reset', 'cleared': deleted})


@app.route('/api/breadcrumbs')
@login_required
def get_breadcrumbs():
    """Get RSSI breadcrumb positions for TARGETS ONLY (heatmap)."""
    conn = get_db()
    c = conn.cursor()
    # Only get breadcrumbs for devices that are targets
    c.execute('''
        SELECT r.bd_address, r.system_lat, r.system_lon, r.rssi, r.timestamp
        FROM rssi_history r
        INNER JOIN targets t ON r.bd_address = t.bd_address
        WHERE r.system_lat IS NOT NULL AND r.system_lon IS NOT NULL AND r.rssi IS NOT NULL
        ORDER BY r.timestamp DESC
        LIMIT 1000
    ''')
    points = [{'bd_address': row[0], 'lat': row[1], 'lon': row[2], 'rssi': row[3], 'time': row[4]} for row in c.fetchall()]
    conn.close()
    return jsonify(points)


@app.route('/api/breadcrumbs/reset', methods=['POST'])
@login_required
def reset_breadcrumbs():
    """Clear all breadcrumb data."""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM rssi_history')
    deleted = c.rowcount
    conn.commit()
    conn.close()

    add_log(f"Breadcrumbs reset: {deleted} points cleared", "INFO")
    return jsonify({'status': 'reset', 'cleared': deleted})


@app.route('/api/system_trail')
@login_required
def get_system_trail():
    """Get system GPS position trail (where the system has been)."""
    conn = get_db()
    c = conn.cursor()
    # Get unique system positions
    c.execute('''
        SELECT DISTINCT system_lat, system_lon, MAX(timestamp) as last_time
        FROM rssi_history
        WHERE system_lat IS NOT NULL AND system_lon IS NOT NULL
        GROUP BY ROUND(system_lat, 5), ROUND(system_lon, 5)
        ORDER BY last_time DESC
        LIMIT 500
    ''')
    points = [{'lat': row[0], 'lon': row[1], 'time': row[2]} for row in c.fetchall()]
    conn.close()
    return jsonify(points)


@app.route('/api/system_trail/reset', methods=['POST'])
@login_required
def reset_system_trail():
    """Clear system trail data (same as breadcrumbs reset)."""
    return reset_breadcrumbs()


def calculate_geolocation(observations):
    """
    Calculate emitter geolocation using RSSI-weighted centroid algorithm.

    Algorithm:
    1. Convert RSSI to estimated distance using path loss model
    2. Weight each observation inversely by distance (closer = more weight)
    3. Calculate weighted centroid of all system positions
    4. Estimate CEP (Circular Error Probable) at 95% confidence

    Path Loss Model: RSSI = TxPower - 10 * n * log10(d)
    Where: TxPower ~ -59 dBm at 1m, n ~ 2-4 depending on environment
    """
    import math

    # Path loss parameters (can be tuned)
    TX_POWER = -59  # RSSI at 1 meter (typical for BT)
    PATH_LOSS_EXP = 2.5  # Path loss exponent (2=free space, 3-4=indoor)

    weighted_lat = 0
    weighted_lon = 0
    total_weight = 0
    distances = []

    for obs in observations:
        lat, lon, rssi, _ = obs

        # Convert RSSI to distance estimate
        # d = 10 ^ ((TxPower - RSSI) / (10 * n))
        try:
            distance = math.pow(10, (TX_POWER - rssi) / (10 * PATH_LOSS_EXP))
            distance = max(1, min(distance, 500))  # Clamp to 1-500m
        except:
            distance = 50  # Default fallback

        distances.append(distance)

        # Weight inversely by distance squared (closer observations matter more)
        weight = 1.0 / (distance * distance)

        weighted_lat += lat * weight
        weighted_lon += lon * weight
        total_weight += weight

    if total_weight == 0:
        return None

    # Calculate weighted centroid
    est_lat = weighted_lat / total_weight
    est_lon = weighted_lon / total_weight

    # Calculate CEP (Circular Error Probable) at 95% confidence
    # Use RMS of distances as proxy for uncertainty
    avg_distance = sum(distances) / len(distances)
    variance = sum((d - avg_distance) ** 2 for d in distances) / len(distances)
    std_dev = math.sqrt(variance)

    # CEP95 ~ 2.45 * standard deviation for 2D normal distribution
    # Scale by average distance and observation count
    cep = max(5, min(avg_distance * 0.5 + std_dev * 2.0, 200))

    # Reduce CEP with more observations (better confidence)
    cep = cep * math.sqrt(10 / len(observations))

    return {
        'lat': est_lat,
        'lon': est_lon,
        'cep': round(cep, 1),
        'confidence': min(95, 50 + len(observations) * 3),
        'method': 'rssi_weighted_centroid',
        'observations': len(observations)
    }


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


@app.route('/api/system/stats')
@login_required
def get_system_stats():
    """Get system hardware statistics (CPU, memory, temperature)."""
    stats = {
        'cpu_percent': None,
        'memory_percent': None,
        'cpu_temp': None,
        'disk_percent': None,
        'uptime': None
    }

    try:
        # CPU usage
        result = subprocess.run(
            ['grep', 'cpu ', '/proc/stat'],
            capture_output=True, text=True, timeout=2
        )
        if result.stdout:
            fields = result.stdout.split()
            if len(fields) >= 5:
                idle = int(fields[4])
                total = sum(int(x) for x in fields[1:8])
                # Store for delta calculation
                if not hasattr(get_system_stats, 'last_cpu'):
                    get_system_stats.last_cpu = (total, idle)
                last_total, last_idle = get_system_stats.last_cpu
                total_delta = total - last_total
                idle_delta = idle - last_idle
                if total_delta > 0:
                    stats['cpu_percent'] = round(100 * (1 - idle_delta / total_delta), 1)
                get_system_stats.last_cpu = (total, idle)
    except Exception:
        pass

    try:
        # Memory usage
        result = subprocess.run(['free', '-m'], capture_output=True, text=True, timeout=2)
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 3:
                    total = int(parts[1])
                    used = int(parts[2])
                    stats['memory_percent'] = round(100 * used / total, 1) if total > 0 else 0
    except Exception:
        pass

    try:
        # CPU temperature (Raspberry Pi and others)
        temp_paths = [
            '/sys/class/thermal/thermal_zone0/temp',
            '/sys/class/hwmon/hwmon0/temp1_input',
            '/sys/devices/virtual/thermal/thermal_zone0/temp'
        ]
        for temp_path in temp_paths:
            try:
                with open(temp_path, 'r') as f:
                    temp = int(f.read().strip())
                    stats['cpu_temp'] = round(temp / 1000, 1)  # Convert milli-celsius to celsius
                    break
            except FileNotFoundError:
                continue
    except Exception:
        pass

    try:
        # Disk usage
        result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=2)
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    # Parse percentage (remove %)
                    stats['disk_percent'] = int(parts[4].replace('%', ''))
    except Exception:
        pass

    try:
        # Uptime
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            stats['uptime'] = f"{hours}h {minutes}m"
    except Exception:
        pass

    return jsonify(stats)


@app.route('/api/gps/follow', methods=['POST'])
@login_required
def set_gps_follow():
    """Toggle GPS follow mode."""
    global follow_gps
    data = request.json
    follow_gps = data.get('follow', True)
    return jsonify({'follow': follow_gps})


@app.route('/api/gps/config', methods=['GET', 'POST'])
@login_required
def gps_config():
    """Get or set GPS configuration."""
    if request.method == 'POST':
        data = request.json

        if 'source' in data:
            CONFIG['GPS_SOURCE'] = data['source']
        if 'nmea_host' in data:
            CONFIG['NMEA_TCP_HOST'] = data['nmea_host']
        if 'nmea_port' in data:
            CONFIG['NMEA_TCP_PORT'] = int(data['nmea_port'])
        if 'gpsd_host' in data:
            CONFIG['GPSD_HOST'] = data['gpsd_host']
        if 'gpsd_port' in data:
            CONFIG['GPSD_PORT'] = int(data['gpsd_port'])
        if 'serial_port' in data:
            CONFIG['GPS_SERIAL_PORT'] = data['serial_port']
        if 'serial_baud' in data:
            CONFIG['GPS_SERIAL_BAUD'] = int(data['serial_baud'])

        add_log(f"GPS config updated: source={CONFIG['GPS_SOURCE']}", "INFO")
        return jsonify({'status': 'updated'})

    # GET - return current config
    return jsonify({
        'source': CONFIG.get('GPS_SOURCE', 'nmea_tcp'),
        'nmea_host': CONFIG.get('NMEA_TCP_HOST', '127.0.0.1'),
        'nmea_port': CONFIG.get('NMEA_TCP_PORT', 10110),
        'gpsd_host': CONFIG.get('GPSD_HOST', '127.0.0.1'),
        'gpsd_port': CONFIG.get('GPSD_PORT', 2947),
        'serial_port': CONFIG.get('GPS_SERIAL_PORT', '/dev/ttyUSB0'),
        'serial_baud': CONFIG.get('GPS_SERIAL_BAUD', 9600),
        'current_location': current_location
    })


@app.route('/api/gps/test', methods=['POST'])
@login_required
def test_gps():
    """Test GPS connection with current settings."""
    source = CONFIG.get('GPS_SOURCE', 'nmea_tcp')

    add_log(f"Testing GPS source: {source}", "INFO")

    if source == 'nmea_tcp':
        result = get_gps_from_nmea_tcp()
    elif source == 'gpsd':
        result = get_gps_from_gpsd()
    elif source == 'serial':
        result = get_gps_from_serial()
    else:
        result = None

    if result:
        add_log(f"GPS test successful: {result['lat']:.6f}, {result['lon']:.6f}", "INFO")
        return jsonify({'status': 'success', 'location': result})
    else:
        add_log(f"GPS test failed for source: {source}", "WARNING")
        return jsonify({'status': 'failed', 'error': f'Could not connect to {source}'})


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def manage_settings():
    """Get or update system settings."""
    conn = get_db()
    c = conn.cursor()

    if request.method == 'POST':
        data = request.json

        # Update CONFIG and store in database
        settings_map = {
            'system_id': 'SYSTEM_ID',
            'system_name': 'SYSTEM_NAME',
            'gps_source': 'GPS_SOURCE',
            'nmea_host': 'NMEA_TCP_HOST',
            'nmea_port': 'NMEA_TCP_PORT',
            'gpsd_host': 'GPSD_HOST',
            'gpsd_port': 'GPSD_PORT',
            'serial_port': 'GPS_SERIAL_PORT',
            'serial_baud': 'GPS_SERIAL_BAUD',
            'sms_alert_interval': 'SMS_ALERT_INTERVAL',
        }

        for key, config_key in settings_map.items():
            if key in data:
                value = data[key]
                # Convert to int for numeric fields
                if key in ['nmea_port', 'gpsd_port', 'serial_baud', 'sms_alert_interval']:
                    value = int(value)
                CONFIG[config_key] = value
                # Store in database
                c.execute('''INSERT OR REPLACE INTO system_settings (key, value, updated_at)
                            VALUES (?, ?, datetime('now'))''', (config_key, str(value)))

        conn.commit()
        add_log(f"System settings updated by {session.get('username')}", "INFO")
        conn.close()
        return jsonify({'status': 'updated'})

    # GET - return current settings
    conn.close()
    return jsonify({
        'system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
        'system_name': CONFIG.get('SYSTEM_NAME', 'BlueK9 Unit 1'),
        'gps_source': CONFIG.get('GPS_SOURCE', 'nmea_tcp'),
        'nmea_host': CONFIG.get('NMEA_TCP_HOST', '127.0.0.1'),
        'nmea_port': CONFIG.get('NMEA_TCP_PORT', 10110),
        'gpsd_host': CONFIG.get('GPSD_HOST', '127.0.0.1'),
        'gpsd_port': CONFIG.get('GPSD_PORT', 2947),
        'serial_port': CONFIG.get('GPS_SERIAL_PORT', '/dev/ttyUSB0'),
        'serial_baud': CONFIG.get('GPS_SERIAL_BAUD', 9600),
        'sms_alert_interval': CONFIG.get('SMS_ALERT_INTERVAL', 60),
    })


@app.route('/api/users', methods=['GET', 'POST'])
@login_required
def manage_users():
    """Get users or create a new user (admin only)."""
    import hashlib
    conn = get_db()
    c = conn.cursor()

    if request.method == 'POST':
        # Check if current user is admin
        current_user = session.get('username')
        c.execute('SELECT is_admin FROM users WHERE username = ?', (current_user,))
        user_row = c.fetchone()
        if not user_row or not user_row['is_admin']:
            conn.close()
            return jsonify({'error': 'Admin access required'}), 403

        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')
        is_admin = 1 if data.get('is_admin') else 0

        if not username or not password:
            conn.close()
            return jsonify({'error': 'Username and password required'}), 400

        # Check if user exists
        c.execute('SELECT id FROM users WHERE username = ?', (username,))
        if c.fetchone():
            conn.close()
            return jsonify({'error': 'User already exists'}), 400

        password_hash = hashlib.sha256(password.encode()).hexdigest()
        c.execute('''INSERT INTO users (username, password_hash, is_admin, created_by)
                    VALUES (?, ?, ?, ?)''', (username, password_hash, is_admin, current_user))
        conn.commit()
        add_log(f"User {username} created by {current_user}", "INFO")
        conn.close()
        return jsonify({'status': 'created', 'username': username})

    # GET - return all users
    c.execute('SELECT id, username, is_admin, created_at, last_login, created_by FROM users')
    users = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(users)


@app.route('/api/users/<username>', methods=['DELETE'])
@login_required
def delete_user(username):
    """Delete a user (admin only)."""
    conn = get_db()
    c = conn.cursor()

    # Check if current user is admin
    current_user = session.get('username')
    c.execute('SELECT is_admin FROM users WHERE username = ?', (current_user,))
    user_row = c.fetchone()
    if not user_row or not user_row['is_admin']:
        conn.close()
        return jsonify({'error': 'Admin access required'}), 403

    # Can't delete yourself or the default admin
    if username == current_user:
        conn.close()
        return jsonify({'error': 'Cannot delete yourself'}), 400
    if username == CONFIG['DEFAULT_USER']:
        conn.close()
        return jsonify({'error': 'Cannot delete default admin'}), 400

    c.execute('DELETE FROM users WHERE username = ?', (username,))
    if c.rowcount > 0:
        conn.commit()
        add_log(f"User {username} deleted by {current_user}", "INFO")
        conn.close()
        return jsonify({'status': 'deleted'})
    else:
        conn.close()
        return jsonify({'error': 'User not found'}), 404


@app.route('/api/users/password', methods=['POST'])
@login_required
def change_password():
    """Change password for current user."""
    import hashlib
    conn = get_db()
    c = conn.cursor()

    data = request.json
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')

    if not current_password or not new_password:
        conn.close()
        return jsonify({'error': 'Current and new password required'}), 400

    username = session.get('username')
    current_hash = hashlib.sha256(current_password.encode()).hexdigest()

    # Verify current password
    c.execute('SELECT id FROM users WHERE username = ? AND password_hash = ?',
              (username, current_hash))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Current password incorrect'}), 401

    # Update password
    new_hash = hashlib.sha256(new_password.encode()).hexdigest()
    c.execute('UPDATE users SET password_hash = ? WHERE username = ?', (new_hash, username))
    conn.commit()
    add_log(f"Password changed for user {username}", "INFO")
    conn.close()
    return jsonify({'status': 'changed'})


@app.route('/api/config/export')
@login_required
def export_config():
    """Export user's UI configuration."""
    conn = get_db()
    c = conn.cursor()

    username = session.get('username')
    c.execute('SELECT config_json FROM user_config WHERE username = ?', (username,))
    row = c.fetchone()
    conn.close()

    if row and row['config_json']:
        config = json.loads(row['config_json'])
    else:
        config = {}

    # Include system settings for admin
    config['exported_at'] = datetime.now().isoformat()
    config['exported_by'] = username

    return jsonify(config)


@app.route('/api/config/import', methods=['POST'])
@login_required
def import_config():
    """Import user's UI configuration."""
    conn = get_db()
    c = conn.cursor()

    data = request.json
    username = session.get('username')

    # Store config
    config_json = json.dumps(data)
    c.execute('''INSERT OR REPLACE INTO user_config (username, config_json, updated_at)
                VALUES (?, ?, datetime('now'))''', (username, config_json))
    conn.commit()
    add_log(f"Config imported for user {username}", "INFO")
    conn.close()

    return jsonify({'status': 'imported'})


@app.route('/api/config/layout', methods=['POST'])
@login_required
def save_layout():
    """Save user's layout configuration."""
    conn = get_db()
    c = conn.cursor()

    data = request.json
    username = session.get('username')

    # Get existing config
    c.execute('SELECT config_json FROM user_config WHERE username = ?', (username,))
    row = c.fetchone()

    if row and row['config_json']:
        config = json.loads(row['config_json'])
    else:
        config = {}

    # Update layout section
    config['layout'] = data

    # Store config
    config_json = json.dumps(config)
    c.execute('''INSERT OR REPLACE INTO user_config (username, config_json, updated_at)
                VALUES (?, ?, datetime('now'))''', (username, config_json))
    conn.commit()
    conn.close()

    return jsonify({'status': 'saved'})


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
