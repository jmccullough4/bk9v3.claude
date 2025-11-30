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
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
from flask_socketio import SocketIO, emit
import struct

# Dynamically determine install directory (parent of client/)
INSTALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    # Display Configuration
    'TIMEZONE': 'UTC',  # Timezone for display (e.g., 'America/New_York', 'America/Los_Angeles')
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
import uuid
SESSION_ID = str(uuid.uuid4())[:8]  # Unique ID for this session, changes on restart

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
follow_gps = False  # Disabled by default
log_messages = []
btmon_rssi_cache = {}  # BD Address -> latest RSSI from btmon
btmon_device_cache = {}  # BD Address -> full device info from btmon

# Ubertooth state
ubertooth_process = None
ubertooth_thread = None
ubertooth_running = False
ubertooth_data = {}  # LAP -> piconet info (UAP, channel, clock)

# WARHAMMER Network state (NetBird-based mesh network)
# Uses local `netbird status -d` for peer discovery (no API token needed)
WARHAMMER_CONFIG = {
    'NETWORK_NAME': 'WARHAMMER'
}
warhammer_peers = {}  # peer_id -> peer info (name, ip, location, status)
warhammer_routes = {}  # route_id -> route info
warhammer_monitor_thread = None
warhammer_running = False
peer_locations = {}  # system_id -> {lat, lon, timestamp}

# Cellular signal state
cellular_signal = {
    'bars': 0,
    'rssi': None,
    'quality': 0,
    'technology': None,
    'operator': None,
    'imei': None,
    'imsi': None,
    'iccid': None,
    'phone_number': None
}
cellular_monitor_thread = None
cellular_running = False

# Bluetooth Company Identifiers (common ones)
# Full list at: https://www.bluetooth.com/specifications/assigned-numbers/company-identifiers/
BT_COMPANY_IDS = {
    0x0000: "Ericsson Technology Licensing",
    0x0001: "Nokia Mobile Phones",
    0x0002: "Intel Corp.",
    0x0003: "IBM Corp.",
    0x0004: "Toshiba Corp.",
    0x0006: "Microsoft",
    0x000A: "Qualcomm",
    0x000D: "Texas Instruments",
    0x000F: "Broadcom Corporation",
    0x001D: "Qualcomm Technologies",
    0x004C: "Apple, Inc.",
    0x0059: "Nordic Semiconductor",
    0x0075: "Samsung Electronics",
    0x0087: "Garmin International",
    0x00E0: "Google",
    0x00D2: "Dialog Semiconductor",
    0x0131: "Huawei Technologies",
    0x0157: "Xiaomi Inc.",
    0x0171: "Amazon.com Services",
    0x01B0: "Fitbit, Inc.",
    0x022B: "Facebook Technologies",
    0x02FF: "JBL",
    0x038F: "Tile, Inc.",
    0x0310: "Harman International",
    0x0301: "Sony Corporation",
    0x0046: "Sony Ericsson Mobile",
    0x0047: "Vizio, Inc.",
    0x00B0: "LG Electronics",
    0x0154: "Bose Corporation",
    0x018D: "Logitech International",
    0x0822: "Meta Platforms",
}

# LMP Version to Bluetooth Core Spec version mapping
LMP_VERSION_MAP = {
    0x00: ("1.0b", "Original Bluetooth"),
    0x01: ("1.1", "IEEE 802.15.1-2002"),
    0x02: ("1.2", "AFH, eSCO, faster connection"),
    0x03: ("2.0+EDR", "Enhanced Data Rate 3Mbps"),
    0x04: ("2.1+EDR", "Secure Simple Pairing"),
    0x05: ("3.0+HS", "High Speed (WiFi), Enhanced Power Control"),
    0x06: ("4.0", "Bluetooth Low Energy (BLE) introduced"),
    0x07: ("4.1", "Coexistence with LTE, LE privacy"),
    0x08: ("4.2", "LE Data Packet Length Extension, LE Secure Connections"),
    0x09: ("5.0", "2x speed, 4x range, 8x broadcast capacity"),
    0x0a: ("5.1", "Direction Finding (AoA/AoD)"),
    0x0b: ("5.2", "LE Audio, EATT, LE Power Control"),
    0x0c: ("5.3", "Enhanced Periodic Advertising, Connection Subrating"),
    0x0d: ("5.4", "Advertising Coding Selection, PAwR"),
    0x0e: ("6.0", "Bluetooth Channel Sounding for distance measurement"),
}

# Bluetooth Feature Byte Descriptions (Page 0)
# Reference: Bluetooth Core Spec Vol 2 Part C Section 3.3
BT_FEATURES_PAGE0 = {
    # Byte 0
    (0, 0): "3-slot packets",
    (0, 1): "5-slot packets",
    (0, 2): "Encryption",
    (0, 3): "Slot offset",
    (0, 4): "Timing accuracy",
    (0, 5): "Role switch",
    (0, 6): "Hold mode",
    (0, 7): "Sniff mode",
    # Byte 1
    (1, 1): "Power control requests",
    (1, 2): "Channel quality driven",
    (1, 3): "SCO link",
    (1, 4): "HV2 packets",
    (1, 5): "HV3 packets",
    (1, 6): "μ-law log",
    (1, 7): "A-law log",
    # Byte 2
    (2, 0): "CVSD synchronous",
    (2, 1): "Paging parameter negotiation",
    (2, 2): "Power control",
    (2, 3): "Transparent SCO data",
    (2, 4): "Flow control lag (LSB)",
    (2, 5): "Flow control lag (mid)",
    (2, 6): "Flow control lag (MSB)",
    (2, 7): "Broadcast Encryption",
    # Byte 3
    (3, 1): "Enhanced Data Rate ACL 2 Mbps",
    (3, 2): "Enhanced Data Rate ACL 3 Mbps",
    (3, 3): "Enhanced inquiry scan",
    (3, 4): "Interlaced inquiry scan",
    (3, 5): "Interlaced page scan",
    (3, 6): "RSSI with inquiry results",
    (3, 7): "EV3 packets (eSCO)",
    # Byte 4
    (4, 0): "EV4 packets",
    (4, 1): "EV5 packets",
    (4, 3): "AFH capable slave",
    (4, 4): "AFH classification slave",
    (4, 5): "BR/EDR Not Supported (BLE only)",
    (4, 6): "LE Supported (Controller)",
    (4, 7): "3-slot EDR ACL packets",
    # Byte 5
    (5, 0): "5-slot EDR ACL packets",
    (5, 1): "Sniff subrating",
    (5, 2): "Pause encryption",
    (5, 3): "AFH capable master",
    (5, 4): "AFH classification master",
    (5, 5): "EDR eSCO 2 Mbps",
    (5, 6): "EDR eSCO 3 Mbps",
    (5, 7): "3-slot EDR eSCO",
    # Byte 6
    (6, 0): "Extended inquiry response",
    (6, 1): "Simultaneous LE and BR/EDR (Controller)",
    (6, 3): "Secure Simple Pairing (Controller)",
    (6, 4): "Encapsulated PDU",
    (6, 5): "Erroneous data reporting",
    (6, 6): "Non-flushable packet boundary",
    # Byte 7
    (7, 0): "Link Supervision Timeout Event",
    (7, 1): "Inquiry TX Power Level",
    (7, 2): "Enhanced Power Control",
}


def decode_lmp_version(lmp_version):
    """Decode LMP version to human-readable Bluetooth version."""
    if isinstance(lmp_version, str):
        # Parse hex string like "0xe" or "14"
        try:
            if lmp_version.startswith('0x'):
                lmp_version = int(lmp_version, 16)
            else:
                lmp_version = int(lmp_version)
        except ValueError:
            return {"version": "Unknown", "description": f"Could not parse: {lmp_version}"}

    version_info = LMP_VERSION_MAP.get(lmp_version)
    if version_info:
        return {
            "lmp_version": lmp_version,
            "bt_version": version_info[0],
            "description": version_info[1]
        }
    return {
        "lmp_version": lmp_version,
        "bt_version": f"Unknown (LMP {lmp_version})",
        "description": "Version not recognized"
    }


def decode_feature_bytes(feature_hex_string):
    """
    Decode Bluetooth feature bytes into human-readable capabilities.
    Input: "0xbf 0xfe 0x2d 0xfe 0xdb 0xff 0x7b 0x87" or similar
    """
    features = []

    # Parse hex bytes
    try:
        # Handle various formats
        hex_values = feature_hex_string.replace(',', ' ').split()
        bytes_list = []
        for val in hex_values:
            val = val.strip()
            if val.startswith('0x'):
                bytes_list.append(int(val, 16))
            elif val:
                try:
                    bytes_list.append(int(val, 16))
                except ValueError:
                    continue
    except Exception:
        return ["Could not parse features"]

    # Decode each bit
    for byte_idx, byte_val in enumerate(bytes_list):
        for bit_idx in range(8):
            if byte_val & (1 << bit_idx):
                feature_name = BT_FEATURES_PAGE0.get((byte_idx, bit_idx))
                if feature_name:
                    features.append(feature_name)

    return features


def parse_device_info_output(raw_output):
    """
    Parse hcitool info output and return structured, human-readable data.
    """
    result = {
        'raw': raw_output,
        'parsed': {},
        'analysis': []
    }

    if not raw_output or 'Timeout' in raw_output or 'Error' in raw_output:
        result['analysis'].append("Device did not respond to info request")
        return result

    # Parse BD Address
    addr_match = re.search(r'BD Address:\s*([0-9A-Fa-f:]{17})', raw_output)
    if addr_match:
        result['parsed']['bd_address'] = addr_match.group(1).upper()

    # Parse Device Name
    name_match = re.search(r'Device Name:\s*(.+?)(?:\n|$)', raw_output)
    if name_match:
        result['parsed']['device_name'] = name_match.group(1).strip()
        result['analysis'].append(f"Device identifies as: {result['parsed']['device_name']}")

    # Parse LMP Version
    lmp_match = re.search(r'LMP Version:\s*.*?\((0x[0-9a-fA-F]+)\)\s*LMP Subversion:\s*(0x[0-9a-fA-F]+)', raw_output)
    if lmp_match:
        lmp_version = lmp_match.group(1)
        lmp_subversion = lmp_match.group(2)
        version_info = decode_lmp_version(lmp_version)
        result['parsed']['lmp_version'] = lmp_version
        result['parsed']['lmp_subversion'] = lmp_subversion
        result['parsed']['bluetooth_version'] = version_info['bt_version']
        result['parsed']['version_description'] = version_info['description']
        result['analysis'].append(f"Bluetooth {version_info['bt_version']}: {version_info['description']}")

    # Parse Manufacturer
    mfr_match = re.search(r'Manufacturer:\s*(.+?)(?:\n|$)', raw_output)
    if mfr_match:
        result['parsed']['manufacturer'] = mfr_match.group(1).strip()
        result['analysis'].append(f"Made by: {result['parsed']['manufacturer']}")

    # Parse Features Page 0
    features_match = re.search(r'Features page 0:\s*([0-9a-fA-Fx\s]+)', raw_output)
    if features_match:
        features_hex = features_match.group(1).strip()
        features_list = decode_feature_bytes(features_hex)
        result['parsed']['features_hex'] = features_hex
        result['parsed']['features'] = features_list

        # Categorize features for analysis
        if features_list:
            # Check for key capabilities
            has_edr = any('EDR' in f for f in features_list)
            has_ble = any('LE' in f or 'BLE' in f for f in features_list)
            has_ssp = any('Simple Pairing' in f for f in features_list)
            has_afh = any('AFH' in f for f in features_list)

            if has_edr:
                result['analysis'].append("Supports Enhanced Data Rate (EDR) - faster Classic BT transfers")
            if has_ble:
                result['analysis'].append("Supports Bluetooth Low Energy (BLE)")
            if has_ssp:
                result['analysis'].append("Supports Secure Simple Pairing (modern pairing)")
            if has_afh:
                result['analysis'].append("Supports Adaptive Frequency Hopping (better interference handling)")

            # Add capability summary
            result['parsed']['capabilities_summary'] = {
                'edr': has_edr,
                'ble': has_ble,
                'secure_pairing': has_ssp,
                'afh': has_afh,
                'total_features': len(features_list)
            }

    # Parse Device Class if present
    class_match = re.search(r'Class:\s*(0x[0-9A-Fa-f]+)', raw_output)
    if class_match:
        device_class = class_match.group(1)
        result['parsed']['device_class'] = device_class
        # Decode device class (Major Device Class is bits 8-12)
        try:
            class_val = int(device_class, 16)
            major_class = (class_val >> 8) & 0x1F
            major_classes = {
                0: "Miscellaneous",
                1: "Computer",
                2: "Phone",
                3: "LAN/Network",
                4: "Audio/Video",
                5: "Peripheral",
                6: "Imaging",
                7: "Wearable",
                8: "Toy",
                9: "Health"
            }
            device_type = major_classes.get(major_class, f"Unknown ({major_class})")
            result['parsed']['device_type_class'] = device_type
            result['analysis'].append(f"Device type: {device_type}")
        except ValueError:
            pass

    return result


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

    # Device info logs - detailed analysis from hcitool info
    c.execute('''CREATE TABLE IF NOT EXISTS device_info_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bd_address TEXT NOT NULL,
        device_name TEXT,
        bluetooth_version TEXT,
        version_description TEXT,
        manufacturer TEXT,
        device_class TEXT,
        device_type_class TEXT,
        features TEXT,
        capabilities TEXT,
        analysis TEXT,
        raw_output TEXT,
        system_lat REAL,
        system_lon REAL,
        queried_at DATETIME DEFAULT CURRENT_TIMESTAMP
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


def load_settings_from_db():
    """Load persisted settings from database on startup."""
    global CONFIG
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT key, value FROM system_settings')
        rows = c.fetchall()
        conn.close()

        # Map of database keys to their types for proper conversion
        int_keys = {'NMEA_TCP_PORT', 'GPSD_PORT', 'GPS_SERIAL_BAUD', 'SMS_ALERT_INTERVAL'}

        for row in rows:
            key = row['key']
            value = row['value']
            if key in CONFIG:
                # Convert to appropriate type
                if key in int_keys:
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        continue
                CONFIG[key] = value
                logger.info(f"Loaded setting {key} from database")

        logger.info(f"Settings loaded: GPS_SOURCE={CONFIG.get('GPS_SOURCE')}, SYSTEM_ID={CONFIG.get('SYSTEM_ID')}")
    except Exception as e:
        logger.warning(f"Could not load settings from database: {e}")


def get_targets_from_db():
    """Get all targets from database as a list of dicts."""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT bd_address, alias, notes, priority FROM targets')
        targets_list = []
        for row in c.fetchall():
            targets_list.append({
                'bd_address': row['bd_address'],
                'alias': row['alias'] or '',
                'notes': row['notes'] or '',
                'priority': row['priority'] or 1
            })
        conn.close()
        return targets_list
    except Exception as e:
        logger.warning(f"Could not load targets from database: {e}")
        return []


def save_target_to_db(bd_address, alias='', notes='', priority=1, source=''):
    """Save a target to the database (used for peer sync)."""
    try:
        conn = get_db()
        c = conn.cursor()
        # Check if target already exists
        c.execute('SELECT bd_address FROM targets WHERE bd_address = ?', (bd_address.upper(),))
        existing = c.fetchone()
        if not existing:
            c.execute('''
                INSERT INTO targets (bd_address, alias, notes, priority)
                VALUES (?, ?, ?, ?)
            ''', (bd_address.upper(), alias, notes, priority))
            conn.commit()
            conn.close()
            add_log(f"Target synced from {source}: {bd_address}", "INFO")
            return True
        conn.close()
        return False
    except Exception as e:
        logger.warning(f"Could not save target to database: {e}")
        return False


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
    # Expanded Bluetooth OUI database
    oui_db = {
        # Apple
        '000393': 'Apple, Inc.',
        '000A27': 'Apple, Inc.',
        '000A95': 'Apple, Inc.',
        '000D93': 'Apple, Inc.',
        '0010FA': 'Apple, Inc.',
        '001451': 'Apple, Inc.',
        '0016CB': 'Apple, Inc.',
        '0017F2': 'Apple, Inc.',
        '0019E3': 'Apple, Inc.',
        '001B63': 'Apple, Inc.',
        '001CB3': 'Apple, Inc.',
        '001D4F': 'Apple, Inc.',
        '001E52': 'Apple, Inc.',
        '001EC2': 'Apple, Inc.',
        '001F5B': 'Apple, Inc.',
        '001FF3': 'Apple, Inc.',
        '002241': 'Apple, Inc.',
        '002312': 'Apple, Inc.',
        '002332': 'Apple, Inc.',
        '002436': 'Apple, Inc.',
        '0025BC': 'Apple, Inc.',
        '0025DB': 'Apple, Inc.',
        '002608': 'Apple, Inc.',
        '0026B0': 'Apple, Inc.',
        '0026BB': 'Apple, Inc.',
        '003065': 'Apple, Inc.',
        '00309D': 'Apple, Inc.',
        '003EE1': 'Apple, Inc.',
        '0050E4': 'Apple, Inc.',
        '005E0C': 'Apple, Inc.',
        '006171': 'Apple, Inc.',
        '006D52': 'Apple, Inc.',
        '008865': 'Apple, Inc.',
        '00B362': 'Apple, Inc.',
        '00C610': 'Apple, Inc.',
        '00CDFE': 'Apple, Inc.',
        '00F4B9': 'Apple, Inc.',
        '00F76F': 'Apple, Inc.',
        '041552': 'Apple, Inc.',
        '045453': 'Apple, Inc.',
        '04D3CF': 'Apple, Inc.',
        '04DB56': 'Apple, Inc.',
        '04E536': 'Apple, Inc.',
        '04F13E': 'Apple, Inc.',
        '04F7E4': 'Apple, Inc.',
        '086698': 'Apple, Inc.',
        '0898E0': 'Apple, Inc.',
        '08F4AB': 'Apple, Inc.',
        '0C3021': 'Apple, Inc.',
        '0C4DE9': 'Apple, Inc.',
        '0C74C2': 'Apple, Inc.',
        '0C771A': 'Apple, Inc.',
        '0CBC9F': 'Apple, Inc.',
        '10417F': 'Apple, Inc.',
        '109ADD': 'Apple, Inc.',
        '10DDB1': 'Apple, Inc.',
        '14109F': 'Apple, Inc.',
        '1499E2': 'Apple, Inc.',
        '14BD61': 'Apple, Inc.',
        '18AF61': 'Apple, Inc.',
        '18AF8F': 'Apple, Inc.',
        '18E7F4': 'Apple, Inc.',
        '18EE69': 'Apple, Inc.',
        '18F643': 'Apple, Inc.',
        '1C1AC0': 'Apple, Inc.',
        '1C36BB': 'Apple, Inc.',
        '1C5CF2': 'Apple, Inc.',
        '1C9148': 'Apple, Inc.',
        '1C9E46': 'Apple, Inc.',
        '1CABA7': 'Apple, Inc.',
        '20768F': 'Apple, Inc.',
        '207D74': 'Apple, Inc.',
        '209BCD': 'Apple, Inc.',
        '20A286': 'Apple, Inc.',
        '20C9D0': 'Apple, Inc.',
        '244B03': 'Apple, Inc.',
        '24A074': 'Apple, Inc.',
        '24A2E1': 'Apple, Inc.',
        '24AB81': 'Apple, Inc.',
        '24E314': 'Apple, Inc.',
        '24F094': 'Apple, Inc.',
        '280B5C': 'Apple, Inc.',
        '283737': 'Apple, Inc.',
        '285AEB': 'Apple, Inc.',
        '286ABA': 'Apple, Inc.',
        '28A02B': 'Apple, Inc.',
        '28CFDA': 'Apple, Inc.',
        '28CFE9': 'Apple, Inc.',
        '28E02C': 'Apple, Inc.',
        '28E14C': 'Apple, Inc.',
        '28E7CF': 'Apple, Inc.',
        '28F076': 'Apple, Inc.',
        '2C1F23': 'Apple, Inc.',
        '2C200B': 'Apple, Inc.',
        '2C3361': 'Apple, Inc.',
        '2C3F38': 'Apple, Inc.',
        '2CB43A': 'Apple, Inc.',
        '2CBE08': 'Apple, Inc.',
        '2CF0A2': 'Apple, Inc.',
        '2CF0EE': 'Apple, Inc.',
        '30636B': 'Apple, Inc.',
        '3090AB': 'Apple, Inc.',
        '30F7C5': 'Apple, Inc.',
        '34363B': 'Apple, Inc.',
        '34C059': 'Apple, Inc.',
        '34E2FD': 'Apple, Inc.',
        '380F4A': 'Apple, Inc.',
        '3871DE': 'Apple, Inc.',
        '38484C': 'Apple, Inc.',
        '38B54D': 'Apple, Inc.',
        '38C986': 'Apple, Inc.',
        '38CADA': 'Apple, Inc.',
        '3C0754': 'Apple, Inc.',
        '3C15C2': 'Apple, Inc.',
        '3C2EFF': 'Apple, Inc.',
        '3CE072': 'Apple, Inc.',
        '3CFEC5': 'Apple, Inc.',
        '400971': 'Apple, Inc.',
        '403004': 'Apple, Inc.',
        '40331A': 'Apple, Inc.',
        '403CFC': 'Apple, Inc.',
        '406C8F': 'Apple, Inc.',
        '40A6D9': 'Apple, Inc.',
        '40B395': 'Apple, Inc.',
        '40D32D': 'Apple, Inc.',
        '442A60': 'Apple, Inc.',
        '4480EB': 'Apple, Inc.',
        '44D884': 'Apple, Inc.',
        '44FB42': 'Apple, Inc.',
        '483B38': 'Apple, Inc.',
        '48437C': 'Apple, Inc.',
        '4860BC': 'Apple, Inc.',
        '48746E': 'Apple, Inc.',
        '48A195': 'Apple, Inc.',
        '48BF6B': 'Apple, Inc.',
        '48D705': 'Apple, Inc.',
        '48E9F1': 'Apple, Inc.',
        '4C32D6': 'Apple, Inc.',
        '4C3275': 'Apple, Inc.',
        '4C5789': 'Apple, Inc.',
        '4C74BF': 'Apple, Inc.',
        '4C8D79': 'Apple, Inc.',
        '4CB199': 'Apple, Inc.',
        '5082D5': 'Apple, Inc.',
        '50EAD6': 'Apple, Inc.',
        '542696': 'Apple, Inc.',
        '544E90': 'Apple, Inc.',
        '5477A9': 'Apple, Inc.',
        '54724F': 'Apple, Inc.',
        '549963': 'Apple, Inc.',
        '54AE27': 'Apple, Inc.',
        '54E43A': 'Apple, Inc.',
        '54EAA8': 'Apple, Inc.',
        '58404E': 'Apple, Inc.',
        '5855CA': 'Apple, Inc.',
        '5C59A8': 'Apple, Inc.',
        '5C5948': 'Apple, Inc.',
        '5C8D4E': 'Apple, Inc.',
        '5C95AE': 'Apple, Inc.',
        '5C969D': 'Apple, Inc.',
        '5C97F3': 'Apple, Inc.',
        '5CF938': 'Apple, Inc.',
        '5CFCFE': 'Apple, Inc.',
        '600308': 'Apple, Inc.',
        '60334B': 'Apple, Inc.',
        '60C547': 'Apple, Inc.',
        '60D9C7': 'Apple, Inc.',
        '60F445': 'Apple, Inc.',
        '60F81D': 'Apple, Inc.',
        '60FACD': 'Apple, Inc.',
        '60FEC5': 'Apple, Inc.',
        '645AED': 'Apple, Inc.',
        '64A3CB': 'Apple, Inc.',
        '64B0A6': 'Apple, Inc.',
        '64E682': 'Apple, Inc.',
        '680927': 'Apple, Inc.',
        '685B35': 'Apple, Inc.',
        '6896E8': 'Apple, Inc.',
        '68A86D': 'Apple, Inc.',
        '68AE20': 'Apple, Inc.',
        '68D93C': 'Apple, Inc.',
        '68DBCA': 'Apple, Inc.',
        '68FB7E': 'Apple, Inc.',
        '6C19C0': 'Apple, Inc.',
        '6C3E6D': 'Apple, Inc.',
        '6C4008': 'Apple, Inc.',
        '6C709F': 'Apple, Inc.',
        '6C72E7': 'Apple, Inc.',
        '6C8DC1': 'Apple, Inc.',
        '6CAB31': 'Apple, Inc.',
        '6CC26B': 'Apple, Inc.',
        '70700D': 'Apple, Inc.',
        '7073CB': 'Apple, Inc.',
        '70A2B3': 'Apple, Inc.',
        '70CD60': 'Apple, Inc.',
        '70DEE2': 'Apple, Inc.',
        '70EC50': 'Apple, Inc.',
        '7451BA': 'Apple, Inc.',
        '749EAF': 'Apple, Inc.',
        '74E1B6': 'Apple, Inc.',
        '78009E': 'Apple, Inc.',
        '782327': 'Apple, Inc.',
        '783A84': 'Apple, Inc.',
        '786C1C': 'Apple, Inc.',
        '787B8A': 'Apple, Inc.',
        '78886D': 'Apple, Inc.',
        '78A3E4': 'Apple, Inc.',
        '78CA39': 'Apple, Inc.',
        '78D75F': 'Apple, Inc.',
        '78FD94': 'Apple, Inc.',
        '7C0191': 'Apple, Inc.',
        '7C04D0': 'Apple, Inc.',
        '7C11BE': 'Apple, Inc.',
        '7C5049': 'Apple, Inc.',
        '7C6DF8': 'Apple, Inc.',
        '7C6D62': 'Apple, Inc.',
        '7CC3A1': 'Apple, Inc.',
        '7CD1C3': 'Apple, Inc.',
        '7CF05F': 'Apple, Inc.',
        '80006E': 'Apple, Inc.',
        '802154': 'Apple, Inc.',
        '804971': 'Apple, Inc.',
        '8088C8': 'Apple, Inc.',
        '809B20': 'Apple, Inc.',
        '80929F': 'Apple, Inc.',
        '80BE05': 'Apple, Inc.',
        '80E650': 'Apple, Inc.',
        '80EA96': 'Apple, Inc.',
        '8441FC': 'Apple, Inc.',
        '84788B': 'Apple, Inc.',
        '848506': 'Apple, Inc.',
        '8489AD': 'Apple, Inc.',
        '8488C8': 'Apple, Inc.',
        '84A134': 'Apple, Inc.',
        '84B153': 'Apple, Inc.',
        '84FCAC': 'Apple, Inc.',
        '84FCFE': 'Apple, Inc.',
        '88036B': 'Apple, Inc.',
        '881908': 'Apple, Inc.',
        '886650': 'Apple, Inc.',
        '8866A5': 'Apple, Inc.',
        '889E68': 'Apple, Inc.',
        '88C663': 'Apple, Inc.',
        '88CB87': 'Apple, Inc.',
        '88E87F': 'Apple, Inc.',
        '8C006D': 'Apple, Inc.',
        '8C2937': 'Apple, Inc.',
        '8C2DAA': 'Apple, Inc.',
        '8C5877': 'Apple, Inc.',
        '8C7B9D': 'Apple, Inc.',
        '8C7C92': 'Apple, Inc.',
        '8C8590': 'Apple, Inc.',
        '8C8FE9': 'Apple, Inc.',
        '8CFABA': 'Apple, Inc.',
        '90003E': 'Apple, Inc.',
        '901711': 'Apple, Inc.',
        '903C92': 'Apple, Inc.',
        '908D6C': 'Apple, Inc.',
        '9099FA': 'Apple, Inc.',
        '90B21F': 'Apple, Inc.',
        '90B931': 'Apple, Inc.',
        '90C1C6': 'Apple, Inc.',
        '90FD61': 'Apple, Inc.',
        '9417C5': 'Apple, Inc.',
        '94BF2D': 'Apple, Inc.',
        '94E979': 'Apple, Inc.',
        '94F6A3': 'Apple, Inc.',
        '98460A': 'Apple, Inc.',
        '98B8E3': 'Apple, Inc.',
        '98D6BB': 'Apple, Inc.',
        '98E0D9': 'Apple, Inc.',
        '98F0AB': 'Apple, Inc.',
        '98FE94': 'Apple, Inc.',
        '9C04EB': 'Apple, Inc.',
        '9C207B': 'Apple, Inc.',
        '9C293F': 'Apple, Inc.',
        '9C35EB': 'Apple, Inc.',
        '9C4FDA': 'Apple, Inc.',
        '9C84BF': 'Apple, Inc.',
        '9CF387': 'Apple, Inc.',
        '9CF48E': 'Apple, Inc.',
        'A01828': 'Apple, Inc.',
        'A03BE3': 'Apple, Inc.',
        'A43135': 'Apple, Inc.',
        'A46706': 'Apple, Inc.',
        'A4B197': 'Apple, Inc.',
        'A4C361': 'Apple, Inc.',
        'A4D18C': 'Apple, Inc.',
        'A4D1D2': 'Apple, Inc.',
        'A4F1E8': 'Apple, Inc.',
        'A82066': 'Apple, Inc.',
        'A85B78': 'Apple, Inc.',
        'A860B6': 'Apple, Inc.',
        'A88808': 'Apple, Inc.',
        'A88E24': 'Apple, Inc.',
        'A8968A': 'Apple, Inc.',
        'A8BBCF': 'Apple, Inc.',
        'A8FAD8': 'Apple, Inc.',
        'AC293A': 'Apple, Inc.',
        'AC3C0B': 'Apple, Inc.',
        'ACBC32': 'Apple, Inc.',
        'ACCF5C': 'Apple, Inc.',
        'ACDE48': 'Apple, Inc.',
        'ACFDEC': 'Apple, Inc.',
        'B03495': 'Apple, Inc.',
        'B035E5': 'Apple, Inc.',
        'B04BBF': 'Apple, Inc.',
        'B065BD': 'Apple, Inc.',
        'B09FBA': 'Apple, Inc.',
        'B418D1': 'Apple, Inc.',
        'B4F0AB': 'Apple, Inc.',
        'B8098A': 'Apple, Inc.',
        'B817C2': 'Apple, Inc.',
        'B844D9': 'Apple, Inc.',
        'B85E7B': 'Apple, Inc.',
        'B863BC': 'Apple, Inc.',
        'B88D12': 'Apple, Inc.',
        'B8C75D': 'Apple, Inc.',
        'B8E856': 'Apple, Inc.',
        'B8F6B1': 'Apple, Inc.',
        'B8FF61': 'Apple, Inc.',
        'BC3BAF': 'Apple, Inc.',
        'BC4CC4': 'Apple, Inc.',
        'BC5436': 'Apple, Inc.',
        'BC6778': 'Apple, Inc.',
        'BC9256': 'Apple, Inc.',
        'BCA920': 'Apple, Inc.',
        'BCE632': 'Apple, Inc.',
        'BC9FAF': 'Apple, Inc.',
        'C01ADA': 'Apple, Inc.',
        'C06394': 'Apple, Inc.',
        'C0847A': 'Apple, Inc.',
        'C09F42': 'Apple, Inc.',
        'C0A53E': 'Apple, Inc.',
        'C0B658': 'Apple, Inc.',
        'C0CCF8': 'Apple, Inc.',
        'C0D012': 'Apple, Inc.',
        'C0CECD': 'Apple, Inc.',
        'C0F2FB': 'Apple, Inc.',
        'C4618B': 'Apple, Inc.',
        'C4B301': 'Apple, Inc.',
        'C81EE7': 'Apple, Inc.',
        'C82A14': 'Apple, Inc.',
        'C8334B': 'Apple, Inc.',
        'C869CD': 'Apple, Inc.',
        'C86F1D': 'Apple, Inc.',
        'C8B5B7': 'Apple, Inc.',
        'C8BCC8': 'Apple, Inc.',
        'C8D083': 'Apple, Inc.',
        'C8E0EB': 'Apple, Inc.',
        'C8F650': 'Apple, Inc.',
        'CC088D': 'Apple, Inc.',
        'CC20E8': 'Apple, Inc.',
        'CC25EF': 'Apple, Inc.',
        'CC29F5': 'Apple, Inc.',
        'CC4463': 'Apple, Inc.',
        'CC785F': 'Apple, Inc.',
        'CC08E0': 'Apple, Inc.',
        'CCC760': 'Apple, Inc.',
        'D004CD': 'Apple, Inc.',
        'D023DB': 'Apple, Inc.',
        'D02598': 'Apple, Inc.',
        'D03311': 'Apple, Inc.',
        'D0817A': 'Apple, Inc.',
        'D0A637': 'Apple, Inc.',
        'D0C5F3': 'Apple, Inc.',
        'D0D2B0': 'Apple, Inc.',
        'D0E140': 'Apple, Inc.',
        'D42F7B': 'Apple, Inc.',
        'D49A20': 'Apple, Inc.',
        'D4619D': 'Apple, Inc.',
        'D4B8DB': 'Apple, Inc.',
        'D4DCCD': 'Apple, Inc.',
        'D4F46F': 'Apple, Inc.',
        'D83062': 'Apple, Inc.',
        'D89E3F': 'Apple, Inc.',
        'D8A25E': 'Apple, Inc.',
        'D8BB2C': 'Apple, Inc.',
        'D8CF9C': 'Apple, Inc.',
        'D8D1CB': 'Apple, Inc.',
        'DC0C5C': 'Apple, Inc.',
        'DC2B2A': 'Apple, Inc.',
        'DC2B61': 'Apple, Inc.',
        'DC37C9': 'Apple, Inc.',
        'DC3714': 'Apple, Inc.',
        'DC415F': 'Apple, Inc.',
        'DC56E6': 'Apple, Inc.',
        'DC86D8': 'Apple, Inc.',
        'DC9B9C': 'Apple, Inc.',
        'DCA4CA': 'Apple, Inc.',
        'DCA904': 'Apple, Inc.',
        'E05F45': 'Apple, Inc.',
        'E0ACCB': 'Apple, Inc.',
        'E0B52D': 'Apple, Inc.',
        'E0B9BA': 'Apple, Inc.',
        'E0C767': 'Apple, Inc.',
        'E0C97A': 'Apple, Inc.',
        'E0F5C6': 'Apple, Inc.',
        'E0F847': 'Apple, Inc.',
        'E4254E': 'Apple, Inc.',
        'E425E7': 'Apple, Inc.',
        'E42B34': 'Apple, Inc.',
        'E48B7F': 'Apple, Inc.',
        'E49A79': 'Apple, Inc.',
        'E4C63D': 'Apple, Inc.',
        'E4CE8F': 'Apple, Inc.',
        'E4E0A6': 'Apple, Inc.',
        'E8040B': 'Apple, Inc.',
        'E80688': 'Apple, Inc.',
        'E8061A': 'Apple, Inc.',
        'E8802E': 'Apple, Inc.',
        'E88D28': 'Apple, Inc.',
        'E8B4C8': 'Apple, Inc.',
        'EC3586': 'Apple, Inc.',
        'EC852F': 'Apple, Inc.',
        'F02475': 'Apple, Inc.',
        'F04F7C': 'Apple, Inc.',
        'F05B7B': 'Apple, Inc.',
        'F07960': 'Apple, Inc.',
        'F099BF': 'Apple, Inc.',
        'F0B479': 'Apple, Inc.',
        'F0C1F1': 'Apple, Inc.',
        'F0CB00': 'Apple, Inc.',
        'F0D1A9': 'Apple, Inc.',
        'F0DBE2': 'Apple, Inc.',
        'F0DBF8': 'Apple, Inc.',
        'F0DCE2': 'Apple, Inc.',
        'F40F24': 'Apple, Inc.',
        'F431C3': 'Apple, Inc.',
        'F437B7': 'Apple, Inc.',
        'F45C89': 'Apple, Inc.',
        'F4F15A': 'Apple, Inc.',
        'F4F951': 'Apple, Inc.',
        'F8038B': 'Apple, Inc.',
        'F81EDF': 'Apple, Inc.',
        'F827D5': 'Apple, Inc.',
        'F82793': 'Apple, Inc.',
        'F8A9A0': 'Apple, Inc.',
        'F8D0BD': 'Apple, Inc.',
        'F8E079': 'Apple, Inc.',
        'FC253F': 'Apple, Inc.',
        'FCE998': 'Apple, Inc.',
        'FCFC48': 'Apple, Inc.',

        # Samsung
        '0017C9': 'Samsung Electronics',
        '0018AF': 'Samsung Electronics',
        '001A8A': 'Samsung Electronics',
        '001EE2': 'Samsung Electronics',
        '002119': 'Samsung Electronics',
        '0023D6': 'Samsung Electronics',
        '0023D7': 'Samsung Electronics',
        '0024E9': 'Samsung Electronics',
        '002567': 'Samsung Electronics',
        '0026E2': 'Samsung Electronics',
        '0E38E3': 'Samsung Electronics',
        '10D38A': 'Samsung Electronics',
        '14B484': 'Samsung Electronics',
        '183A2D': 'Samsung Electronics',
        '18E2C2': 'Samsung Electronics',
        '2013E0': 'Samsung Electronics',
        '24924E': 'Samsung Electronics',
        '2C0E3D': 'Samsung Electronics',
        '2CA835': 'Samsung Electronics',
        '302DE8': 'Samsung Electronics',
        '340395': 'Samsung Electronics',
        '38016C': 'Samsung Electronics',
        '380A94': 'Samsung Electronics',
        '3C5A37': 'Samsung Electronics',
        '3C62C6': 'Samsung Electronics',
        '40F3AE': 'Samsung Electronics',
        '4844F7': 'Samsung Electronics',
        '5056BF': 'Samsung Electronics',
        '549B12': 'Samsung Electronics',
        '5CA39D': 'Samsung Electronics',
        '6077E2': 'Samsung Electronics',
        '649ABE': 'Samsung Electronics',
        '6C8336': 'Samsung Electronics',
        '78471D': 'Samsung Electronics',
        '78D6F0': 'Samsung Electronics',
        '84119E': 'Samsung Electronics',
        '8425DB': 'Samsung Electronics',
        '8455A5': 'Samsung Electronics',
        '88329B': 'Samsung Electronics',
        '8C71F8': 'Samsung Electronics',
        '9463D1': 'Samsung Electronics',
        '94D771': 'Samsung Electronics',
        '983B16': 'Samsung Electronics',
        '9C65B0': 'Samsung Electronics',
        'A00798': 'Samsung Electronics',
        'A80600': 'Samsung Electronics',
        'A8F274': 'Samsung Electronics',
        'B8D9CE': 'Samsung Electronics',
        'C0BDD1': 'Samsung Electronics',
        'C45006': 'Samsung Electronics',
        'D0176A': 'Samsung Electronics',
        'D0817A': 'Samsung Electronics',
        'D4878A': 'Samsung Electronics',
        'D87B1A': 'Samsung Electronics',
        'E0CBEE': 'Samsung Electronics',
        'EC107B': 'Samsung Electronics',
        'F008F1': 'Samsung Electronics',
        'F025B7': 'Samsung Electronics',
        'F0728C': 'Samsung Electronics',
        'F49F54': 'Samsung Electronics',
        'F8042E': 'Samsung Electronics',
        'F8D0AC': 'Samsung Electronics',
        'FCA183': 'Samsung Electronics',
        'FCFCB4': 'Samsung Electronics',

        # Google
        '00226B': 'Google, Inc.',
        '00E5F1': 'Google, Inc.',
        '083E5E': 'Google, Inc.',
        '201A06': 'Google, Inc.',
        '3C5AB4': 'Google, Inc.',
        '5C0A5B': 'Google, Inc.',
        '5CE8B7': 'Google, Inc.',
        '6C29A2': 'Google, Inc.',
        '70699D': 'Google, Inc.',
        '7CB07C': 'Google, Inc.',
        '94EB2C': 'Google, Inc.',
        'A47733': 'Google, Inc.',
        'D4F546': 'Google, Inc.',
        'F4F5D8': 'Google, Inc.',
        'F4F5E8': 'Google, Inc.',
        'F8:0F:F9': 'Google, Inc.',

        # LG Electronics
        '001E75': 'LG Electronics',
        '0019A1': 'LG Electronics',
        '10F96F': 'LG Electronics',
        '2C54CF': 'LG Electronics',
        '30766F': 'LG Electronics',
        '3C25D7': 'LG Electronics',
        '48597F': 'LG Electronics',
        '58A2B5': 'LG Electronics',
        '5C70A3': 'LG Electronics',
        '64899A': 'LG Electronics',
        '6C5C14': 'LG Electronics',
        '88C9D0': 'LG Electronics',
        '9C02A8': 'LG Electronics',
        'A8D3F7': 'LG Electronics',
        'B8D61A': 'LG Electronics',
        'BC8CCD': 'LG Electronics',
        'C49A02': 'LG Electronics',
        'CC2D83': 'LG Electronics',
        'E892A4': 'LG Electronics',
        'F0D7AA': 'LG Electronics',

        # Sony
        '001315': 'Sony Corporation',
        '001A80': 'Sony Corporation',
        '001D28': 'Sony Corporation',
        '001E45': 'Sony Corporation',
        '00248D': 'Sony Corporation',
        '002567': 'Sony Corporation',
        '002680': 'Sony Corporation',
        '0024BE': 'Sony Corporation',
        '00EB2D': 'Sony Corporation',
        '04767A': 'Sony Corporation',
        '1093E9': 'Sony Corporation',
        '183D5E': 'Sony Corporation',
        '1C14B3': 'Sony Corporation',
        '244B81': 'Sony Corporation',
        '28B9D1': 'Sony Corporation',
        '308454': 'Sony Corporation',
        '384613': 'Sony Corporation',
        '3C0766': 'Sony Corporation',
        '403DC6': 'Sony Corporation',
        '589B0E': 'Sony Corporation',
        '78843C': 'Sony Corporation',
        '8C64A2': 'Sony Corporation',
        '9CC7D1': 'Sony Corporation',
        'AC9B0A': 'Sony Corporation',
        'B478C7': 'Sony Corporation',
        'C8F24E': 'Sony Corporation',
        'D89553': 'Sony Corporation',
        'E0D4E8': 'Sony Corporation',
        'F4424F': 'Sony Corporation',
        'FC0FE6': 'Sony Corporation',

        # Microsoft
        '001DD8': 'Microsoft Corp',
        '28187D': 'Microsoft Corp',
        '30596C': 'Microsoft Corp',
        '3495DB': 'Microsoft Corp',
        '4494FC': 'Microsoft Corp',
        '50579D': 'Microsoft Corp',
        '58A5F2': 'Microsoft Corp',
        '60455E': 'Microsoft Corp',
        '7C1E52': 'Microsoft Corp',
        '7CED8D': 'Microsoft Corp',
        '9C4FDA': 'Microsoft Corp',
        'B4AE2B': 'Microsoft Corp',
        'C81479': 'Microsoft Corp',
        'DC536C': 'Microsoft Corp',
        'F4B7E2': 'Microsoft Corp',

        # Motorola/Lenovo
        '000A28': 'Motorola',
        '000E5C': 'Motorola',
        '001195': 'Motorola',
        '0013E0': 'Motorola',
        '00179A': 'Motorola',
        '001A1E': 'Motorola',
        '001BCE': 'Motorola',
        '001EC7': 'Motorola',
        '0020E0': 'Motorola',
        '002490': 'Motorola',
        '002567': 'Motorola',
        '0816C3': 'Motorola',
        '108651': 'Motorola',
        '40F407': 'Motorola',
        '6CC217': 'Motorola',
        '881FA1': 'Motorola',
        'A0F450': 'Motorola',
        'C0EE1B': 'Motorola',
        'D41D71': 'Motorola',
        'D4612E': 'Motorola',
        'E40F2D': 'Motorola',
        'EC1F72': 'Motorola',
        'F875A4': 'Motorola',

        # Huawei
        '001882': 'Huawei Technologies',
        '001E10': 'Huawei Technologies',
        '0021E8': 'Huawei Technologies',
        '0022A1': 'Huawei Technologies',
        '0025A5': 'Huawei Technologies',
        '002EC7': 'Huawei Technologies',
        '005A13': 'Huawei Technologies',
        '04B0E7': 'Huawei Technologies',
        '04C06F': 'Huawei Technologies',
        '04F938': 'Huawei Technologies',
        '0819A6': 'Huawei Technologies',
        '0C96BF': 'Huawei Technologies',
        '10B1F8': 'Huawei Technologies',
        '14B968': 'Huawei Technologies',
        '1C1D67': 'Huawei Technologies',
        '20A680': 'Huawei Technologies',
        '24094C': 'Huawei Technologies',
        '24698E': 'Huawei Technologies',
        '28A6DB': 'Huawei Technologies',
        '34CDBE': 'Huawei Technologies',
        '38BC01': 'Huawei Technologies',
        '3C474D': 'Huawei Technologies',
        '4C5499': 'Huawei Technologies',
        '501CBD': 'Huawei Technologies',
        '54B561': 'Huawei Technologies',
        '5C7D5E': 'Huawei Technologies',
        '5C8E96': 'Huawei Technologies',
        '5CE2E5': 'Huawei Technologies',
        '60DE44': 'Huawei Technologies',
        '70723C': 'Huawei Technologies',
        '78D752': 'Huawei Technologies',
        '84742A': 'Huawei Technologies',
        '8817E0': 'Huawei Technologies',
        '9028A6': 'Huawei Technologies',
        '941882': 'Huawei Technologies',
        '986E5D': 'Huawei Technologies',
        '9C28EF': 'Huawei Technologies',
        'A8CA7B': 'Huawei Technologies',
        'B4307D': 'Huawei Technologies',
        'C4072F': 'Huawei Technologies',
        'C8D15E': 'Huawei Technologies',
        'D0D04B': 'Huawei Technologies',
        'D46E5C': 'Huawei Technologies',
        'E0247F': 'Huawei Technologies',
        'E0A3AC': 'Huawei Technologies',
        'E4712D': 'Huawei Technologies',
        'E8088B': 'Huawei Technologies',
        'EC233D': 'Huawei Technologies',
        'F04B6A': 'Huawei Technologies',
        'F429E1': 'Huawei Technologies',
        'FC48EF': 'Huawei Technologies',

        # OnePlus
        '64A2F9': 'OnePlus Technology',
        '94653A': 'OnePlus Technology',
        'C0EEFB': 'OnePlus Technology',

        # Xiaomi
        '0C1DAF': 'Xiaomi',
        '100927': 'Xiaomi',
        '14F65A': 'Xiaomi',
        '18593D': 'Xiaomi',
        '286C07': 'Xiaomi',
        '2CFDA1': 'Xiaomi',
        '34CE00': 'Xiaomi',
        '3C956E': 'Xiaomi',
        '4C63EB': 'Xiaomi',
        '50EC50': 'Xiaomi',
        '5C0272': 'Xiaomi',
        '643115': 'Xiaomi',
        '6C5AB5': 'Xiaomi',
        '7C1DD9': 'Xiaomi',
        '840D8E': 'Xiaomi',
        '8CB8A1': 'Xiaomi',
        '98FAE3': 'Xiaomi',
        'A086C6': 'Xiaomi',
        'B0E235': 'Xiaomi',
        'B8D7AF': 'Xiaomi',
        'C48E8F': 'Xiaomi',
        'D4970B': 'Xiaomi',
        'F0B429': 'Xiaomi',
        'F4F5E8': 'Xiaomi',
        'F8A45F': 'Xiaomi',

        # Intel
        '001E67': 'Intel Corporate',
        '0021D8': 'Intel Corporate',
        '0024D6': 'Intel Corporate',
        '002713': 'Intel Corporate',
        '00266D': 'Intel Corporate',
        '04D4C4': 'Intel Corporate',
        '0813CE': 'Intel Corporate',
        '080027': 'Intel Corporate',
        '345BB0': 'Intel Corporate',
        '3C970E': 'Intel Corporate',
        '50E085': 'Intel Corporate',
        '5CCF7F': 'Intel Corporate',
        '5CE0C5': 'Intel Corporate',
        '606720': 'Intel Corporate',
        '6C294D': 'Intel Corporate',
        '803049': 'Intel Corporate',
        '840B2D': 'Intel Corporate',
        '8851FB': 'Intel Corporate',
        '8C700B': 'Intel Corporate',
        '98541B': 'Intel Corporate',
        '9C4E36': 'Intel Corporate',
        'A06FE6': 'Intel Corporate',
        'A433D1': 'Intel Corporate',
        'A44E31': 'Intel Corporate',
        'B4E1C4': 'Intel Corporate',
        'C8D9D2': 'Intel Corporate',
        'F40343': 'Intel Corporate',
        'F8634B': 'Intel Corporate',

        # Bose
        '0468B0': 'Bose Corporation',
        '14EB33': 'Bose Corporation',
        '2C41A1': 'Bose Corporation',
        '4C87A5': 'Bose Corporation',
        '88C6E3': 'Bose Corporation',

        # JBL/Harman
        '000DE7': 'Harman International',
        '002076': 'Harman International',
        '04FE31': 'Harman International',
        '181F12': 'Harman International',
        '1C48F9': 'Harman International',
        '2C4D79': 'Harman International',
        '400E85': 'Harman International',
        '50C002': 'Harman International',
        '5CD5B2': 'Harman International',
        '74F61C': 'Harman International',
        '805EC0': 'Harman International',
        '8C8EF2': 'Harman International',
        '9890AE': 'Harman International',
        'B4F559': 'Harman International',
        'F84E17': 'Harman International',

        # Beats (Apple subsidiary)
        '18F1D8': 'Beats Electronics',
        '5C15C6': 'Beats Electronics',

        # Fitbit
        '3E48E4': 'Fitbit Inc.',
        'C69F79': 'Fitbit Inc.',

        # Tile
        'C4:7C:8D': 'Tile Inc.',

        # Garmin
        '006792': 'Garmin International',
        'C0D3C0': 'Garmin International',

        # Skullcandy
        '000450': 'Skullcandy',

        # Jabra/GN Audio
        '001E4C': 'GN Audio A/S',
        '507A55': 'GN Audio A/S',
        '501829': 'GN Audio A/S',

        # Sennheiser
        '001BDC': 'Sennheiser',
        '00144F': 'Sennheiser',

        # Plantronics/Poly
        '001A48': 'Plantronics',
        '205D47': 'Plantronics',

        # Logitech
        '001F20': 'Logitech',
        '0059DC': 'Logitech',
        '046D2C': 'Logitech',
        '6C5D3A': 'Logitech',
        'B0247C': 'Logitech',

        # Dell
        '001A4B': 'Dell Inc.',
        '14187E': 'Dell Inc.',
        '18A99B': 'Dell Inc.',
        '246E96': 'Dell Inc.',
        '54BEF7': 'Dell Inc.',
        'B499BA': 'Dell Inc.',
        'D4BED9': 'Dell Inc.',
        'F01FAF': 'Dell Inc.',

        # HP
        '001083': 'Hewlett-Packard',
        '001185': 'Hewlett-Packard',
        '001321': 'Hewlett-Packard',
        '00215A': 'Hewlett-Packard',
        '0025B3': 'Hewlett-Packard',
        '3863BB': 'Hewlett-Packard',
        '48DF37': 'Hewlett-Packard',
        '683085': 'Hewlett-Packard',
        '80C16E': 'Hewlett-Packard',
        '8CB094': 'Hewlett-Packard',
        '98E7F4': 'Hewlett-Packard',
        'B05ADA': 'Hewlett-Packard',
        'E8E0B7': 'Hewlett-Packard',

        # Sena Technologies (Bluetooth)
        '00158D': 'Sena Technologies',

        # Cambridge Silicon Radio (CSR) - common BT chipsets
        '00025B': 'Cambridge Silicon Radio',
        '001019': 'Cambridge Silicon Radio',
        '001CDF': 'Cambridge Silicon Radio',
        '002238': 'Cambridge Silicon Radio',
        '000272': 'CC&C Technologies',

        # Broadcom (BT chipsets)
        '0003BA': 'Broadcom',
        '001018': 'Broadcom',
        '001217': 'Broadcom',
        '00127C': 'Broadcom',
        '0016B4': 'Broadcom',
        '0019B9': 'Broadcom',
        '001A3F': 'Broadcom',
        '001B63': 'Broadcom',
        '001D0F': 'Broadcom',
        '001EC7': 'Broadcom',
        '00256B': 'Broadcom',
        '002722': 'Broadcom',

        # Qualcomm
        '000998': 'Qualcomm Inc.',
        '040CCE': 'Qualcomm Inc.',
        '0C14F2': 'Qualcomm Inc.',
        '247F20': 'Qualcomm Inc.',
        '38AAFF': 'Qualcomm Inc.',
        '403766': 'Qualcomm Inc.',
        '60F189': 'Qualcomm Inc.',
        '787282': 'Qualcomm Inc.',
        '84A466': 'Qualcomm Inc.',
        '9CE33F': 'Qualcomm Inc.',
        'BC851F': 'Qualcomm Inc.',
        'D4F528': 'Qualcomm Inc.',
        'EC7F07': 'Qualcomm Inc.',
        'F46AB5': 'Qualcomm Inc.',

        # Texas Instruments
        '000D83': 'Texas Instruments',
        '001276': 'Texas Instruments',
        '001583': 'Texas Instruments',
        '001B8F': 'Texas Instruments',
        '00200F': 'Texas Instruments',
        '1C4B7C': 'Texas Instruments',
        'D03972': 'Texas Instruments',
        'D4F5EF': 'Texas Instruments',
        'EC2E3D': 'Texas Instruments',

        # Amazon
        '0014F2': 'Amazon Technologies',
        '383F48': 'Amazon Technologies',
        '44650D': 'Amazon Technologies',
        '5000E3': 'Amazon Technologies',
        '68378E': 'Amazon Technologies',
        '6C5697': 'Amazon Technologies',
        '747618': 'Amazon Technologies',
        '7C6193': 'Amazon Technologies',
        '84D611': 'Amazon Technologies',
        'A002DC': 'Amazon Technologies',
        'B47C9C': 'Amazon Technologies',
        'C8F5C5': 'Amazon Technologies',
        'F0272D': 'Amazon Technologies',
        'FC65DE': 'Amazon Technologies',

        # Fossil Group (smartwatches)
        'D8C770': 'Fossil Group',

        # Nintendo
        '002331': 'Nintendo Co., Ltd.',
        '002659': 'Nintendo Co., Ltd.',
        '002709': 'Nintendo Co., Ltd.',
        '34AF2C': 'Nintendo Co., Ltd.',
        '40D28A': 'Nintendo Co., Ltd.',
        '582F40': 'Nintendo Co., Ltd.',
        '78A2A0': 'Nintendo Co., Ltd.',
        '7CBB8A': 'Nintendo Co., Ltd.',
        '98B6E9': 'Nintendo Co., Ltd.',
        'A4C0E1': 'Nintendo Co., Ltd.',
        'B88AEC': 'Nintendo Co., Ltd.',
        'CC9E00': 'Nintendo Co., Ltd.',
        'D86BF7': 'Nintendo Co., Ltd.',
        'E00C7F': 'Nintendo Co., Ltd.',
        'E84ECE': 'Nintendo Co., Ltd.',

        # GoPro
        'D4D918': 'GoPro',
        'F45214': 'GoPro',

        # DJI
        '60601F': 'DJI',
        '4C0B57': 'DJI',

        # Tesla
        '4C:FC:AA': 'Tesla Motors',
        'E4:8B:7F': 'Tesla Motors',

        # Unknown placeholder
        '000000': 'Unknown',
    }
    return oui_db.get(oui, f'OUI:{oui}')


def get_device_type(bd_address):
    """
    Determine if device is Classic or BLE using DEFINITIVE indicators only.
    Returns 'unknown' if we cannot be certain - never guess.
    False positives are unacceptable.
    """
    bd_address = bd_address.upper()

    # Method 1: Check btmon cache first - most reliable source from actual HCI events
    btmon_info = btmon_device_cache.get(bd_address)
    if btmon_info:
        # If btmon saw it as BLE or Classic, trust that
        if btmon_info.get('device_type') in ['ble', 'classic']:
            return btmon_info['device_type']

        # These address types from btmon are DEFINITIVE BLE indicators
        addr_type = btmon_info.get('addr_type', '').lower()
        if addr_type in ['resolvable', 'static', 'non-resolvable', 'random']:
            return 'ble'

        # If btmon saw advertising data, it's BLE
        if btmon_info.get('adv_type'):
            return 'ble'

    # Method 2: Try bluetoothctl info for DEFINITIVE indicators only
    try:
        result = subprocess.run(
            ['bluetoothctl', 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=3
        )
        output = result.stdout.lower()

        # DEFINITIVE BLE indicators
        if 'addresstype: random' in output:
            return 'ble'
        if 'le only' in output:
            return 'ble'

        # DEFINITIVE Classic indicator - Device Class is ONLY present for Classic BR/EDR
        if 'class: 0x' in output:
            return 'classic'

        # Classic-only service UUIDs (BR/EDR profiles)
        classic_uuids = ['handsfree', 'a2dp', 'avrcp', 'hfp', 'headset',
                        'serial port', 'obex', 'pbap', 'map ', 'hid']
        for uuid in classic_uuids:
            if f'uuid: {uuid}' in output:
                return 'classic'

        # BLE-only GATT service UUIDs
        ble_uuids = ['generic access', 'generic attribute', 'battery service',
                     'heart rate', 'health thermometer', 'device information']
        for uuid in ble_uuids:
            if f'uuid: {uuid}' in output:
                return 'ble'

    except Exception:
        pass

    # Method 3: Check hcitool info for Device Class (Classic-only)
    try:
        result = subprocess.run(
            ['hcitool', 'info', bd_address],
            capture_output=True,
            text=True,
            timeout=2
        )
        # Device Class is DEFINITIVE Classic indicator - BLE devices don't have this
        if 'class:' in result.stdout.lower() and '0x' in result.stdout.lower():
            return 'classic'
    except Exception:
        pass

    # If we can't be certain, return unknown
    # NEVER GUESS - false positives are unacceptable
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


# ==================== BTMON DEVICE MONITORING ====================

# Parser state for btmon context tracking
class BtmonParserState:
    """Track btmon parsing context across multiple lines."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.current_addr = None
        self.current_event = None
        self.current_device = {}
        self.is_le_event = False
        self.is_classic_event = False
        self.addr_type = None

btmon_parser_state = BtmonParserState()


def get_company_name(company_id):
    """Get company name from Bluetooth Company ID."""
    if isinstance(company_id, str):
        try:
            company_id = int(company_id, 16)
        except:
            return None
    return BT_COMPANY_IDS.get(company_id)


def parse_btmon_line(line):
    """
    Parse btmon output for rich device information.

    Extracts:
    - BD Address with type detection (Resolvable/Static/Non-Resolvable = BLE, OUI = Classic)
    - Device type (Classic/BLE) from event type and address format
    - RSSI (handles "invalid" RSSI)
    - Device name (from advertising data or name request)
    - Manufacturer/Company ID (decimal format like "Apple, Inc. (76)")
    - Address type (Public/Random/Resolvable/Static)
    - TX Power (filters out 127 dBm which means unknown)
    """
    global btmon_rssi_cache, btmon_device_cache, btmon_parser_state

    state = btmon_parser_state
    line_stripped = line.strip()

    # Detect HCI Event type - this tells us Classic vs BLE
    if '> HCI Event:' in line or '< HCI Command:' in line or '@ MGMT Event:' in line:
        # New event - save any pending device data
        if state.current_addr and state.current_device:
            save_btmon_device_data(state.current_addr, state.current_device)

        state.reset()

        # Identify event type from HCI Events
        if 'LE Meta Event' in line or 'LE Extended Advertising Report' in line:
            state.is_le_event = True
            state.current_event = 'le_meta'
        elif 'Inquiry Result' in line:
            state.is_classic_event = True
            state.current_event = 'inquiry_result'
        elif 'Extended Inquiry Result' in line:
            state.is_classic_event = True
            state.current_event = 'extended_inquiry'
        elif 'Remote Name Req Complete' in line:
            state.is_classic_event = True
            state.current_event = 'name_complete'
        elif 'Remote Name Request' in line:
            state.is_classic_event = True
            state.current_event = 'name_request'
        elif 'Connect Complete' in line or 'Connect Failed' in line:
            state.current_event = 'connect'
        elif 'Device Found' in line:
            # MGMT Event: Device Found - could be either type
            state.current_event = 'device_found'
        return

    # Parse explicit LE Address (from MGMT events)
    # Format: "LE Address: 47:63:95:64:91:7A (Resolvable)"
    le_addr_match = re.search(r'LE Address:\s*([0-9A-Fa-f:]{17})\s*\((\w+)\)', line_stripped)
    if le_addr_match:
        state.current_addr = le_addr_match.group(1).upper()
        addr_type = le_addr_match.group(2).lower()
        state.current_device['addr_type'] = addr_type
        state.current_device['device_type'] = 'ble'
        state.is_le_event = True
        return

    # Parse explicit BR/EDR Address (from MGMT events)
    # Format: "BR/EDR Address: 72:3D:D6:79:8B:36 (OUI 72-3D-D6)"
    bredr_addr_match = re.search(r'BR/EDR Address:\s*([0-9A-Fa-f:]{17})', line_stripped)
    if bredr_addr_match:
        state.current_addr = bredr_addr_match.group(1).upper()
        state.current_device['device_type'] = 'classic'
        state.is_classic_event = True
        return

    # Parse Address with BLE-specific type indicators
    # Format: "Address: 47:63:95:64:91:7A (Resolvable)" or "(Static)" or "(Non-Resolvable)"
    addr_ble_match = re.search(r'Address:\s*([0-9A-Fa-f:]{17})\s*\((Resolvable|Static|Non-Resolvable)\)', line_stripped)
    if addr_ble_match:
        state.current_addr = addr_ble_match.group(1).upper()
        addr_type = addr_ble_match.group(2).lower()
        state.current_device['addr_type'] = addr_type
        state.current_device['device_type'] = 'ble'
        state.is_le_event = True
        return

    # Parse Address with OUI - indicates Classic/BR-EDR
    # Format: "Address: 4E:4A:9D:42:1C:F4 (OUI 4E-4A-9D)"
    addr_oui_match = re.search(r'Address:\s*([0-9A-Fa-f:]{17})\s*\(OUI [0-9A-Fa-f-]+\)', line_stripped)
    if addr_oui_match:
        state.current_addr = addr_oui_match.group(1).upper()
        # OUI format often indicates Classic, but could be either - use event context
        if not state.is_le_event:
            state.current_device['device_type'] = 'classic'
            state.is_classic_event = True
        return

    # Parse Address with Public/Random type
    # Format: "Address type: Random (0x01)" or "Address type: Public (0x00)"
    addr_type_match = re.search(r'Address type:\s*(Random|Public)\s*\(0x0[01]\)', line_stripped)
    if addr_type_match:
        addr_type = addr_type_match.group(1).lower()
        state.current_device['addr_type'] = addr_type
        if addr_type == 'random':
            state.current_device['device_type'] = 'ble'
            state.is_le_event = True
        return

    # Parse generic Address if we don't have one yet
    if not state.current_addr:
        addr_match = re.search(r'Address:\s*([0-9A-Fa-f:]{17})', line_stripped)
        if addr_match:
            state.current_addr = addr_match.group(1).upper()

    # Parse RSSI - handle "invalid" RSSI
    # Format: "RSSI: -96 dBm (0xa0)" or "RSSI: invalid (0x99)"
    if 'RSSI:' in line_stripped:
        if 'invalid' in line_stripped.lower():
            # Invalid RSSI, skip it
            return
        rssi_match = re.search(r'RSSI:\s*(-?\d+)\s*dBm', line_stripped)
        if rssi_match:
            rssi = int(rssi_match.group(1))
            state.current_device['rssi'] = rssi

            # Also update legacy cache for compatibility
            if state.current_addr:
                btmon_rssi_cache[state.current_addr] = {
                    'rssi': rssi,
                    'timestamp': time.time()
                }
        return

    # Parse Device Name from Remote Name Req Complete
    # Format: "Name: DeviceName" (after Status line)
    if state.current_event == 'name_complete':
        name_match = re.search(r'^\s*Name:\s*(.+)', line_stripped)
        if name_match:
            name = name_match.group(1).strip()
            if name and name != '(null)' and len(name) > 0:
                state.current_device['device_name'] = name
            return

    # Parse Device Name (complete or short) from advertising
    name_match = re.search(r'Name \((?:complete|short)\):\s*(.+)', line_stripped)
    if name_match:
        name = name_match.group(1).strip()
        if name and name != '(null)':
            state.current_device['device_name'] = name
        return

    # Parse Local Name from advertising
    local_name_match = re.search(r'(?:Complete|Shortened) Local Name:\s*(.+)', line_stripped)
    if local_name_match:
        name = local_name_match.group(1).strip()
        if name:
            state.current_device['device_name'] = name
        return

    # Parse Company with decimal ID - "Company: Apple, Inc. (76)"
    company_dec_match = re.search(r'Company:\s*([^(]+)\s*\((\d+)\)', line_stripped)
    if company_dec_match:
        company_name = company_dec_match.group(1).strip()
        company_id = int(company_dec_match.group(2))
        state.current_device['company_id'] = company_id
        state.current_device['company_name'] = company_name
        return

    # Parse Company ID (hex format: "Company: Apple, Inc. (0x004c)")
    company_hex_match = re.search(r'Company:\s*([^(]+)\s*\(0x([0-9A-Fa-f]+)\)', line_stripped)
    if company_hex_match:
        company_name = company_hex_match.group(1).strip()
        company_id = int(company_hex_match.group(2), 16)
        state.current_device['company_id'] = company_id
        state.current_device['company_name'] = company_name
        return

    # Parse Company ID alone (format: "Company ID: 0x004c")
    company_id_match = re.search(r'Company ID:\s*0x([0-9A-Fa-f]+)', line_stripped)
    if company_id_match:
        company_id = int(company_id_match.group(1), 16)
        state.current_device['company_id'] = company_id
        company_name = get_company_name(company_id)
        if company_name:
            state.current_device['company_name'] = company_name
        return

    # Parse TX Power - filter out 127 dBm which means "unknown"
    # Format: "TX power: 12 dBm" or "TX power: 0 dBm"
    tx_power_match = re.search(r'TX [Pp]ower:\s*(-?\d+)\s*dBm', line_stripped)
    if tx_power_match:
        tx_power = int(tx_power_match.group(1))
        # 127 dBm means unknown/not available
        if tx_power != 127:
            state.current_device['tx_power'] = tx_power
        return

    # Parse Legacy PDU Type - strong BLE indicator
    # Format: "Legacy PDU Type: ADV_IND (0x0013)" or "ADV_NONCONN_IND" or "SCAN_RSP"
    pdu_match = re.search(r'Legacy PDU Type:\s*(ADV_\w+|SCAN_RSP)', line_stripped)
    if pdu_match:
        pdu_type = pdu_match.group(1)
        state.current_device['adv_type'] = pdu_type
        state.current_device['device_type'] = 'ble'
        state.is_le_event = True
        return

    # Parse Device Class (Classic indicator)
    class_match = re.search(r'Class:\s*(0x[0-9A-Fa-f]+)', line_stripped)
    if class_match:
        state.current_device['device_class'] = class_match.group(1)
        state.current_device['device_type'] = 'classic'
        state.is_classic_event = True
        return

    # Parse Flags for BR/EDR support
    if 'BR/EDR Not Supported' in line_stripped:
        state.current_device['device_type'] = 'ble'
        state.current_device['ble_only'] = True
        state.is_le_event = True
        return

    # Parse "Not Connectable" flag (from MGMT Device Found)
    if 'Not Connectable' in line_stripped:
        state.current_device['connectable'] = False
        return

    # Parse LE General Discoverable Mode flag - BLE indicator
    if 'LE General Discoverable Mode' in line_stripped:
        state.current_device['device_type'] = 'ble'
        state.is_le_event = True
        return

    # Parse Event Type (ADV_IND, ADV_SCAN_IND, etc.) - BLE indicator
    event_type_match = re.search(r'Event type:\s*0x[0-9A-Fa-f]+', line_stripped)
    if event_type_match:
        # Hex event type from LE Extended Advertising Report
        state.current_device['device_type'] = 'ble'
        state.is_le_event = True
        return


def save_btmon_device_data(bd_addr, device_data):
    """Save parsed btmon device data and update device record."""
    global btmon_device_cache, btmon_parser_state

    if not bd_addr:
        return

    bd_addr = bd_addr.upper()
    now = time.time()

    # Determine device type from context if not already set
    if 'device_type' not in device_data:
        if btmon_parser_state.is_le_event:
            device_data['device_type'] = 'ble'
        elif btmon_parser_state.is_classic_event:
            device_data['device_type'] = 'classic'

    device_data['timestamp'] = now

    # Update or create cache entry
    if bd_addr in btmon_device_cache:
        # Merge with existing data (don't overwrite with None)
        for key, value in device_data.items():
            if value is not None:
                btmon_device_cache[bd_addr][key] = value
    else:
        btmon_device_cache[bd_addr] = device_data

    # Update main devices dict if device exists
    if bd_addr in devices:
        updated = False

        # Update device type if we now have a definitive answer
        if 'device_type' in device_data and device_data['device_type'] in ['ble', 'classic']:
            if devices[bd_addr].get('device_type') in ['unknown', None]:
                devices[bd_addr]['device_type'] = device_data['device_type']
                updated = True

        # Update name if we got a better one
        if 'device_name' in device_data and device_data['device_name']:
            current_name = devices[bd_addr].get('device_name', '')
            if not current_name or current_name in ['Unknown', 'BLE Device', '']:
                devices[bd_addr]['device_name'] = device_data['device_name']
                updated = True

        # Update company/manufacturer
        if 'company_name' in device_data and device_data['company_name']:
            devices[bd_addr]['bt_company'] = device_data['company_name']
            updated = True

        # Update RSSI
        if 'rssi' in device_data:
            devices[bd_addr]['rssi'] = device_data['rssi']
            updated = True

        # Update TX power
        if 'tx_power' in device_data:
            devices[bd_addr]['tx_power'] = device_data['tx_power']
            updated = True

        # Emit update if changed
        if updated:
            socketio.emit('device_update', devices[bd_addr])


def get_btmon_device_info(bd_address):
    """Get cached device info from btmon for a device."""
    bd_address = bd_address.upper()
    cached = btmon_device_cache.get(bd_address)
    if cached and (time.time() - cached.get('timestamp', 0)) < 60:
        return cached
    return None


def btmon_monitor_loop():
    """Background thread to monitor btmon output for device info."""
    global btmon_process, scanning_active, btmon_parser_state

    add_log("Starting btmon device monitor...", "INFO")

    try:
        # Start btmon process with timestamp option
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

        # Save any pending device data before exit
        if btmon_parser_state.current_addr and btmon_parser_state.current_device:
            save_btmon_device_data(btmon_parser_state.current_addr, btmon_parser_state.current_device)
        btmon_parser_state.reset()

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


# ==================== ADVANCED BLUETOOTH RADIO OPTIMIZATION ====================
# Techniques for capturing non-discoverable and paired Classic Bluetooth devices
# Designed for Sena UD100 (Class 1, 100m range) and Intel AX210 adapters

# Global state for advanced scanning
advanced_scan_active = False
advanced_scan_thread = None
hidden_scan_active = False
hidden_scan_thread = None
target_survey_active = False
target_survey_thread = None
target_survey_results = {}  # bd_address -> survey result
target_survey_continuous = False  # Run continuously to monitor target presence
target_survey_interval = 30  # Seconds between survey sweeps in continuous mode
target_survey_sweep_count = 0  # Number of completed sweeps
sdp_probe_cache = {}  # bd_address -> {services: [], timestamp: float}
address_sweep_results = {}  # oui -> [found_addresses]

# Known phone/watch manufacturer OUI prefixes for targeted probing
# These devices are often non-discoverable but may respond to direct probes
PHONE_WATCH_OUIS = [
    # Apple (iPhone, Apple Watch)
    '000393', '000A27', '000A95', '000D93', '0010FA', '001451', '0016CB', '0017F2',
    '0019E3', '001B63', '001CB3', '001D4F', '001E52', '001EC2', '001F5B', '001FF3',
    '002241', '002312', '002332', '002436', '0025BC', '0025DB', '002608', '0026B0',
    '0026BB', '003065', '00309D', '003EE1', '0050E4', '006171', '006D52', '008865',
    '00B362', '00C610', '00CDFE', '00F4B9', '00F76F', '041552', '045453', '04D3CF',
    '04DB56', '04E536', '04F13E', '04F7E4', '086698', '0898E0', '08F4AB', '0C3021',
    '0C4DE9', '0C74C2', '0C771A', '0CBC9F', '10417F', '109ADD', '10DDB1', '14109F',
    '1499E2', '14BD61', '18AF61', '18AF8F', '18E7F4', '18EE69', '18F643', '1C1AC0',
    '1C36BB', '1C5CF2', '1C9148', '1C9E46', '1CABA7', '20768F', '207D74', '209BCD',
    '20A286', '20C9D0', '244B03', '24A074', '24A2E1', '24AB81', '24E314', '24F094',
    '280B5C', '283737', '285AEB', '286ABA', '28A02B', '28CFDA', '28CFE9', '28E02C',
    '28E14C', '28E7CF', '28F076', '2C1F23', '2C200B', '2C3361', '2C3F38', '2CB43A',
    '2CBE08', '2CF0A2', '2CF0EE', '30636B', '3090AB', '30F7C5', '34363B', '34C059',
    '34E2FD', '380F4A', '3871DE', '38484C', '38B54D', '38C986', '38CADA', '3C0754',
    '3C15C2', '3C2EFF', '3CE072', '3CFEC5', '400971', '403004', '40331A', '403CFC',
    '406C8F', '40A6D9', '40B395', '40D32D', '442A60', '4480EB', '44D884', '44FB42',
    '483B38', '48437C', '4860BC', '48746E', '48A195', '48BF6B', '48D705', '48E9F1',
    # Samsung (Galaxy phones, watches)
    '0017C9', '0018AF', '001A8A', '001EE2', '002119', '0023D6', '0023D7', '0024E9',
    '002567', '0026E2', '0E38E3', '10D38A', '14B484', '183A2D', '18E2C2', '2013E0',
    '24924E', '2C0E3D', '2CA835', '302DE8', '340395', '38016C', '380A94', '3C5A37',
    '3C62C6', '40F3AE', '4844F7', '5056BF', '549B12', '5CA39D', '6077E2', '649ABE',
    '6C8336', '78471D', '78D6F0', '84119E', '8425DB', '8455A5', '88329B', '8C71F8',
    '9463D1', '94D771', '983B16', '9C65B0', 'A00798', 'A80600', 'A8F274', 'B8D9CE',
    'C0BDD1', 'C45006', 'D0176A', 'D4878A', 'D87B1A', 'E0CBEE', 'EC107B', 'F008F1',
    'F025B7', 'F0728C', 'F49F54', 'F8042E', 'F8D0AC', 'FCA183', 'FCFCB4',
    # Google (Pixel phones, watches)
    '3C5AB4', '54609A', '58CB52', '94EB2C', 'A47733', 'F4F5D8', 'F4F5E8', 'F8A9D0',
    # Garmin (fitness watches)
    '0080E1', '0019FD', '009069', '00A0BF', '3CF862', '582F40', '78E103', 'A0B3CC',
    # Fitbit
    '3C9CC9', '7CB35B', 'C0EEAE', 'DCEF28', 'E85A8B', 'F8ED8B',
    # Huawei/Honor (phones, watches)
    '001A8C', '001E10', '002233', '002254', '002568', '04021F', '044F4C', '04BA36',
    '04C06F', '04F9D9', '082E5F', '083EBD', '0C45C3', '0C96BF', '10051C', '102AB3',
    # Xiaomi (phones, Mi Band)
    '04CF8C', '0C1DAF', '18F0E4', '286C07', '28E31F', '2CE029', '34803B', '34CE00',
    '4C49E3', '50EC50', '5C0A5B', '640980', '6436F3', '64B473', '78024C', '7802F8',
    '7C1DD9', '7C49EB', '8472A4', '84A93E', '8C451A', '94E979', '98FAE3', '9C99A0',
    # OnePlus
    '000272', '30F6EF', '64A2F9', 'C0EEFB', 'D4B27A', 'E0EB10',
    # Oppo/Realme
    '0C5F8E', '18F46A', '1C77F6', '248742', '2C8DB1', '3CE576', '505FC7', '5C4A1F',
]


def is_phone_watch_oui(bd_address):
    """Check if a BD address belongs to a known phone/watch manufacturer."""
    oui = bd_address.upper().replace(':', '')[:6]
    return oui in PHONE_WATCH_OUIS


def set_hci_scan_parameters(interface='hci0', inquiry_mode='extended', page_scan_type='interlaced'):
    """
    Configure HCI controller for optimal device discovery.

    Inquiry Modes:
    - 'standard' (0x00): Standard inquiry result format
    - 'rssi' (0x01): Inquiry result with RSSI
    - 'extended' (0x02): Extended inquiry result with EIR data

    Page Scan Types:
    - 'standard' (0x00): Standard page scan (R0)
    - 'interlaced' (0x01): Interlaced page scan - more aggressive, catches devices faster

    Page Scan Modes:
    - R0: 1.28s window - standard
    - R1: Extended first scan (mandatory)
    - R2: Extended all scans
    """
    try:
        # Set inquiry mode via HCI command
        # OGF 0x03 (Host Controller & Baseband), OCF 0x0045 (Write Inquiry Mode)
        inquiry_modes = {'standard': '00', 'rssi': '01', 'extended': '02'}
        mode_byte = inquiry_modes.get(inquiry_mode, '02')

        result = subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x0045', mode_byte],
            capture_output=True, text=True, timeout=5
        )
        add_log(f"Set inquiry mode to {inquiry_mode} on {interface}", "DEBUG")

        # Set page scan type
        # OGF 0x03, OCF 0x0047 (Write Page Scan Type)
        page_types = {'standard': '00', 'interlaced': '01'}
        page_byte = page_types.get(page_scan_type, '01')

        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x0047', page_byte],
            capture_output=True, timeout=5
        )
        add_log(f"Set page scan type to {page_scan_type} on {interface}", "DEBUG")

        # Set inquiry scan type to interlaced for better discovery
        # OGF 0x03, OCF 0x0043 (Write Inquiry Scan Type)
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x0043', '01'],
            capture_output=True, timeout=5
        )

        # Optimize page scan timing for catching paired devices
        # OGF 0x03, OCF 0x001C (Write Page Scan Activity)
        # Window: 0x0012 (11.25ms) - wider window catches more devices
        # Interval: 0x0800 (1.28s) - standard interval
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x001C',
             '00', '08',  # Interval: 0x0800 (little-endian)
             '12', '00'], # Window: 0x0012 (little-endian)
            capture_output=True, timeout=5
        )
        add_log(f"Optimized page scan timing on {interface}", "DEBUG")

        # Optimize inquiry scan timing
        # OGF 0x03, OCF 0x001E (Write Inquiry Scan Activity)
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x001E',
             '00', '08',  # Interval: 0x0800
             '12', '00'], # Window: 0x0012
            capture_output=True, timeout=5
        )

        return True
    except Exception as e:
        add_log(f"Failed to set HCI parameters on {interface}: {e}", "WARNING")
        return False


def aggressive_inquiry(interface='hci0', duration=15):
    """
    Perform aggressive inquiry using multiple techniques to maximize Classic device detection.

    Techniques:
    1. Extended inquiry with RSSI and EIR data
    2. Multiple LAP codes (GIAC, LIAC, and reserved)
    3. Periodic inquiry for catching intermittent devices
    4. Interlaced scanning mode
    """
    devices_found = []
    seen_addresses = set()

    try:
        add_log(f"Starting aggressive inquiry on {interface} ({duration}s)", "INFO")

        # Configure optimal HCI parameters first
        set_hci_scan_parameters(interface, inquiry_mode='extended', page_scan_type='interlaced')

        # Ensure adapter is up and discoverable mode is off (we want to scan, not be found)
        subprocess.run(['hciconfig', interface, 'up'], capture_output=True, timeout=5)
        subprocess.run(['hciconfig', interface, 'noscan'], capture_output=True, timeout=5)

        # LAP codes to try:
        # 0x9E8B33 - GIAC (General Inquiry Access Code) - most common
        # 0x9E8B00 - LIAC (Limited Inquiry Access Code) - devices in limited discoverable
        # 0x9E8B01-0x9E8B3F - Reserved LAPs that some devices respond to
        lap_codes = [
            '9e8b33',  # GIAC - General
            '9e8b00',  # LIAC - Limited
            '9e8b01',  # Reserved - some headsets
            '9e8b02',  # Reserved
            '9e8b10',  # Reserved - some cars
            '9e8b1f',  # Reserved
            '9e8b3e',  # Reserved
        ]

        # Phase 1: Extended inquiry with each LAP code
        for lap in lap_codes:
            try:
                # Send HCI Inquiry command
                # OGF 0x01 (Link Control), OCF 0x0001 (Inquiry)
                # LAP (3 bytes, little-endian), inquiry_length (1 byte), num_responses (1 byte)
                # inquiry_length 0x08 = 10.24 seconds max, 0x30 = max 61.44 seconds
                result = subprocess.run(
                    ['hcitool', '-i', interface, 'cmd', '0x01', '0x0001',
                     lap[4:6], lap[2:4], lap[0:2],  # LAP in little-endian
                     '04',  # Inquiry length: 4 * 1.28s = 5.12s
                     '00'], # Num responses: unlimited
                    capture_output=True, text=True, timeout=8
                )
            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                add_log(f"LAP {lap} inquiry failed: {e}", "DEBUG")

        # Phase 2: Standard hcitool inquiry with flush
        try:
            result = subprocess.run(
                ['hcitool', '-i', interface, 'inq', '--flush', f'--length={min(duration, 20)}'],
                capture_output=True, text=True, timeout=duration + 5
            )

            # Parse inquiry results
            for line in result.stdout.splitlines():
                match = re.search(r'([0-9A-Fa-f:]{17})', line)
                if match:
                    bd_addr = match.group(1).upper()
                    if bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        # Extract class if present
                        class_match = re.search(r'class:\s*(0x[0-9A-Fa-f]+)', line, re.I)
                        device_class = class_match.group(1) if class_match else None

                        devices_found.append({
                            'bd_address': bd_addr,
                            'device_name': None,
                            'device_type': 'classic',
                            'device_class': device_class,
                            'manufacturer': get_manufacturer(bd_addr),
                            'discovery_method': 'aggressive_inquiry'
                        })
        except subprocess.TimeoutExpired:
            add_log("Inquiry timeout (expected)", "DEBUG")
        except Exception as e:
            add_log(f"Inquiry error: {e}", "WARNING")

        # Phase 3: Use hcitool scan with extended options
        try:
            result = subprocess.run(
                ['hcitool', '-i', interface, 'scan', '--flush', '--length=8', '--class'],
                capture_output=True, text=True, timeout=15
            )

            for line in result.stdout.splitlines():
                if 'Scanning' in line:
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 1:
                    match = re.match(r'([0-9A-Fa-f:]{17})', parts[0])
                    if match:
                        bd_addr = match.group(1).upper()
                        if bd_addr not in seen_addresses:
                            seen_addresses.add(bd_addr)
                            name = parts[1] if len(parts) > 1 else None
                            devices_found.append({
                                'bd_address': bd_addr,
                                'device_name': name,
                                'device_type': 'classic',
                                'manufacturer': get_manufacturer(bd_addr),
                                'discovery_method': 'aggressive_scan'
                            })
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            add_log(f"Scan error: {e}", "WARNING")

        add_log(f"Aggressive inquiry found {len(devices_found)} devices", "INFO")
        return devices_found

    except Exception as e:
        add_log(f"Aggressive inquiry error: {e}", "ERROR")
        return devices_found


def sdp_probe(bd_address, interface='hci0'):
    """
    Probe a device using SDP (Service Discovery Protocol) to force a response.

    This technique works on paired/connected devices that aren't discoverable:
    1. Attempt to connect and query SDP records
    2. Device must respond to SDP queries even if not discoverable
    3. Extracts service list and device info

    Requires the device to be within range and not actively rejecting connections.
    """
    global sdp_probe_cache

    try:
        add_log(f"SDP probing {bd_address} on {interface}", "DEBUG")

        services = []
        device_info = {'bd_address': bd_address, 'services': [], 'responded': False}

        # Use sdptool to browse all services
        try:
            result = subprocess.run(
                ['sdptool', '-i', interface, 'browse', bd_address],
                capture_output=True, text=True, timeout=15
            )

            if result.stdout and 'Service Name' in result.stdout:
                device_info['responded'] = True

                # Parse service records
                current_service = {}
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith('Service Name:'):
                        if current_service:
                            services.append(current_service)
                        current_service = {'name': line.split(':', 1)[1].strip()}
                    elif line.startswith('Service RecHandle:'):
                        current_service['handle'] = line.split(':', 1)[1].strip()
                    elif line.startswith('Service Class ID List:'):
                        current_service['class_ids'] = []
                    elif '"0x' in line and current_service:
                        uuid_match = re.search(r'"(0x[0-9A-Fa-f]+)"', line)
                        if uuid_match and 'class_ids' in current_service:
                            current_service['class_ids'].append(uuid_match.group(1))
                    elif line.startswith('Protocol Descriptor List:'):
                        current_service['protocols'] = []
                    elif 'RFCOMM' in line:
                        channel_match = re.search(r'Channel:\s*(\d+)', line)
                        if channel_match:
                            current_service['rfcomm_channel'] = int(channel_match.group(1))
                    elif 'L2CAP' in line:
                        psm_match = re.search(r'PSM:\s*(0x[0-9A-Fa-f]+|\d+)', line)
                        if psm_match:
                            current_service['l2cap_psm'] = psm_match.group(1)

                if current_service:
                    services.append(current_service)

                device_info['services'] = services
                add_log(f"SDP probe {bd_address}: found {len(services)} services", "INFO")

        except subprocess.TimeoutExpired:
            add_log(f"SDP browse timeout for {bd_address}", "DEBUG")
        except Exception as e:
            add_log(f"SDP browse error: {e}", "DEBUG")

        # Try specific service searches if browse failed
        if not services:
            # Common service UUIDs to probe
            service_uuids = [
                ('0x1101', 'Serial Port'),
                ('0x1103', 'Dialup Networking'),
                ('0x1105', 'OBEX Object Push'),
                ('0x1106', 'OBEX File Transfer'),
                ('0x110A', 'Audio Source'),
                ('0x110B', 'Audio Sink'),
                ('0x110C', 'A/V Remote Control Target'),
                ('0x110E', 'A/V Remote Control'),
                ('0x1112', 'Headset AG'),
                ('0x111E', 'Handsfree'),
                ('0x111F', 'Handsfree AG'),
                ('0x1124', 'HID'),
                ('0x112F', 'Phonebook Access'),
                ('0x1132', 'Message Access'),
                ('0x1200', 'PnP Information'),
            ]

            for uuid, name in service_uuids:
                try:
                    result = subprocess.run(
                        ['sdptool', '-i', interface, 'search', '--bdaddr', bd_address, uuid],
                        capture_output=True, text=True, timeout=5
                    )
                    if 'Service Name' in result.stdout or 'Searching' not in result.stderr:
                        if result.returncode == 0 and result.stdout.strip():
                            device_info['responded'] = True
                            services.append({'name': name, 'uuid': uuid})
                except subprocess.TimeoutExpired:
                    pass
                except:
                    pass

            device_info['services'] = services

        # Cache results
        sdp_probe_cache[bd_address] = {
            'services': services,
            'timestamp': time.time(),
            'responded': device_info['responded']
        }

        return device_info

    except Exception as e:
        add_log(f"SDP probe error for {bd_address}: {e}", "ERROR")
        return {'bd_address': bd_address, 'services': [], 'responded': False}


def l2cap_ping_sweep(bd_address, interface='hci0', psm_range=None):
    """
    Attempt L2CAP connections to detect device presence.

    PSM (Protocol/Service Multiplexer) common values:
    - 0x0001: SDP
    - 0x0003: RFCOMM
    - 0x000F: BNEP (Bluetooth Network)
    - 0x0011: HID Control
    - 0x0013: HID Interrupt
    - 0x0015: AVCTP (AV Control)
    - 0x0017: AVDTP (AV Distribution)
    - 0x001B: ATT (Attribute Protocol)
    """
    if psm_range is None:
        psm_range = [0x0001, 0x0003, 0x000F, 0x0011, 0x0013, 0x0015, 0x0017]

    device_responded = False
    responding_psms = []

    try:
        for psm in psm_range:
            try:
                # Use l2ping with specific options
                # -s 0 sends minimal data, -c 1 for single attempt
                result = subprocess.run(
                    ['l2ping', '-i', interface, '-c', '1', '-t', '2', bd_address],
                    capture_output=True, text=True, timeout=4
                )

                if 'bytes from' in result.stdout or 'time' in result.stdout:
                    device_responded = True
                    responding_psms.append(psm)
                    add_log(f"L2CAP response from {bd_address}", "DEBUG")
                    break  # One response is enough to confirm presence

            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                pass

        return {'responded': device_responded, 'psms': responding_psms}

    except Exception as e:
        add_log(f"L2CAP sweep error for {bd_address}: {e}", "WARNING")
        return {'responded': False, 'psms': []}


def rfcomm_probe(bd_address, interface='hci0', channels=None):
    """
    Probe RFCOMM channels to detect device and enumerate services.

    Many paired devices have open RFCOMM channels that will respond
    even when not in discoverable mode.
    """
    if channels is None:
        channels = list(range(1, 16))  # Channels 1-15

    responding_channels = []

    try:
        for channel in channels:
            try:
                # Try to connect briefly
                result = subprocess.run(
                    ['rfcomm', '-i', interface, 'connect', '/dev/rfcomm99', bd_address, str(channel)],
                    capture_output=True, text=True, timeout=3
                )
                # Even a rejection means the device is present
                if 'connected' in result.stdout.lower() or 'refused' in result.stderr.lower():
                    responding_channels.append(channel)
            except subprocess.TimeoutExpired:
                pass
            except:
                pass
            finally:
                # Clean up
                subprocess.run(['rfcomm', 'release', '/dev/rfcomm99'], capture_output=True, timeout=2)

        return {'responded': len(responding_channels) > 0, 'channels': responding_channels}

    except Exception as e:
        add_log(f"RFCOMM probe error for {bd_address}: {e}", "WARNING")
        return {'responded': False, 'channels': []}


def address_sweep(oui_prefix, interface='hci0', range_size=16, known_addresses=None):
    """
    Sweep a range of BD addresses based on OUI prefix.

    Useful when you know a device's manufacturer but not the full address.
    For example, if you see an Apple device AA:BB:CC:XX:XX:XX, you can
    sweep nearby addresses to find other devices from the same batch.

    This is computationally expensive but can find "hidden" devices.
    """
    global address_sweep_results

    if known_addresses is None:
        known_addresses = set()

    found_devices = []
    oui_prefix = oui_prefix.upper().replace(':', '')[:6]  # First 6 hex chars

    try:
        add_log(f"Starting address sweep for OUI {oui_prefix} (range: {range_size})", "INFO")

        # If we have known addresses with this OUI, focus around them
        if known_addresses:
            for known_addr in known_addresses:
                if known_addr.upper().replace(':', '')[:6] == oui_prefix:
                    # Extract the NAP (last 3 bytes)
                    nap = int(known_addr.replace(':', '')[6:], 16)

                    # Sweep around this address
                    for offset in range(-range_size//2, range_size//2 + 1):
                        if offset == 0:
                            continue

                        new_nap = (nap + offset) & 0xFFFFFF  # Keep within 24 bits
                        bd_addr = f"{oui_prefix[0:2]}:{oui_prefix[2:4]}:{oui_prefix[4:6]}:" \
                                  f"{new_nap >> 16:02X}:{(new_nap >> 8) & 0xFF:02X}:{new_nap & 0xFF:02X}"

                        if bd_addr.upper() not in known_addresses:
                            # Quick presence check with l2ping
                            presence = l2cap_ping_sweep(bd_addr, interface)
                            if presence['responded']:
                                add_log(f"Sweep found device: {bd_addr}", "INFO")
                                found_devices.append({
                                    'bd_address': bd_addr,
                                    'device_type': 'classic',
                                    'manufacturer': get_manufacturer(bd_addr),
                                    'discovery_method': 'address_sweep'
                                })
        else:
            # Random sweep within OUI (less effective but can work)
            for i in range(range_size):
                nap = (int(time.time() * 1000) + i * 7919) & 0xFFFFFF  # Pseudo-random
                bd_addr = f"{oui_prefix[0:2]}:{oui_prefix[2:4]}:{oui_prefix[4:6]}:" \
                          f"{nap >> 16:02X}:{(nap >> 8) & 0xFF:02X}:{nap & 0xFF:02X}"

                presence = l2cap_ping_sweep(bd_addr, interface)
                if presence['responded']:
                    add_log(f"Sweep found device: {bd_addr}", "INFO")
                    found_devices.append({
                        'bd_address': bd_addr,
                        'device_type': 'classic',
                        'manufacturer': get_manufacturer(bd_addr),
                        'discovery_method': 'address_sweep'
                    })

        address_sweep_results[oui_prefix] = found_devices
        add_log(f"Address sweep complete. Found {len(found_devices)} devices", "INFO")
        return found_devices

    except Exception as e:
        add_log(f"Address sweep error: {e}", "ERROR")
        return found_devices


def page_scan_optimization(interface='hci0'):
    """
    Optimize the local adapter's page scan parameters for maximum
    responsiveness to incoming connections.

    This helps when tracking paired devices that might try to
    reconnect to their paired device.
    """
    try:
        # Enable page scan and inquiry scan
        subprocess.run(['hciconfig', interface, 'piscan'], capture_output=True, timeout=5)

        # Set scan mode to interlaced
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x0043', '01'],  # Inquiry scan type
            capture_output=True, timeout=5
        )
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x0047', '01'],  # Page scan type
            capture_output=True, timeout=5
        )

        # Set class of device to look like common pairable device
        # This can help trigger reconnection attempts from paired devices
        # Major class: Phone (0x02), Minor class: Smartphone (0x0C)
        # Service class: Audio, Telephony (0x200420)
        subprocess.run(
            ['hciconfig', interface, 'class', '0x5A020C'],
            capture_output=True, timeout=5
        )

        add_log(f"Page scan optimized on {interface}", "DEBUG")
        return True

    except Exception as e:
        add_log(f"Page scan optimization error: {e}", "WARNING")
        return False


def truncated_page_scan(bd_address, interface='hci0'):
    """
    Use truncated page scan to detect if a device is present without
    completing the full paging procedure.

    This is faster and can detect devices that might reject connections.
    Uses HCI commands directly for truncated page mode.
    """
    try:
        # Enable truncated page state
        # OGF 0x03, OCF 0x005B (Write Truncated Page Scan)
        # This is BT 2.1+ feature
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x005B', '01'],
            capture_output=True, timeout=5
        )

        # Attempt to page the device
        bd_bytes = bd_address.replace(':', '').lower()
        page_cmd = ['hcitool', '-i', interface, 'cmd', '0x01', '0x0005']  # Create Connection
        for i in range(0, 12, 2):
            page_cmd.append(bd_bytes[10-i:12-i])  # BD_ADDR in reverse
        page_cmd.extend(['18', 'cc', '01', '00', '00', '00'])  # Packet type, mode, clock offset

        result = subprocess.run(page_cmd, capture_output=True, text=True, timeout=5)

        # Check for page response in btmon or hci events
        time.sleep(0.5)

        # Cancel the connection attempt
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x01', '0x0008'] +
            [bd_bytes[10-i:12-i] for i in range(0, 12, 2)],  # Create Connection Cancel
            capture_output=True, timeout=5
        )

        # Disable truncated page
        subprocess.run(
            ['hcitool', '-i', interface, 'cmd', '0x03', '0x005B', '00'],
            capture_output=True, timeout=5
        )

        return True  # Device responded to page if we got here without errors

    except Exception as e:
        add_log(f"Truncated page scan error for {bd_address}: {e}", "DEBUG")
        return False


def deep_scan_device(bd_address, interface='hci0'):
    """
    Perform comprehensive scan of a specific BD address using all available techniques.
    Use this when you have a suspected device address but need to confirm presence
    and gather information.
    """
    add_log(f"Deep scanning {bd_address} on {interface}", "INFO")

    device_info = {
        'bd_address': bd_address,
        'confirmed': False,
        'methods_responded': [],
        'services': [],
        'rssi': None,
        'device_type': 'classic',
        'manufacturer': get_manufacturer(bd_address)
    }

    # Method 1: L2CAP ping
    l2cap_result = l2cap_ping_sweep(bd_address, interface)
    if l2cap_result['responded']:
        device_info['confirmed'] = True
        device_info['methods_responded'].append('l2cap')

    # Method 2: SDP probe
    sdp_result = sdp_probe(bd_address, interface)
    if sdp_result['responded']:
        device_info['confirmed'] = True
        device_info['methods_responded'].append('sdp')
        device_info['services'] = sdp_result['services']

    # Method 3: Name request
    try:
        result = subprocess.run(
            ['hcitool', '-i', interface, 'name', bd_address],
            capture_output=True, text=True, timeout=8
        )
        if result.stdout.strip():
            device_info['confirmed'] = True
            device_info['methods_responded'].append('name')
            device_info['device_name'] = result.stdout.strip()
    except:
        pass

    # Method 4: RSSI (requires connection)
    try:
        subprocess.run(['hcitool', '-i', interface, 'cc', bd_address],
                      capture_output=True, timeout=5)
        time.sleep(0.2)
        result = subprocess.run(
            ['hcitool', '-i', interface, 'rssi', bd_address],
            capture_output=True, text=True, timeout=3
        )
        rssi_match = re.search(r'RSSI return value:\s*(-?\d+)', result.stdout)
        if rssi_match:
            device_info['confirmed'] = True
            device_info['methods_responded'].append('rssi')
            device_info['rssi'] = int(rssi_match.group(1))
    except:
        pass
    finally:
        # Disconnect
        subprocess.run(['hcitool', '-i', interface, 'dc', bd_address],
                      capture_output=True, timeout=2)

    if device_info['confirmed']:
        add_log(f"Deep scan confirmed {bd_address} via {device_info['methods_responded']}", "INFO")
    else:
        add_log(f"Deep scan: {bd_address} not responding", "DEBUG")

    return device_info


def advanced_classic_scan(interface='hci0', duration=30, aggressive=True):
    """
    Master function for advanced Classic Bluetooth scanning.
    Combines all techniques to maximize device detection.

    Parameters:
    - interface: HCI interface to use
    - duration: Total scan duration in seconds
    - aggressive: Enable aggressive techniques (SDP probing, sweeps)

    Returns list of found devices with detection method metadata.
    """
    global devices

    all_found = []
    seen_addresses = set()

    try:
        add_log(f"Starting advanced Classic scan on {interface} ({duration}s)", "INFO")

        # Phase 1: Configure optimal HCI parameters
        set_hci_scan_parameters(interface, inquiry_mode='extended', page_scan_type='interlaced')

        # Enable page scan to catch reconnecting devices
        page_scan_optimization(interface)

        # Phase 2: Aggressive inquiry
        inquiry_duration = min(15, duration // 2)
        inquiry_results = aggressive_inquiry(interface, inquiry_duration)
        for dev in inquiry_results:
            if dev['bd_address'] not in seen_addresses:
                seen_addresses.add(dev['bd_address'])
                all_found.append(dev)

        # Phase 3: Standard stimulation scan
        stim_results = stimulate_bluetooth_classic(interface)
        for dev in stim_results:
            if dev['bd_address'] not in seen_addresses:
                seen_addresses.add(dev['bd_address'])
                dev['discovery_method'] = 'stimulation'
                all_found.append(dev)

        if aggressive:
            # Phase 4: SDP probe known devices that might have hidden neighbors
            for bd_addr in list(devices.keys())[:10]:  # Limit to 10 to avoid timeout
                if devices[bd_addr].get('device_type') == 'classic':
                    sdp_result = sdp_probe(bd_addr, interface)
                    if sdp_result['responded'] and bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        all_found.append({
                            'bd_address': bd_addr,
                            'device_type': 'classic',
                            'services': sdp_result['services'],
                            'discovery_method': 'sdp_probe'
                        })

            # Phase 5: Address sweep for interesting OUIs (Apple, Samsung, etc.)
            interesting_ouis = ['D0:03:4B', 'AC:BC:32', '00:0A:95', '00:1A:7D']  # Common target OUIs
            for oui in interesting_ouis:
                if any(d['bd_address'].upper().startswith(oui.replace(':', '').upper()[:6].replace(':', '')[:2] + ':' +
                       oui.replace(':', '').upper()[2:4] + ':' + oui.replace(':', '').upper()[4:6])
                       for d in all_found):
                    sweep_results = address_sweep(oui, interface, range_size=8,
                                                 known_addresses=seen_addresses)
                    for dev in sweep_results:
                        if dev['bd_address'] not in seen_addresses:
                            seen_addresses.add(dev['bd_address'])
                            all_found.append(dev)

        add_log(f"Advanced scan complete. Found {len(all_found)} devices", "INFO")
        return all_found

    except Exception as e:
        add_log(f"Advanced scan error: {e}", "ERROR")
        return all_found


def start_advanced_scan(interface='hci0', duration=30, aggressive=True):
    """Start advanced scanning in a background thread."""
    global advanced_scan_active, advanced_scan_thread

    if advanced_scan_active:
        add_log("Advanced scan already running", "WARNING")
        return False

    advanced_scan_active = True

    def scan_worker():
        global advanced_scan_active
        try:
            results = advanced_classic_scan(interface, duration, aggressive)
            for dev in results:
                process_found_device(dev)
        finally:
            advanced_scan_active = False

    advanced_scan_thread = threading.Thread(target=scan_worker, daemon=True)
    advanced_scan_thread.start()
    add_log("Advanced scan started in background", "INFO")
    return True


def stop_advanced_scan():
    """Stop the advanced scan."""
    global advanced_scan_active
    advanced_scan_active = False
    add_log("Advanced scan stop requested", "INFO")


def hidden_device_hunt(interface='hci0', duration=45):
    """
    Aggressive scan specifically designed to detect hidden phones and smartwatches.

    Modern phones and watches are typically "non-discoverable" meaning they don't respond
    to standard Bluetooth inquiry scans. This function uses multiple techniques:

    1. Extended BLE scanning - Most modern phones/watches use BLE advertisements
    2. Passive HCI monitoring - Captures any RF activity
    3. Page scan spoofing - Advertise as common device to trigger reconnects
    4. Known OUI probing - L2ping probe known phone/watch manufacturer addresses
    5. Device class filtering - Focus on phone/wearable device classes

    Returns list of detected devices with enhanced classification.
    """
    global hidden_scan_active

    if hidden_scan_active:
        add_log("Hidden device hunt already running", "WARNING")
        return []

    hidden_scan_active = True
    devices_found = []
    seen_addresses = set()

    try:
        add_log(f"Starting HIDDEN DEVICE HUNT on {interface} ({duration}s)", "INFO")
        add_log("Targeting: Phones, Smartwatches, Fitness Trackers", "INFO")

        # Phase 1: Configure adapter for maximum sensitivity
        add_log("Phase 1: Configuring adapter for maximum sensitivity...", "INFO")
        subprocess.run(['hciconfig', interface, 'up'], capture_output=True, timeout=5)
        set_hci_scan_parameters(interface, inquiry_mode='extended', page_scan_type='interlaced')

        # Enable page scan to catch paired devices trying to reconnect
        page_scan_optimization(interface)

        if not hidden_scan_active:
            return devices_found

        # Phase 2: Extended BLE passive scan (primary method for modern devices)
        # Most phones/watches advertise via BLE even when Classic is hidden
        add_log("Phase 2: Extended BLE passive scan (20s)...", "INFO")
        try:
            # Use btmgmt for lower-level BLE scanning
            subprocess.run(['btmgmt', '-i', interface, 'le', 'on'], capture_output=True, timeout=5)

            # Start aggressive BLE scan with duplicate filtering disabled
            proc = subprocess.Popen(
                ['stdbuf', '-oL', 'timeout', '20', 'bluetoothctl', 'scan', 'le'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            for line in iter(proc.stdout.readline, ''):
                if not hidden_scan_active:
                    proc.terminate()
                    break
                if not line:
                    break
                line = line.strip()

                # Parse device discoveries
                new_match = re.search(r'\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)', line)
                if new_match:
                    bd_addr = new_match.group(1).upper()
                    name = new_match.group(2).strip() or None

                    if bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        manufacturer = get_manufacturer(bd_addr)
                        is_phone_watch = is_phone_watch_oui(bd_addr)

                        device_info = {
                            'bd_address': bd_addr,
                            'device_name': name,
                            'device_type': 'ble',
                            'manufacturer': manufacturer,
                            'discovery_method': 'hidden_hunt_ble',
                            'is_phone_watch': is_phone_watch
                        }

                        if is_phone_watch:
                            add_log(f"[PHONE/WATCH] Found: {bd_addr} ({manufacturer})", "INFO")

                        devices_found.append(device_info)

            proc.wait(timeout=5)
        except Exception as e:
            add_log(f"BLE passive scan error: {e}", "DEBUG")

        if not hidden_scan_active:
            return devices_found

        # Phase 3: Classic inquiry with aggressive parameters
        add_log("Phase 3: Aggressive Classic inquiry...", "INFO")
        try:
            # Use multiple LAP codes
            lap_codes = ['9e8b33', '9e8b00', '9e8b01']

            for lap in lap_codes:
                if not hidden_scan_active:
                    break
                try:
                    # Extended inquiry
                    subprocess.run(
                        ['hcitool', '-i', interface, 'cmd', '0x01', '0x0001',
                         lap[4:6], lap[2:4], lap[0:2], '0C', '00'],  # 12*1.28s = ~15s
                        capture_output=True, text=True, timeout=3
                    )
                except:
                    pass

            # Standard inquiry with flush
            result = subprocess.run(
                ['hcitool', '-i', interface, 'inq', '--flush', '--length=10'],
                capture_output=True, text=True, timeout=20
            )

            for line in result.stdout.splitlines():
                match = re.search(r'([0-9A-Fa-f:]{17})', line)
                if match:
                    bd_addr = match.group(1).upper()
                    if bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        manufacturer = get_manufacturer(bd_addr)
                        is_phone_watch = is_phone_watch_oui(bd_addr)

                        # Parse device class if available
                        class_match = re.search(r'class:\s*0x([0-9a-fA-F]+)', line)
                        device_class = class_match.group(1) if class_match else None

                        device_info = {
                            'bd_address': bd_addr,
                            'device_name': None,
                            'device_type': 'classic',
                            'manufacturer': manufacturer,
                            'device_class': device_class,
                            'discovery_method': 'hidden_hunt_inquiry',
                            'is_phone_watch': is_phone_watch
                        }

                        if is_phone_watch:
                            add_log(f"[PHONE/WATCH] Found Classic: {bd_addr} ({manufacturer})", "INFO")

                        devices_found.append(device_info)

        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            add_log(f"Classic inquiry error: {e}", "DEBUG")

        if not hidden_scan_active:
            return devices_found

        # Phase 4: Probe known devices from previous scans
        # L2ping devices with phone/watch OUIs that we've seen before
        add_log("Phase 4: Probing known phone/watch addresses...", "INFO")
        try:
            probed = 0
            max_probes = 20  # Limit to avoid excessive time

            # Get addresses from current device list that are phone/watch OUIs
            with devices_lock:
                known_phone_addresses = [
                    addr for addr, dev in devices.items()
                    if is_phone_watch_oui(addr) and addr not in seen_addresses
                ]

            for bd_addr in known_phone_addresses[:max_probes]:
                if not hidden_scan_active:
                    break

                try:
                    result = subprocess.run(
                        ['l2ping', '-i', interface, '-c', '1', '-t', '2', bd_addr],
                        capture_output=True, text=True, timeout=4
                    )

                    if 'bytes from' in result.stdout or 'time=' in result.stdout:
                        add_log(f"[PHONE/WATCH] Confirmed active: {bd_addr}", "INFO")
                        seen_addresses.add(bd_addr)

                        # Update last seen in existing device
                        with devices_lock:
                            if bd_addr in devices:
                                devices[bd_addr]['last_seen'] = datetime.now(timezone.utc).isoformat()

                        probed += 1
                except:
                    pass

            add_log(f"Probed {probed} known phone/watch devices", "DEBUG")

        except Exception as e:
            add_log(f"Known device probe error: {e}", "DEBUG")

        if not hidden_scan_active:
            return devices_found

        # Phase 5: Name resolution for discovered devices
        add_log("Phase 5: Resolving device names...", "INFO")
        for dev in devices_found:
            if not hidden_scan_active:
                break
            if dev.get('device_name') is None:
                try:
                    result = subprocess.run(
                        ['hcitool', '-i', interface, 'name', dev['bd_address']],
                        capture_output=True, text=True, timeout=5
                    )
                    name = result.stdout.strip()
                    if name and len(name) > 0 and name != dev['bd_address']:
                        dev['device_name'] = name
                        if dev.get('is_phone_watch'):
                            add_log(f"[PHONE/WATCH] Name resolved: {dev['bd_address']} = {name}", "INFO")
                except:
                    pass

        # Summary
        phone_watch_count = sum(1 for d in devices_found if d.get('is_phone_watch'))
        add_log(f"Hidden device hunt complete: {len(devices_found)} devices ({phone_watch_count} phones/watches)", "INFO")

        return devices_found

    except Exception as e:
        add_log(f"Hidden device hunt error: {e}", "ERROR")
        return devices_found
    finally:
        hidden_scan_active = False


def start_hidden_device_hunt(interface='hci0', duration=45):
    """Start hidden device hunt in a background thread."""
    global hidden_scan_active, hidden_scan_thread

    if hidden_scan_active:
        add_log("Hidden device hunt already running", "WARNING")
        return False

    hidden_scan_active = True

    def hunt_worker():
        global hidden_scan_active
        try:
            results = hidden_device_hunt(interface, duration)
            for dev in results:
                process_found_device(dev)
        finally:
            hidden_scan_active = False

    hidden_scan_thread = threading.Thread(target=hunt_worker, daemon=True)
    hidden_scan_thread.start()
    add_log("Hidden device hunt started in background", "INFO")
    return True


def stop_hidden_device_hunt():
    """Stop the hidden device hunt."""
    global hidden_scan_active
    hidden_scan_active = False
    add_log("Hidden device hunt stop requested", "INFO")


def ble_scan_for_targets(target_addresses, interface='hci0', duration=15):
    """
    Scan for BLE advertisements and check if any match target addresses.

    Modern phones/devices often advertise via BLE even when Classic Bluetooth
    is non-discoverable. This function runs a BLE scan and returns any
    target addresses found along with their RSSI and name if available.

    Args:
        target_addresses: Set of target BD addresses (uppercase) to look for
        interface: Bluetooth interface to use
        duration: Scan duration in seconds

    Returns:
        dict: {bd_address: {'rssi': int, 'name': str, 'found': True}}
    """
    found_targets = {}
    seen_addresses = {}  # Track all seen addresses with their info

    try:
        add_log(f"BLE scan for {len(target_addresses)} target(s) ({duration}s)...", "INFO")

        # Ensure LE is enabled
        subprocess.run(['btmgmt', '-i', interface, 'le', 'on'],
                      capture_output=True, timeout=5)

        # Use bluetoothctl for BLE scanning - it provides RSSI
        proc = subprocess.Popen(
            ['stdbuf', '-oL', 'timeout', str(duration), 'bluetoothctl', 'scan', 'le'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in iter(proc.stdout.readline, ''):
            if not target_survey_active:
                proc.terminate()
                break
            if not line:
                break
            line = line.strip()

            # Parse [NEW] Device XX:XX:XX:XX:XX:XX Name
            new_match = re.search(r'\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)', line)
            if new_match:
                bd_addr = new_match.group(1).upper()
                name = new_match.group(2).strip() or None
                seen_addresses[bd_addr] = {'name': name, 'rssi': None}

                # Check if this is a target
                if bd_addr in target_addresses:
                    add_log(f"  BLE: Target {bd_addr} found! Name: {name}", "INFO")
                    found_targets[bd_addr] = {
                        'found': True,
                        'name': name,
                        'rssi': None,
                        'method': 'ble_advertisement'
                    }

            # Parse RSSI updates: [CHG] Device XX:XX:XX:XX:XX:XX RSSI: -XX
            rssi_match = re.search(r'\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+RSSI:\s*(-?\d+)', line)
            if rssi_match:
                bd_addr = rssi_match.group(1).upper()
                rssi = int(rssi_match.group(2))

                if bd_addr in seen_addresses:
                    seen_addresses[bd_addr]['rssi'] = rssi

                if bd_addr in target_addresses:
                    if bd_addr not in found_targets:
                        found_targets[bd_addr] = {
                            'found': True,
                            'name': seen_addresses.get(bd_addr, {}).get('name'),
                            'rssi': rssi,
                            'method': 'ble_advertisement'
                        }
                    else:
                        found_targets[bd_addr]['rssi'] = rssi
                    add_log(f"  BLE: Target {bd_addr} RSSI: {rssi} dBm", "DEBUG")

        proc.wait(timeout=5)

    except Exception as e:
        add_log(f"BLE target scan error: {e}", "WARNING")

    add_log(f"BLE scan complete: {len(found_targets)}/{len(target_addresses)} targets found, "
            f"{len(seen_addresses)} total BLE devices seen", "INFO")

    return found_targets


def target_survey(interface='hci0'):
    """
    Actively probe all targets to determine if they are in the area.

    Uses multiple detection techniques:
    Phase 0: BLE advertisement scan - catches modern devices advertising via BLE
    Phase 1: Classic Bluetooth paging - for connectable Classic devices
      - Multiple HCI name requests with extended page timeout
      - HCI info inquiry (Read Remote Extended Features)
      - L2CAP ping
      - SDP probe for services
      - RSSI measurement

    These methods work on non-discoverable devices that are still connectable.
    Returns dict of BD addresses with their survey results.

    Note: Called from start_target_survey() which handles the active flag.
    """
    global target_survey_results

    # Reset results for this run
    target_survey_results = {}

    try:
        # Get targets from database
        db_targets = get_targets_from_db()

        if not db_targets:
            add_log("No targets defined for survey", "WARNING")
            return target_survey_results

        add_log(f"Starting target survey: probing {len(db_targets)} target(s)", "INFO")
        socketio.emit('target_survey_started', {
            'target_count': len(db_targets),
            'targets': [t['bd_address'] for t in db_targets]
        })

        # Build target address set for quick lookup
        target_address_set = {t['bd_address'].upper() for t in db_targets}
        target_alias_map = {t['bd_address'].upper(): t.get('alias', '') for t in db_targets}

        # =====================================================================
        # PHASE 0: BLE Advertisement Scan
        # Modern phones/devices often advertise via BLE even when Classic is hidden
        # This is the most effective method for detecting modern smartphones
        # =====================================================================
        add_log("Phase 0: BLE advertisement scan for targets...", "INFO")
        socketio.emit('target_survey_phase', {'phase': 0, 'description': 'BLE Advertisement Scan'})

        ble_found = {}
        if target_survey_active:
            ble_found = ble_scan_for_targets(target_address_set, interface, duration=15)

            # Process BLE-found targets immediately
            for bd_addr, ble_info in ble_found.items():
                alias = target_alias_map.get(bd_addr, '')
                display_name = f"{alias} ({bd_addr})" if alias else bd_addr

                result = {
                    'bd_address': bd_addr,
                    'alias': alias,
                    'present': True,
                    'methods_responded': ['ble_advertisement'],
                    'rssi': ble_info.get('rssi'),
                    'device_name': ble_info.get('name'),
                    'device_info': None,
                    'services': [],
                    'probe_time': time.time(),
                    'detection_phase': 'ble'
                }
                target_survey_results[bd_addr] = result
                socketio.emit('target_survey_result', result)

                add_log(f"TARGET FOUND (BLE): {display_name}", "WARNING")

                # Process as found device
                device_data = {
                    'bd_address': bd_addr,
                    'device_name': ble_info.get('name'),
                    'device_type': 'ble',
                    'manufacturer': get_manufacturer(bd_addr),
                    'rssi': ble_info.get('rssi'),
                    'discovery_method': 'target_survey_ble'
                }
                process_found_device(device_data)

        if not target_survey_active:
            return target_survey_results

        # =====================================================================
        # PHASE 1: Classic Bluetooth Paging
        # For targets not found via BLE, try Classic probing techniques
        # =====================================================================
        add_log("Phase 1: Classic Bluetooth paging for remaining targets...", "INFO")
        socketio.emit('target_survey_phase', {'phase': 1, 'description': 'Classic Bluetooth Paging'})

        # Configure HCI for aggressive probing
        # Set extended page timeout (0x8000 = ~20 seconds max)
        try:
            # Write Page Timeout - OGF 0x03, OCF 0x0018
            # Value 0x6000 = 15.36 seconds (slot = 0.625ms, so 0x6000 * 0.625 = 15360ms)
            subprocess.run(
                ['hcitool', '-i', interface, 'cmd', '0x03', '0x0018', '00', '60'],
                capture_output=True, timeout=3
            )
            add_log(f"Set extended page timeout on {interface}", "DEBUG")
        except Exception as e:
            add_log(f"Could not set page timeout: {e}", "DEBUG")

        # Set interlaced page scan for faster responses
        set_hci_scan_parameters(interface, inquiry_mode='extended', page_scan_type='interlaced')

        # Filter to targets not already found via BLE
        remaining_targets = [t for t in db_targets if t['bd_address'].upper() not in ble_found]
        add_log(f"Classic probing {len(remaining_targets)} target(s) not found via BLE", "INFO")

        for i, target in enumerate(remaining_targets):
            if not target_survey_active:
                add_log("Target survey cancelled", "INFO")
                break

            bd_address = target['bd_address'].upper()
            alias = target.get('alias', '')
            display_name = f"{alias} ({bd_address})" if alias else bd_address

            add_log(f"Probing target {i+1}/{len(remaining_targets)}: {display_name}", "INFO")
            socketio.emit('target_survey_progress', {
                'current': i + 1,
                'total': len(remaining_targets),
                'bd_address': bd_address,
                'alias': alias,
                'phase': 'classic'
            })

            result = {
                'bd_address': bd_address,
                'alias': alias,
                'present': False,
                'methods_responded': [],
                'rssi': None,
                'device_name': None,
                'device_info': None,
                'services': [],
                'probe_time': time.time(),
                'detection_phase': 'classic'
            }

            # Method 1: Multiple HCI name requests (most effective for non-discoverable)
            # Try up to 3 times with increasing timeouts
            for attempt in range(3):
                if result['device_name'] or not target_survey_active:
                    break
                try:
                    timeout_sec = 8 + (attempt * 4)  # 8s, 12s, 16s
                    add_log(f"  NAME attempt {attempt+1}/3 (timeout {timeout_sec}s)...", "DEBUG")
                    name_result = subprocess.run(
                        ['hcitool', '-i', interface, 'name', bd_address],
                        capture_output=True, text=True, timeout=timeout_sec
                    )
                    name = name_result.stdout.strip()
                    if name and not name.startswith('n/a') and 'error' not in name.lower():
                        result['present'] = True
                        result['methods_responded'].append('name')
                        result['device_name'] = name
                        add_log(f"  NAME: {bd_address} = '{name}'", "INFO")
                        break
                except subprocess.TimeoutExpired:
                    add_log(f"  NAME attempt {attempt+1}: timeout", "DEBUG")
                except Exception as e:
                    add_log(f"  NAME attempt {attempt+1} error: {e}", "DEBUG")

            if not target_survey_active:
                break

            # Method 2: HCI info inquiry (Read Remote Extended Features)
            # This often works even when name request fails
            if not result['present']:
                try:
                    add_log(f"  Trying HCI info inquiry...", "DEBUG")
                    info_result = subprocess.run(
                        ['hcitool', '-i', interface, 'info', bd_address],
                        capture_output=True, text=True, timeout=15
                    )
                    output = info_result.stdout
                    if 'BD Address' in output and 'Device Name' in output:
                        result['present'] = True
                        result['methods_responded'].append('info')
                        # Extract device name from info output
                        name_match = re.search(r'Device Name:\s*(.+)', output)
                        if name_match and not result['device_name']:
                            result['device_name'] = name_match.group(1).strip()
                        # Extract features
                        result['device_info'] = output
                        add_log(f"  INFO: {bd_address} responded", "INFO")
                    elif info_result.returncode == 0 and output.strip():
                        # Got some response even if partial
                        result['present'] = True
                        result['methods_responded'].append('info_partial')
                        add_log(f"  INFO: {bd_address} partial response", "DEBUG")
                except subprocess.TimeoutExpired:
                    add_log(f"  INFO: {bd_address} timeout", "DEBUG")
                except Exception as e:
                    add_log(f"  INFO error: {e}", "DEBUG")

            if not target_survey_active:
                break

            # Method 3: L2CAP ping (works on connectable devices)
            if not result['present']:
                try:
                    add_log(f"  Trying L2PING...", "DEBUG")
                    l2ping_result = subprocess.run(
                        ['l2ping', '-i', interface, '-c', '3', '-t', '5', bd_address],
                        capture_output=True, text=True, timeout=20
                    )
                    if 'bytes from' in l2ping_result.stdout.lower():
                        result['present'] = True
                        result['methods_responded'].append('l2ping')
                        # Extract RTT
                        rtt_match = re.search(r'time=(\d+\.?\d*)ms', l2ping_result.stdout)
                        if rtt_match:
                            result['rtt_ms'] = float(rtt_match.group(1))
                        add_log(f"  L2PING: {bd_address} responded", "INFO")
                except subprocess.TimeoutExpired:
                    add_log(f"  L2PING: {bd_address} timeout", "DEBUG")
                except Exception as e:
                    add_log(f"  L2PING error: {e}", "DEBUG")

            if not target_survey_active:
                break

            # Method 4: SDP probe (check services)
            if not result['present']:
                try:
                    add_log(f"  Trying SDP probe...", "DEBUG")
                    sdp_result = sdp_probe(bd_address, interface)
                    if sdp_result['responded']:
                        result['present'] = True
                        result['methods_responded'].append('sdp')
                        result['services'] = sdp_result.get('services', [])
                        add_log(f"  SDP: {bd_address} has {len(result['services'])} services", "INFO")
                except Exception as e:
                    add_log(f"  SDP error: {e}", "DEBUG")

            if not target_survey_active:
                break

            # Method 5: Try direct connection and RSSI if still not found
            # This is last resort - attempts to create an ACL connection
            if not result['present']:
                try:
                    add_log(f"  Trying direct connection...", "DEBUG")
                    # Create connection with role switch allowed
                    cc_result = subprocess.run(
                        ['hcitool', '-i', interface, 'cc', '--role=m', bd_address],
                        capture_output=True, text=True, timeout=10
                    )
                    time.sleep(0.5)

                    # Check if connection was made
                    con_result = subprocess.run(
                        ['hcitool', '-i', interface, 'con'],
                        capture_output=True, text=True, timeout=3
                    )
                    if bd_address.lower() in con_result.stdout.lower():
                        result['present'] = True
                        result['methods_responded'].append('connection')
                        add_log(f"  CONNECTION: {bd_address} connected", "INFO")

                        # Try to get RSSI while connected
                        try:
                            rssi_result = subprocess.run(
                                ['hcitool', '-i', interface, 'rssi', bd_address],
                                capture_output=True, text=True, timeout=3
                            )
                            rssi_match = re.search(r'RSSI return value:\s*(-?\d+)', rssi_result.stdout)
                            if rssi_match:
                                result['rssi'] = int(rssi_match.group(1))
                                result['methods_responded'].append('rssi')
                                add_log(f"  RSSI: {result['rssi']} dBm", "DEBUG")
                        except:
                            pass
                except subprocess.TimeoutExpired:
                    add_log(f"  CONNECTION: {bd_address} timeout", "DEBUG")
                except Exception as e:
                    add_log(f"  CONNECTION error: {e}", "DEBUG")
                finally:
                    # Always disconnect
                    try:
                        subprocess.run(['hcitool', '-i', interface, 'dc', bd_address],
                                      capture_output=True, timeout=2)
                    except:
                        pass

            # Store result
            target_survey_results[bd_address] = result

            # Emit individual result
            socketio.emit('target_survey_result', result)

            # If target found, process it as a device and potentially alert
            if result['present']:
                add_log(f"TARGET FOUND: {display_name} via {result['methods_responded']}", "WARNING")
                device_data = {
                    'bd_address': bd_address,
                    'device_name': result.get('device_name'),
                    'device_type': 'classic',
                    'manufacturer': get_manufacturer(bd_address),
                    'rssi': result.get('rssi'),
                    'discovery_method': 'target_survey'
                }
                process_found_device(device_data)
            else:
                add_log(f"Target not detected: {display_name} (tried all methods)", "DEBUG")

            # Small delay between targets to let radio settle
            if i < len(remaining_targets) - 1:
                time.sleep(0.5)

        # Summary with phase breakdown
        found_count = sum(1 for r in target_survey_results.values() if r['present'])
        ble_count = sum(1 for r in target_survey_results.values()
                       if r['present'] and r.get('detection_phase') == 'ble')
        classic_count = sum(1 for r in target_survey_results.values()
                          if r['present'] and r.get('detection_phase') == 'classic')

        add_log(f"Target survey complete: {found_count}/{len(db_targets)} targets detected "
                f"(BLE: {ble_count}, Classic: {classic_count})", "INFO")

        socketio.emit('target_survey_complete', {
            'total': len(db_targets),
            'found': found_count,
            'found_ble': ble_count,
            'found_classic': classic_count,
            'results': list(target_survey_results.values())
        })

        return target_survey_results

    except Exception as e:
        add_log(f"Target survey error: {e}", "ERROR")
        return target_survey_results
    finally:
        # Reset page timeout to default
        try:
            subprocess.run(
                ['hcitool', '-i', interface, 'cmd', '0x03', '0x0018', '00', '20'],
                capture_output=True, timeout=3
            )
        except:
            pass
        # Note: target_survey_active is set to False by start_target_survey() worker


def start_target_survey(interface='hci0', continuous=False, interval=30):
    """Start target survey in a background thread.

    Args:
        interface: Bluetooth interface to use (default: hci0)
        continuous: If True, run continuously with interval between sweeps
        interval: Seconds between sweeps in continuous mode (default: 30)
    """
    global target_survey_active, target_survey_thread, target_survey_continuous
    global target_survey_interval, target_survey_sweep_count

    if target_survey_active:
        add_log("Target survey already running", "WARNING")
        return False

    target_survey_active = True
    target_survey_continuous = continuous
    target_survey_interval = max(10, interval)  # Minimum 10 seconds between sweeps
    target_survey_sweep_count = 0

    def survey_worker():
        global target_survey_active, target_survey_sweep_count, target_survey_continuous
        try:
            while target_survey_active:
                target_survey_sweep_count += 1
                sweep_start = time.time()

                # Emit sweep starting event
                socketio.emit('target_survey_sweep_start', {
                    'sweep_number': target_survey_sweep_count,
                    'continuous': target_survey_continuous
                })

                # Run the survey
                target_survey(interface)

                sweep_duration = time.time() - sweep_start

                # Emit sweep complete event with results summary
                found_count = sum(1 for r in target_survey_results.values() if r.get('present'))
                total_count = len(target_survey_results)

                socketio.emit('target_survey_sweep_complete', {
                    'sweep_number': target_survey_sweep_count,
                    'found': found_count,
                    'total': total_count,
                    'duration': round(sweep_duration, 1),
                    'continuous': target_survey_continuous,
                    'next_sweep_in': target_survey_interval if target_survey_continuous and target_survey_active else None
                })

                # If not continuous mode, exit after one sweep
                if not target_survey_continuous:
                    break

                # If still active and in continuous mode, wait before next sweep
                if target_survey_active:
                    add_log(f"Target survey sweep {target_survey_sweep_count} complete. Next sweep in {target_survey_interval}s...", "INFO")

                    # Wait in small increments so we can respond to stop requests quickly
                    wait_start = time.time()
                    while target_survey_active and (time.time() - wait_start) < target_survey_interval:
                        time.sleep(1)
                        # Emit countdown update every 5 seconds
                        remaining = target_survey_interval - int(time.time() - wait_start)
                        if remaining > 0 and remaining % 5 == 0:
                            socketio.emit('target_survey_countdown', {
                                'seconds_remaining': remaining,
                                'sweep_number': target_survey_sweep_count + 1
                            })
        finally:
            target_survey_active = False
            target_survey_continuous = False

    target_survey_thread = threading.Thread(target=survey_worker, daemon=True)
    target_survey_thread.start()

    mode_str = f"continuous (interval: {target_survey_interval}s)" if continuous else "single sweep"
    add_log(f"Target survey started in background ({mode_str})", "INFO")
    return True


def stop_target_survey():
    """Stop the target survey."""
    global target_survey_active, target_survey_continuous
    target_survey_active = False
    target_survey_continuous = False
    add_log("Target survey stop requested", "INFO")


def stimulate_ble_devices(interface='hci0'):
    """
    Stimulate BLE devices to respond using extended BLE scanning.
    Uses bluetoothctl scan on with LE-only filter for aggressive device discovery.
    """
    try:
        add_log("Stimulating BLE devices (extended LE scan)...", "INFO")
        devices_found = []
        seen_addresses = set()
        device_rssi = {}

        # Ensure interface is up and ready
        subprocess.run(['hciconfig', interface, 'up'], capture_output=True, timeout=5)
        subprocess.run(['bluetoothctl', 'select', interface], capture_output=True, timeout=5)
        subprocess.run(['bluetoothctl', 'power', 'on'], capture_output=True, timeout=5)

        # Clear any cached devices first to ensure fresh discovery
        try:
            # Get list of known devices and remove them to force fresh discovery
            result = subprocess.run(['bluetoothctl', 'devices'], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                match = re.search(r'Device\s+([0-9A-Fa-f:]{17})', line)
                if match:
                    bd_addr = match.group(1)
                    subprocess.run(['bluetoothctl', 'remove', bd_addr], capture_output=True, timeout=2)
        except:
            pass

        # Method 1: Extended bluetoothctl scan with real-time parsing (12 seconds)
        # bluetoothctl scan on finds both Classic and LE, we filter by device type after
        add_log("Running extended BLE scan (12 seconds)...", "INFO")
        try:
            proc = subprocess.Popen(
                ['stdbuf', '-oL', 'timeout', '12', 'bluetoothctl', 'scan', 'on'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            # Read output in real-time
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
                    name = new_match.group(2).strip() or 'BLE Device'
                    if bd_addr not in seen_addresses:
                        seen_addresses.add(bd_addr)
                        # Default to BLE - will be refined by device type detection
                        device_type = get_device_type(bd_addr)
                        if device_type == 'unknown':
                            device_type = 'ble'  # Assume BLE for stimulation results
                        devices_found.append({
                            'bd_address': bd_addr,
                            'device_name': name,
                            'device_type': device_type,
                            'manufacturer': get_manufacturer(bd_addr),
                            'rssi': device_rssi.get(bd_addr)
                        })

                # Parse RSSI updates
                rssi_match = re.search(r'\[CHG\]\s+Device\s+([0-9A-Fa-f:]{17})\s+RSSI:\s*(-?\d+)', line)
                if rssi_match:
                    bd_addr = rssi_match.group(1).upper()
                    rssi = int(rssi_match.group(2))
                    device_rssi[bd_addr] = rssi
                    # Update existing device
                    for dev in devices_found:
                        if dev['bd_address'] == bd_addr:
                            dev['rssi'] = rssi
                            break

            proc.wait(timeout=2)
        except Exception as e:
            add_log(f"Extended scan error: {e}", "WARNING")

        # Method 2: Try hcitool lescan for additional BLE-only devices (fallback)
        if len(devices_found) < 5:
            add_log("Running supplemental hcitool lescan...", "DEBUG")
            try:
                result = subprocess.run(
                    ['timeout', '6', 'hcitool', '-i', interface, 'lescan'],
                    capture_output=True,
                    text=True
                )

                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue

                    match = re.match(r'([0-9A-Fa-f:]{17})\s*(.*)', line)
                    if match:
                        bd_addr = match.group(1).upper()
                        name_part = match.group(2).strip()

                        if bd_addr in seen_addresses:
                            continue
                        seen_addresses.add(bd_addr)

                        device_name = name_part if name_part and name_part != '(unknown)' else 'BLE Device'

                        devices_found.append({
                            'bd_address': bd_addr,
                            'device_name': device_name,
                            'device_type': 'ble',
                            'rssi': None,
                            'manufacturer': get_manufacturer(bd_addr)
                        })

            except Exception as e:
                add_log(f"hcitool lescan fallback failed: {e}", "DEBUG")

        add_log(f"BLE stimulation found {len(devices_found)} devices", "INFO")
        return devices_found
    except Exception as e:
        add_log(f"BLE stimulation error: {str(e)}", "ERROR")
        return []


def check_target_and_alert(bd_address, source='scan'):
    """Check if a BD address is a target and trigger alert if so."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM targets WHERE bd_address = ?', (bd_address.upper(),))
    target = c.fetchone()
    conn.close()

    if target:
        # Build device info for alert
        device = devices.get(bd_address, {'bd_address': bd_address})
        device['is_target'] = True

        # Check if this is a "new" sighting (for alert purposes)
        # For get info/name operations, we always want to alert
        add_log(f"TARGET detected via {source}: {bd_address}", "WARNING")
        alert_target_found(device, is_new=True)
        return True
    return False


def get_device_info(bd_address, interface='hci0'):
    """Get detailed info about a specific device. Runs hcitool info for up to 10 seconds."""
    info = {'bd_address': bd_address}
    device_responded = False

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

        # Use the comprehensive parser for human-readable output
        parsed_info = parse_device_info_output(result.stdout)
        info['parsed'] = parsed_info['parsed']
        info['analysis'] = parsed_info['analysis']

        # Extract key fields for backward compatibility
        if parsed_info['parsed'].get('device_name'):
            info['device_name'] = parsed_info['parsed']['device_name']
            device_responded = True

        if parsed_info['parsed'].get('device_class'):
            info['device_class'] = parsed_info['parsed']['device_class']
            device_responded = True

        if parsed_info['parsed'].get('manufacturer'):
            info['manufacturer_info'] = parsed_info['parsed']['manufacturer']
            device_responded = True

        if parsed_info['parsed'].get('bluetooth_version'):
            info['bluetooth_version'] = parsed_info['parsed']['bluetooth_version']
            info['version_description'] = parsed_info['parsed'].get('version_description', '')
            device_responded = True

        if parsed_info['parsed'].get('features'):
            info['features'] = parsed_info['parsed']['features']
            info['capabilities'] = parsed_info['parsed'].get('capabilities_summary', {})

        add_log(f"hcitool info complete for {bd_address}", "INFO")

        # Device responded - add to survey table
        if device_responded:
            device_data = {
                'bd_address': bd_address,
                'device_name': info.get('device_name'),
                'device_type': 'classic',
                'manufacturer': info.get('manufacturer_info') or get_manufacturer(bd_address)
            }
            process_found_device(device_data)
            check_target_and_alert(bd_address, source='get_info')

    except subprocess.TimeoutExpired:
        add_log(f"hcitool info timeout for {bd_address} (10s)", "WARNING")
        info['raw_info'] = "Timeout after 10 seconds - device not responding"
        info['analysis'] = ["Device did not respond within 10 seconds"]
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
    target_alerted = False  # Only alert once per session

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

                # Device responded - add to survey table if not already there
                if bd_address not in devices:
                    device_data = {
                        'bd_address': bd_address,
                        'device_name': name,
                        'device_type': 'classic',
                        'manufacturer': get_manufacturer(bd_address)
                    }
                    process_found_device(device_data)
                else:
                    # Update existing device
                    devices[bd_address]['device_name'] = name
                    socketio.emit('device_update', devices[bd_address])

                # Check if target (only once per session)
                if not target_alerted:
                    target_alerted = True
                    check_target_and_alert(bd_address, source='get_name')

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

# RSSI measurement uncertainty in dBm (typical for Bluetooth)
RSSI_UNCERTAINTY = 8.0  # ±8 dBm is typical for BT measurements

def calculate_distance_from_rssi(rssi, tx_power=-59):
    """
    Calculate approximate distance from RSSI using log-distance path loss model.
    tx_power: expected RSSI at 1 meter (typically -59 to -65 dBm for BT)
    Returns (distance_estimate, distance_min, distance_max) for uncertainty bounds.
    """
    if rssi >= 0:
        return (0.1, 0.1, 1.0)

    n = 2.5  # Path loss exponent (2-4 for indoor, 2-3 for outdoor)

    # Central estimate
    distance = 10 ** ((tx_power - rssi) / (10 * n))

    # Uncertainty bounds (RSSI ± uncertainty)
    dist_min = 10 ** ((tx_power - (rssi + RSSI_UNCERTAINTY)) / (10 * n))
    dist_max = 10 ** ((tx_power - (rssi - RSSI_UNCERTAINTY)) / (10 * n))

    return (min(distance, 1000), min(dist_min, 1000), min(dist_max, 1000))


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two GPS coordinates in meters."""
    import math
    R = 6371000  # Earth radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c


def calculate_spatial_diversity(observations):
    """
    Calculate the spatial spread of observation points in meters.
    Higher diversity = better geolocation potential.
    """
    if len(observations) < 2:
        return 0

    max_spread = 0
    for i, obs1 in enumerate(observations):
        for obs2 in observations[i+1:]:
            spread = haversine_distance(obs1[0], obs1[1], obs2[0], obs2[1])
            max_spread = max(max_spread, spread)

    return max_spread


def calculate_bearing(lat1, lon1, lat2, lon2):
    """
    Calculate bearing from point 1 to point 2 in degrees (0-360).
    0 = North, 90 = East, 180 = South, 270 = West
    """
    import math

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    x = math.sin(delta_lambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)

    bearing = math.atan2(x, y)
    bearing = math.degrees(bearing)
    bearing = (bearing + 360) % 360

    return bearing


# Direction finding state for each tracked device
direction_history = {}  # bd_address -> list of (lat, lon, rssi, timestamp)


def calculate_direction_to_target(bd_address):
    """
    Calculate direction to target using RSSI gradient method.

    This works like iPhone Find My without UWB:
    - Track RSSI changes as the operator moves
    - Determine which direction leads to stronger signal
    - Return bearing to target and confidence

    Returns dict with:
    - bearing: degrees (0=N, 90=E, 180=S, 270=W)
    - confidence: 0-100%
    - trend: 'closer', 'farther', 'stable'
    - rssi_delta: change in RSSI
    """
    import math
    from datetime import datetime, timedelta

    history = direction_history.get(bd_address, [])

    if len(history) < 3:
        return None

    # Get recent readings (last 30 seconds)
    cutoff = datetime.now() - timedelta(seconds=30)
    recent = [(h['lat'], h['lon'], h['rssi'], h['timestamp'])
              for h in history if h['timestamp'] > cutoff]

    if len(recent) < 3:
        return None

    # Need sufficient movement to calculate direction
    first_pos = (recent[0][0], recent[0][1])
    last_pos = (recent[-1][0], recent[-1][1])
    movement = haversine_distance(first_pos[0], first_pos[1], last_pos[0], last_pos[1])

    if movement < 2:  # Less than 2m movement - can't determine direction
        # Return last known direction if we have one
        return {
            'bearing': None,
            'confidence': 0,
            'trend': 'stable',
            'rssi_delta': 0,
            'message': 'Move to determine direction'
        }

    # Calculate RSSI gradient along movement path
    # Find direction of movement
    movement_bearing = calculate_bearing(first_pos[0], first_pos[1], last_pos[0], last_pos[1])

    # Calculate RSSI trend
    first_rssi = sum(r[2] for r in recent[:3]) / 3  # Average first 3
    last_rssi = sum(r[2] for r in recent[-3:]) / 3  # Average last 3
    rssi_delta = last_rssi - first_rssi

    # Determine trend
    if rssi_delta > 2:
        trend = 'closer'
        # Moving in the right direction - target is ahead
        target_bearing = movement_bearing
        confidence = min(90, 40 + rssi_delta * 5)
    elif rssi_delta < -2:
        trend = 'farther'
        # Moving away - target is behind us (opposite direction)
        target_bearing = (movement_bearing + 180) % 360
        confidence = min(90, 40 + abs(rssi_delta) * 5)
    else:
        trend = 'stable'
        # Signal stable - target might be perpendicular to movement
        target_bearing = None
        confidence = 20

    # Build spatial RSSI map for better direction estimate
    # Cluster readings by position and find direction of strongest cluster
    if len(recent) >= 5:
        # Find the reading with strongest RSSI
        strongest = max(recent, key=lambda r: r[2])
        strongest_pos = (strongest[0], strongest[1])

        # Calculate bearing to strongest reading position
        if haversine_distance(last_pos[0], last_pos[1], strongest_pos[0], strongest_pos[1]) > 1:
            spatial_bearing = calculate_bearing(last_pos[0], last_pos[1], strongest_pos[0], strongest_pos[1])

            # Blend spatial bearing with movement-based bearing
            if target_bearing is not None:
                # Weight by confidence
                target_bearing = (target_bearing * 0.6 + spatial_bearing * 0.4) % 360
                confidence = min(95, confidence + 10)
            else:
                target_bearing = spatial_bearing
                confidence = 50

    return {
        'bearing': round(target_bearing, 1) if target_bearing is not None else None,
        'confidence': round(confidence),
        'trend': trend,
        'rssi_delta': round(rssi_delta, 1),
        'movement': round(movement, 1),
        'movement_bearing': round(movement_bearing, 1)
    }


def add_direction_reading(bd_address, lat, lon, rssi):
    """Add a reading to direction history for a device."""
    from datetime import datetime

    if bd_address not in direction_history:
        direction_history[bd_address] = []

    direction_history[bd_address].append({
        'lat': lat,
        'lon': lon,
        'rssi': rssi,
        'timestamp': datetime.now()
    })

    # Keep only last 60 readings
    if len(direction_history[bd_address]) > 60:
        direction_history[bd_address] = direction_history[bd_address][-60:]


def clear_direction_history(bd_address=None):
    """Clear direction history for a device or all devices."""
    global direction_history
    if bd_address:
        direction_history.pop(bd_address, None)
    else:
        direction_history = {}


def estimate_emitter_location(bd_address):
    """
    PASSIVE GEO: Estimate emitter location from scanning data.
    Returns (lat, lon, cep_radius) or None.

    This is passive geolocation - CEP will typically be 30-150m.
    For 95% confidence, we need to account for:
    - RSSI measurement uncertainty (±8 dBm)
    - Limited spatial diversity from passive scanning
    - Multipath and environmental factors
    """
    import math

    conn = get_db()
    c = conn.cursor()

    # Get recent RSSI readings
    c.execute('''
        SELECT rssi, system_lat, system_lon, timestamp
        FROM rssi_history
        WHERE bd_address = ? AND timestamp > datetime('now', '-10 minutes')
        ORDER BY timestamp DESC
        LIMIT 30
    ''', (bd_address,))

    readings = c.fetchall()
    conn.close()

    if len(readings) < 3:
        return None

    # Build observation list: (lat, lon, rssi, timestamp)
    observations = []
    for reading in readings:
        rssi, lat, lon, ts = reading
        if lat and lon and rssi and rssi < 0:
            observations.append((lat, lon, rssi, ts))

    if len(observations) < 3:
        return None

    # Calculate spatial diversity
    spatial_diversity = calculate_spatial_diversity(observations)

    # If observations are too clustered (< 5m spread), geo is unreliable
    if spatial_diversity < 5:
        # Still calculate but with large CEP
        pass

    # Weighted centroid calculation with uncertainty
    total_weight = 0
    weighted_lat = 0
    weighted_lon = 0
    distance_estimates = []
    distance_uncertainties = []

    for obs in observations:
        lat, lon, rssi, _ = obs
        dist, dist_min, dist_max = calculate_distance_from_rssi(rssi)

        # Weight by inverse distance squared (closer = more weight)
        weight = 1.0 / (dist * dist + 1)

        weighted_lat += lat * weight
        weighted_lon += lon * weight
        total_weight += weight

        distance_estimates.append(dist)
        distance_uncertainties.append(dist_max - dist_min)

    if total_weight == 0:
        return None

    est_lat = weighted_lat / total_weight
    est_lon = weighted_lon / total_weight

    # === CEP Calculation for 95% Confidence ===
    #
    # For passive geo, we need to be conservative. CEP95 should contain
    # the emitter 95% of the time.
    #
    # Factors:
    # 1. Average distance uncertainty from RSSI measurements
    # 2. Spatial diversity of observations (more spread = better)
    # 3. Number of observations (more = better, but diminishing returns)
    # 4. Base uncertainty floor for passive scanning

    avg_distance = sum(distance_estimates) / len(distance_estimates)
    avg_uncertainty = sum(distance_uncertainties) / len(distance_uncertainties)

    # Base CEP from average distance uncertainty (this is the dominant factor)
    base_cep = avg_uncertainty * 1.5  # Scale factor for 95% confidence

    # Spatial diversity factor (better diversity = tighter CEP)
    # If diversity < 20m, geo is essentially single-point, CEP is large
    if spatial_diversity < 10:
        diversity_factor = 3.0  # Very poor - triple the CEP
    elif spatial_diversity < 30:
        diversity_factor = 2.0  # Poor
    elif spatial_diversity < 50:
        diversity_factor = 1.5  # Moderate
    elif spatial_diversity < 100:
        diversity_factor = 1.2  # Good
    else:
        diversity_factor = 1.0  # Excellent

    # Observation count factor (more observations = slight improvement)
    obs_factor = math.sqrt(10 / max(len(observations), 1))
    obs_factor = max(0.7, min(obs_factor, 1.5))  # Clamp between 0.7 and 1.5

    # Calculate final CEP
    cep_radius = base_cep * diversity_factor * obs_factor

    # Add floor based on average distance (farther = more uncertainty)
    distance_floor = avg_distance * 0.3  # 30% of avg distance as minimum
    cep_radius = max(cep_radius, distance_floor)

    # PASSIVE GEO MINIMUM: 30m (passive scanning cannot reliably do better)
    cep_radius = max(30, cep_radius)

    # Cap at reasonable maximum
    cep_radius = min(200, cep_radius)

    return (est_lat, est_lon, round(cep_radius, 1))


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


# ==================== ACTIVE GEO TRACKING ====================

# Active geo tracking state
active_geo_sessions = {}  # bd_address -> {'thread': Thread, 'active': bool, 'interface': str, 'methods': list}


def active_geo_track(bd_address, interface='hci0', methods=None):
    """
    Actively track a device using selected methods for RSSI/RTT extraction.
    Methods: 'l2ping' (RTT + connection), 'rssi' (direct hcitool rssi)
    """
    global active_geo_sessions, current_location

    if methods is None:
        methods = ['l2ping', 'rssi']

    method_str = '+'.join(methods)
    add_log(f"Starting active geo tracking for {bd_address} ({method_str})", "INFO")

    # Debug: Log session state
    session = active_geo_sessions.get(bd_address)
    if session is None:
        add_log(f"ERROR: No session found for {bd_address} - sessions: {list(active_geo_sessions.keys())}", "ERROR")
        return
    add_log(f"Session state: active={session.get('active')}, interface={session.get('interface')}", "DEBUG")

    ping_count = 0
    successful_pings = 0
    rssi_readings = 0
    rtt_history = []

    while session.get('active', False):
        try:
            ping_count += 1
            rtt = None
            rssi = None

            # Method 1: L2PING for RTT and connection establishment
            if 'l2ping' in methods:
                try:
                    result = subprocess.run(
                        ['l2ping', '-i', interface, '-c', '1', '-s', '44', '-t', '3', bd_address],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )

                    # Parse l2ping output for RTT
                    if 'time' in result.stdout:
                        rtt_match = re.search(r'time\s+([\d.]+)ms', result.stdout)
                        if rtt_match:
                            rtt = float(rtt_match.group(1))
                            rtt_history.append(rtt)
                            successful_pings += 1
                except subprocess.TimeoutExpired:
                    pass
                except Exception as e:
                    add_log(f"L2PING error: {e}", "DEBUG")

            # Method 2: Direct RSSI query with hcitool
            if 'rssi' in methods:
                # First try to create a connection if needed
                try:
                    # Create ACL connection
                    subprocess.run(
                        ['hcitool', '-i', interface, 'cc', bd_address],
                        capture_output=True, timeout=3
                    )
                    time.sleep(0.2)

                    # Get RSSI
                    rssi_result = subprocess.run(
                        ['hcitool', '-i', interface, 'rssi', bd_address],
                        capture_output=True, text=True, timeout=2
                    )
                    rssi_match = re.search(r'RSSI return value:\s*(-?\d+)', rssi_result.stdout)
                    if rssi_match:
                        rssi = int(rssi_match.group(1))
                        rssi_readings += 1
                except Exception as e:
                    add_log(f"RSSI query error: {e}", "DEBUG")

            # Fallback: Check btmon cache
            if rssi is None:
                rssi = get_btmon_rssi(bd_address)

            # Fallback: bluetoothctl
            if rssi is None:
                rssi = get_rssi_from_bluetoothctl(bd_address)

            # We have an actual response if:
            # - Got RTT from l2ping (device responded to echo request)
            # - Got RSSI from direct query (device is connected)
            got_response = (rtt is not None) or (rssi is not None)

            # Record observation if we have location and RSSI
            if current_location.get('lat') and current_location.get('lon') and rssi:
                conn = get_db()
                c = conn.cursor()
                c.execute('''
                    INSERT INTO rssi_history (bd_address, rssi, system_lat, system_lon)
                    VALUES (?, ?, ?, ?)
                ''', (bd_address, rssi, current_location['lat'], current_location['lon']))
                conn.commit()
                conn.close()

                # If device not in survey yet AND we got an actual response, add it now
                # This is the ONLY place we should add a geo-tracked device to the survey
                if bd_address not in devices and got_response:
                    device_type = get_device_type(bd_address)
                    device_data = {
                        'bd_address': bd_address,
                        'device_name': None,
                        'device_type': device_type if device_type != 'unknown' else 'classic',
                        'manufacturer': get_manufacturer(bd_address),
                        'rssi': rssi
                    }
                    process_found_device(device_data)
                    add_log(f"Device {bd_address} confirmed present (response received)", "INFO")

                # Update RSSI in devices dictionary for survey table
                if bd_address in devices:
                    devices[bd_address]['rssi'] = rssi
                    devices[bd_address]['last_seen'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                # Add reading to direction history for direction finding
                if current_location['lat'] and current_location['lon'] and rssi:
                    try:
                        add_direction_reading(bd_address, current_location['lat'], current_location['lon'], rssi)
                    except Exception as dir_err:
                        add_log(f"Direction history error: {dir_err}", "DEBUG")

                # Calculate direction to target
                direction = None
                try:
                    direction = calculate_direction_to_target(bd_address)
                except Exception as dir_err:
                    add_log(f"Direction calculation error: {dir_err}", "DEBUG")

                # Emit real-time ping data to UI with direction info
                socketio.emit('geo_ping', {
                    'bd_address': bd_address,
                    'rssi': rssi,
                    'rtt': rtt,
                    'ping': ping_count,
                    'rssi_readings': rssi_readings,
                    'success_rate': round(successful_pings / ping_count * 100, 1) if ping_count > 0 else 0,
                    'avg_rtt': round(sum(rtt_history[-10:]) / len(rtt_history[-10:]), 2) if rtt_history else None,
                    'system_lat': current_location['lat'],
                    'system_lon': current_location['lon'],
                    'methods': methods,
                    'direction': direction  # Direction finding data
                })

                add_log(f"GEO [{method_str}] {bd_address}: RSSI={rssi}dBm RTT={rtt}ms", "DEBUG")

                # Recalculate geolocation after sufficient readings
                if rssi_readings >= 2:
                    location = update_device_location(bd_address, rssi)
                    if location and bd_address in devices:
                        devices[bd_address]['emitter_lat'] = location[0]
                        devices[bd_address]['emitter_lon'] = location[1]
                        devices[bd_address]['emitter_accuracy'] = location[2]

                # Always emit device update to refresh survey table with latest RSSI
                if bd_address in devices:
                    socketio.emit('device_update', devices[bd_address])

            else:
                # No RSSI - emit status anyway
                socketio.emit('geo_ping', {
                    'bd_address': bd_address,
                    'rssi': rssi,
                    'rtt': rtt,
                    'ping': ping_count,
                    'rssi_readings': rssi_readings,
                    'success_rate': round(successful_pings / ping_count * 100, 1) if ping_count > 0 else 0,
                    'avg_rtt': round(sum(rtt_history[-10:]) / len(rtt_history[-10:]), 2) if rtt_history else None,
                    'status': 'no_rssi' if not rssi else 'no_gps',
                    'methods': methods
                })

            # Short delay between cycles
            time.sleep(0.5)

        except Exception as e:
            add_log(f"Active geo error for {bd_address}: {e}", "WARNING")
            socketio.emit('geo_ping', {
                'bd_address': bd_address,
                'ping': ping_count,
                'status': 'error',
                'error': str(e)
            })
            time.sleep(1)

        # Re-check session state
        session = active_geo_sessions.get(bd_address, {})

    add_log(f"Stopped active geo tracking for {bd_address} ({successful_pings}/{ping_count} pings)", "INFO")


def start_active_geo(bd_address, interface='hci0', methods=None):
    """Start active geo tracking for a device with selectable methods."""
    global active_geo_sessions

    if methods is None:
        methods = ['l2ping', 'rssi']

    if bd_address in active_geo_sessions and active_geo_sessions[bd_address].get('active'):
        return {'status': 'already_running'}

    active_geo_sessions[bd_address] = {
        'active': True,
        'interface': interface,
        'methods': methods,
        'thread': None
    }

    thread = threading.Thread(
        target=active_geo_track,
        args=(bd_address, interface, methods),
        daemon=True
    )
    active_geo_sessions[bd_address]['thread'] = thread
    thread.start()

    # NOTE: Do NOT add device to survey here - only add when we get an actual response
    # This prevents false target alerts when the device isn't actually present

    return {'status': 'started', 'bd_address': bd_address, 'methods': methods}


def stop_active_geo(bd_address):
    """Stop active geo tracking for a device."""
    global active_geo_sessions

    if bd_address in active_geo_sessions:
        active_geo_sessions[bd_address]['active'] = False
        # Clear direction history when stopping tracking
        clear_direction_history(bd_address)
        return {'status': 'stopped', 'bd_address': bd_address}

    return {'status': 'not_running'}


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

    last_valid_time = 0
    no_fix_reported = False

    while True:
        try:
            loc = get_gps_location()
            if loc and loc.get('lat') and loc['lat'] != 0:
                loc['status'] = 'ok'
                socketio.emit('gps_update', loc)
                last_valid_time = time.time()
                no_fix_reported = False
            else:
                # No valid GPS - report status periodically
                if time.time() - last_valid_time > 5 and not no_fix_reported:
                    socketio.emit('gps_update', {
                        'lat': 0, 'lon': 0,
                        'status': 'no_fix',
                        'source': CONFIG.get('GPS_SOURCE', 'unknown')
                    })
                    no_fix_reported = True
        except Exception as e:
            logger.error(f"GPS error: {e}")
            # Report error status
            socketio.emit('gps_update', {
                'lat': 0, 'lon': 0,
                'status': 'error',
                'error': str(e)
            })
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

    # Enrich with btmon cached data (device type, company, name, etc.)
    btmon_data = get_btmon_device_info(bd_addr)
    if btmon_data:
        # Enrich device_type if we don't have a definitive one
        if device_info.get('device_type') in ['unknown', None] and btmon_data.get('device_type'):
            device_info['device_type'] = btmon_data['device_type']

        # Enrich device name if we don't have a good one
        current_name = device_info.get('device_name', '')
        if (not current_name or current_name in ['Unknown', 'BLE Device', '']) and btmon_data.get('device_name'):
            device_info['device_name'] = btmon_data['device_name']

        # Add company/manufacturer info from btmon
        if btmon_data.get('company_name'):
            device_info['bt_company'] = btmon_data['company_name']

        # Add TX power if available
        if btmon_data.get('tx_power') is not None:
            device_info['tx_power'] = btmon_data['tx_power']

        # Add address type (public/random)
        if btmon_data.get('addr_type'):
            device_info['addr_type'] = btmon_data['addr_type']

    # Check if it's a new device or update
    is_new = bd_addr not in devices

    # Update in-memory cache
    if bd_addr in devices:
        devices[bd_addr].update(device_info)
        devices[bd_addr]['last_seen'] = now_str
        # Increment packet count
        devices[bd_addr]['packet_count'] = devices[bd_addr].get('packet_count', 0) + 1
    else:
        device_info['first_seen'] = now_str
        device_info['last_seen'] = now_str
        device_info['packet_count'] = 1  # First packet
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


def scan_single_adapter(interface):
    """Scan using a single Bluetooth adapter. Thread-safe."""
    try:
        all_devices = scan_classic_bluetooth(interface)
        return all_devices
    except Exception as e:
        add_log(f"Scan error on {interface}: {str(e)}", "ERROR")
        return []


def scan_loop():
    """Main scanning loop with parallel multi-adapter support."""
    global scanning_active, active_radios

    add_log("Scanning started", "INFO")

    while scanning_active:
        try:
            # Get active Bluetooth interfaces (default to both hci0 and hci1 if available)
            bt_interfaces = active_radios.get('bluetooth', [])
            if not bt_interfaces:
                # Default: try to use both hci0 (UD100) and hci1 (AX210) in parallel
                bt_interfaces = []
                for iface in ['hci0', 'hci1']:
                    try:
                        result = subprocess.run(['hciconfig', iface], capture_output=True, text=True, timeout=2)
                        if 'UP RUNNING' in result.stdout:
                            bt_interfaces.append(iface)
                    except:
                        pass
                if not bt_interfaces:
                    bt_interfaces = ['hci0']  # Final fallback

            if len(bt_interfaces) > 1:
                # Parallel scanning with multiple adapters
                from concurrent.futures import ThreadPoolExecutor, as_completed

                all_devices = []
                with ThreadPoolExecutor(max_workers=len(bt_interfaces)) as executor:
                    futures = {executor.submit(scan_single_adapter, iface): iface for iface in bt_interfaces}
                    for future in as_completed(futures, timeout=30):
                        iface = futures[future]
                        try:
                            devices_from_adapter = future.result()
                            if devices_from_adapter:
                                for dev in devices_from_adapter:
                                    dev['adapter'] = iface  # Track which adapter found it
                                all_devices.extend(devices_from_adapter)
                        except Exception as e:
                            add_log(f"Parallel scan error on {iface}: {e}", "WARNING")

                # Process all found devices
                for dev in all_devices:
                    if not scanning_active:
                        break
                    process_found_device(dev)
            else:
                # Single adapter mode
                for iface in bt_interfaces:
                    if not scanning_active:
                        break

                    # Unified scan - handles both Classic and BLE
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
    """Get list of available Bluetooth, WiFi, and Ubertooth radios."""
    radios = {'bluetooth': [], 'wifi': [], 'ubertooth': []}

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

    # Ubertooth devices
    try:
        if check_ubertooth_available():
            # Try to get device info
            result = subprocess.run(['ubertooth-util', '-v'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Parse firmware version from output
                version_info = result.stdout.strip() if result.stdout else 'Unknown'
                radios['ubertooth'].append({
                    'interface': 'ubertooth0',
                    'version': version_info,
                    'status': 'running' if ubertooth_running else 'ready',
                    'type': 'ubertooth'
                })
    except Exception as e:
        logger.debug(f"Ubertooth not available: {e}")

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


# ==================== UBERTOOTH FUNCTIONS ====================

def check_ubertooth_available():
    """Check if Ubertooth tools are available on the system."""
    try:
        result = subprocess.run(['which', 'ubertooth-rx'], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def get_ubertooth_info():
    """Get Ubertooth device info."""
    try:
        result = subprocess.run(['ubertooth-util', '-v'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            # Parse version info
            return {
                'available': True,
                'version': result.stdout.strip() if result.stdout else 'Unknown',
                'status': 'ready' if not ubertooth_running else 'running'
            }
    except FileNotFoundError:
        return {'available': False, 'error': 'Ubertooth tools not installed'}
    except subprocess.TimeoutExpired:
        return {'available': False, 'error': 'Ubertooth device not responding'}
    except Exception as e:
        return {'available': False, 'error': str(e)}
    return {'available': False, 'error': 'Unknown error'}


def parse_ubertooth_output(line):
    """Parse a line of ubertooth-rx output for piconet information."""
    global ubertooth_data

    # ubertooth-rx outputs lines like:
    # systime=1234567890 ch=39 LAP=abcdef err=0 clk6=12 clk=0x12345678
    # or with UAP recovery:
    # systime=1234567890 ch=39 LAP=abcdef UAP=12 err=0 clk6=12 clk=0x12345678

    try:
        if 'LAP=' in line:
            parts = {}
            for part in line.split():
                if '=' in part:
                    key, value = part.split('=', 1)
                    parts[key] = value

            lap = parts.get('LAP', '').upper()
            if lap and len(lap) == 6:
                channel = parts.get('ch', '?')
                uap = parts.get('UAP', None)
                clk = parts.get('clk', None)
                clk6 = parts.get('clk6', None)

                # Store piconet data
                if lap not in ubertooth_data:
                    ubertooth_data[lap] = {
                        'lap': lap,
                        'first_seen': datetime.now().isoformat(),
                        'channels': set(),
                        'packet_count': 0
                    }

                ubertooth_data[lap]['last_seen'] = datetime.now().isoformat()
                ubertooth_data[lap]['channels'].add(int(channel) if channel.isdigit() else channel)
                ubertooth_data[lap]['packet_count'] += 1

                if uap:
                    ubertooth_data[lap]['uap'] = uap.upper()
                    # Construct BD_ADDR format: UAP:LAP -> XX:XX:XX:YY:YY:YY
                    # LAP is lower 3 bytes, UAP is next byte above
                    bd_partial = f"??:{uap}:{lap[0:2]}:{lap[2:4]}:{lap[4:6]}"
                    ubertooth_data[lap]['bd_partial'] = bd_partial

                if clk:
                    ubertooth_data[lap]['clock'] = clk

                return ubertooth_data[lap]
    except Exception as e:
        logger.error(f"Error parsing ubertooth output: {e}")
    return None


def ubertooth_scanner_thread():
    """Background thread to run ubertooth-rx and capture piconet data."""
    global ubertooth_process, ubertooth_running, ubertooth_data

    add_log("Ubertooth scanner starting", "INFO")

    try:
        # Run ubertooth-rx to capture Bluetooth packets
        # -r captures raw packets, -q prints LAP/UAP info
        ubertooth_process = subprocess.Popen(
            ['ubertooth-rx', '-q'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        while ubertooth_running and ubertooth_process.poll() is None:
            line = ubertooth_process.stdout.readline()
            if line:
                piconet_info = parse_ubertooth_output(line.strip())
                if piconet_info:
                    # Emit update to clients
                    socketio.emit('ubertooth_update', {
                        'lap': piconet_info['lap'],
                        'uap': piconet_info.get('uap'),
                        'bd_partial': piconet_info.get('bd_partial'),
                        'channels': list(piconet_info['channels']),
                        'packet_count': piconet_info['packet_count'],
                        'first_seen': piconet_info['first_seen'],
                        'last_seen': piconet_info['last_seen']
                    })

    except FileNotFoundError:
        add_log("Ubertooth tools not found - install ubertooth package", "ERROR")
    except Exception as e:
        add_log(f"Ubertooth scanner error: {e}", "ERROR")
    finally:
        if ubertooth_process:
            ubertooth_process.terminate()
            try:
                ubertooth_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                ubertooth_process.kill()
        ubertooth_process = None
        ubertooth_running = False
        add_log("Ubertooth scanner stopped", "INFO")


def start_ubertooth():
    """Start the Ubertooth scanner."""
    global ubertooth_thread, ubertooth_running, ubertooth_data

    if ubertooth_running:
        return False, "Ubertooth already running"

    if not check_ubertooth_available():
        return False, "Ubertooth tools not available"

    # Clear previous data
    ubertooth_data = {}
    ubertooth_running = True

    ubertooth_thread = threading.Thread(target=ubertooth_scanner_thread, daemon=True)
    ubertooth_thread.start()

    return True, "Ubertooth scanner started"


def stop_ubertooth():
    """Stop the Ubertooth scanner."""
    global ubertooth_running, ubertooth_process

    if not ubertooth_running:
        return False, "Ubertooth not running"

    ubertooth_running = False

    if ubertooth_process:
        ubertooth_process.terminate()

    return True, "Ubertooth scanner stopping"


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


@app.route('/api/version')
def get_version():
    """Get application version from git."""
    version_info = {
        'version': 'v3.2.3',
        'commit': None,
        'branch': None,
        'session_id': SESSION_ID  # Used by frontend to detect restarts
    }

    try:
        # Try to get git commit info
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=2,
            cwd=INSTALL_DIR
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
            version_info['commit'] = commit
            version_info['version'] = f'v3.2.3-{commit}'

        # Get branch name
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, timeout=2,
            cwd=INSTALL_DIR
        )
        if result.returncode == 0:
            version_info['branch'] = result.stdout.strip()

    except Exception:
        pass

    return jsonify(version_info)


@app.route('/api/updates/check', methods=['GET'])
@login_required
def check_for_updates():
    """Check if updates are available from GitHub for the current branch."""
    update_info = {
        'current_commit': None,
        'remote_commit': None,
        'update_available': False,
        'commits_behind': 0,
        'recent_changes': [],
        'current_branch': None,
        'error': None
    }

    try:
        # Get current branch
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
        )
        current_branch = result.stdout.strip() if result.returncode == 0 else 'main'
        update_info['current_branch'] = current_branch

        # Get current commit
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
        )
        if result.returncode == 0:
            update_info['current_commit'] = result.stdout.strip()

        # Fetch from remote (fetch all branches)
        subprocess.run(
            ['git', 'fetch', 'origin', '--prune'],
            capture_output=True, text=True, timeout=30, cwd=INSTALL_DIR
        )

        # Get remote commit for the current branch
        remote_ref = f'origin/{current_branch}'
        result = subprocess.run(
            ['git', 'rev-parse', '--short', remote_ref],
            capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
        )
        if result.returncode != 0:
            # If current branch doesn't exist on remote, try main/master
            for fallback in ['origin/main', 'origin/master']:
                result = subprocess.run(
                    ['git', 'rev-parse', '--short', fallback],
                    capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
                )
                if result.returncode == 0:
                    remote_ref = fallback
                    break

        if result.returncode == 0:
            update_info['remote_commit'] = result.stdout.strip()

        # Check if different
        if update_info['current_commit'] and update_info['remote_commit']:
            update_info['update_available'] = update_info['current_commit'] != update_info['remote_commit']

            # Count commits behind
            result = subprocess.run(
                ['git', 'rev-list', '--count', f'HEAD..{remote_ref}'],
                capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
            )
            if result.returncode == 0:
                try:
                    update_info['commits_behind'] = int(result.stdout.strip())
                except ValueError:
                    update_info['commits_behind'] = 0

            # Get recent changes on remote
            if update_info['update_available'] and update_info['commits_behind'] > 0:
                result = subprocess.run(
                    ['git', 'log', '--oneline', '-10', f'HEAD..{remote_ref}'],
                    capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
                )
                if result.returncode == 0 and result.stdout.strip():
                    update_info['recent_changes'] = result.stdout.strip().split('\n')

        add_log(f"Update check: {current_branch} @ {update_info['current_commit']} -> {update_info['remote_commit']} ({update_info['commits_behind']} behind)", "DEBUG")

    except subprocess.TimeoutExpired:
        update_info['error'] = 'Timeout checking for updates'
    except Exception as e:
        update_info['error'] = str(e)

    return jsonify(update_info)


@app.route('/api/updates/apply', methods=['POST'])
@login_required
def apply_updates():
    """Apply available updates from GitHub and automatically restart."""
    result = {
        'success': False,
        'old_commit': None,
        'new_commit': None,
        'current_branch': None,
        'changes': [],
        'error': None,
        'restart_required': True,
        'auto_restart': True
    }

    try:
        # Get current branch
        proc = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
        )
        current_branch = proc.stdout.strip() if proc.returncode == 0 else 'main'
        result['current_branch'] = current_branch

        # Get current commit
        proc = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
        )
        result['old_commit'] = proc.stdout.strip() if proc.returncode == 0 else None

        # Stash any local changes
        subprocess.run(['git', 'stash'], capture_output=True, timeout=10, cwd=INSTALL_DIR)

        # Pull updates from the current branch
        add_log(f"Pulling updates for branch: {current_branch}", "INFO")
        proc = subprocess.run(
            ['git', 'pull', 'origin', current_branch],
            capture_output=True, text=True, timeout=60, cwd=INSTALL_DIR
        )
        if proc.returncode != 0:
            # Try main as fallback
            proc = subprocess.run(
                ['git', 'pull', 'origin', 'main'],
                capture_output=True, text=True, timeout=60, cwd=INSTALL_DIR
            )
        if proc.returncode != 0:
            # Try master as last fallback
            proc = subprocess.run(
                ['git', 'pull', 'origin', 'master'],
                capture_output=True, text=True, timeout=60, cwd=INSTALL_DIR
            )

        if proc.returncode == 0:
            result['success'] = True

            # Get new commit
            proc = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
            )
            result['new_commit'] = proc.stdout.strip() if proc.returncode == 0 else None

            # Get changes made
            if result['old_commit'] and result['new_commit']:
                proc = subprocess.run(
                    ['git', 'log', '--oneline', f"{result['old_commit']}..{result['new_commit']}"],
                    capture_output=True, text=True, timeout=5, cwd=INSTALL_DIR
                )
                if proc.returncode == 0:
                    result['changes'] = proc.stdout.strip().split('\n') if proc.stdout.strip() else []

            add_log(f"Updates applied: {result['old_commit']} -> {result['new_commit']}", "INFO")

            # Notify all clients that system is restarting
            socketio.emit('system_restart')

            # Schedule automatic restart after response is sent
            def delayed_restart():
                time.sleep(2)  # Give time for response to be sent
                do_system_restart()

            restart_thread = threading.Thread(target=delayed_restart, daemon=True)
            restart_thread.start()

        else:
            result['error'] = proc.stderr or 'Failed to pull updates'
            add_log(f"Update failed: {result['error']}", "ERROR")

    except subprocess.TimeoutExpired:
        result['error'] = 'Timeout applying updates'
    except Exception as e:
        result['error'] = str(e)

    return jsonify(result)


def do_system_restart():
    """Execute the actual system restart using flag-based approach with start.sh loop."""
    global scanning_active, warhammer_running

    add_log("Initiating system restart...", "INFO")

    # Stop any active scanning
    scanning_active = False

    # Stop WARHAMMER monitoring
    warhammer_running = False

    # Stop any active geo tracking sessions
    for bd_addr in list(active_geo_sessions.keys()):
        stop_active_geo(bd_addr)

    # Create restart flag file - start.sh will detect this and restart
    restart_flag = '/tmp/bluek9_restart'
    try:
        with open(restart_flag, 'w') as f:
            f.write(str(time.time()))
        add_log("Restart flag created, exiting for restart...", "INFO")
    except Exception as e:
        add_log(f"Failed to create restart flag: {e}", "ERROR")

    # Give a moment for the log to be sent
    time.sleep(0.5)

    # Exit the application - start.sh loop will restart it
    os._exit(0)


@app.route('/api/system/restart', methods=['POST'])
@login_required
def restart_system():
    """Restart the BlueK9 application via start.sh script."""
    try:
        add_log("System restart initiated by operator", "INFO")

        # Notify all clients to reset their UI
        socketio.emit('system_restart')

        # Schedule restart in background thread
        def delayed_restart():
            time.sleep(2)  # Brief delay to allow response to be sent
            do_system_restart()

        restart_thread = threading.Thread(target=delayed_restart, daemon=False)
        restart_thread.start()

        return jsonify({'status': 'restarting'})

    except Exception as e:
        add_log(f"Restart failed: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500


@app.route('/api/state')
@login_required
def get_system_state():
    """Get current system operation state for UI sync on reconnect."""
    # Get active geo sessions
    geo_sessions = []
    for bd_addr, session in active_geo_sessions.items():
        if session.get('active'):
            geo_sessions.append({
                'bd_address': bd_addr,
                'interface': session.get('interface', 'hci0'),
                'methods': session.get('methods', ['l2ping', 'rssi'])
            })

    return jsonify({
        'scanning': scanning_active,
        'active_geo_sessions': geo_sessions,
        'device_count': len(devices),
        'target_count': len(targets)
    })


@app.route('/api/scan/start', methods=['POST'])
@login_required
def start_scan():
    """Start scanning."""
    global scanning_active, scan_thread

    if not scanning_active:
        scanning_active = True

        # Optimize HCI parameters on all available adapters for maximum device detection
        for iface in ['hci0', 'hci1']:
            try:
                result = subprocess.run(['hciconfig', iface], capture_output=True, text=True, timeout=2)
                if 'UP RUNNING' in result.stdout:
                    set_hci_scan_parameters(iface, inquiry_mode='extended', page_scan_type='interlaced')
                    add_log(f"Optimized {iface} for extended inquiry mode", "INFO")
            except:
                pass

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
    """Stop all active scanning operations."""
    global scanning_active, advanced_scan_active, hidden_scan_active, target_survey_active

    stopped = []

    # Stop regular scanning
    if scanning_active:
        scanning_active = False
        stopped.append('scan')

    # Stop btmon
    stop_btmon()

    # Stop advanced scan if running
    if advanced_scan_active:
        stop_advanced_scan()
        stopped.append('advanced')

    # Stop hidden device hunt if running
    if hidden_scan_active:
        stop_hidden_device_hunt()
        stopped.append('hidden')

    # Stop target survey if running
    if target_survey_active:
        stop_target_survey()
        stopped.append('target_survey')

    add_log(f"Scan stop requested. Stopped: {', '.join(stopped) if stopped else 'none active'}", "INFO")
    return jsonify({'status': 'stopped', 'stopped': stopped})


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


@app.route('/api/scan/aggressive', methods=['POST'])
@login_required
def aggressive_scan_endpoint():
    """
    Run aggressive inquiry scan using multiple LAP codes and interlaced scanning.
    Faster than full advanced scan but more thorough than quick scan.

    POST body:
    {
        "interface": "hci0",  // HCI interface (default: hci0)
        "duration": 20        // Scan duration in seconds (default: 20)
    }
    """
    data = request.json or {}
    interface = data.get('interface', 'hci0')
    duration = data.get('duration', 20)

    try:
        add_log(f"Starting aggressive inquiry via API ({duration}s)", "INFO")

        # Configure optimal HCI parameters
        set_hci_scan_parameters(interface, inquiry_mode='extended', page_scan_type='interlaced')

        # Run aggressive inquiry
        devices_found = aggressive_inquiry(interface, duration)

        # Process found devices
        for dev in devices_found:
            process_found_device(dev)

        add_log(f"Aggressive inquiry complete: {len(devices_found)} devices", "INFO")
        return jsonify({
            'status': 'completed',
            'devices_found': len(devices_found),
            'interface': interface,
            'duration': duration
        })
    except Exception as e:
        add_log(f"Aggressive inquiry error: {e}", "ERROR")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/scan/advanced', methods=['POST'])
@login_required
def advanced_scan():
    """
    Run advanced Classic Bluetooth scan using all available techniques.

    POST body:
    {
        "interface": "hci0",     // HCI interface (default: hci0)
        "duration": 30,          // Scan duration in seconds (default: 30)
        "aggressive": true       // Enable aggressive techniques (default: true)
    }
    """
    data = request.json or {}
    interface = data.get('interface', 'hci0')
    duration = data.get('duration', 30)
    aggressive = data.get('aggressive', True)

    success = start_advanced_scan(interface, duration, aggressive)
    if success:
        return jsonify({
            'status': 'started',
            'interface': interface,
            'duration': duration,
            'aggressive': aggressive,
            'message': 'Advanced scan running in background'
        })
    else:
        return jsonify({
            'status': 'error',
            'message': 'Advanced scan already running'
        }), 409


@app.route('/api/scan/advanced/stop', methods=['POST'])
@login_required
def stop_advanced():
    """Stop the running advanced scan."""
    stop_advanced_scan()
    return jsonify({'status': 'stopped'})


@app.route('/api/scan/hidden', methods=['POST'])
@login_required
def hidden_scan_endpoint():
    """
    Run hidden device hunt specifically targeting phones and smartwatches.

    This scan uses multiple techniques to detect non-discoverable devices:
    - Extended BLE scanning (most modern phones use BLE)
    - Aggressive Classic inquiry with multiple LAP codes
    - L2ping probing of known phone/watch manufacturer OUIs
    - Page scan optimization to catch paired devices

    POST body:
    {
        "interface": "hci0",  // HCI interface (default: hci0)
        "duration": 45        // Scan duration in seconds (default: 45)
    }
    """
    data = request.json or {}
    interface = data.get('interface', 'hci0')
    duration = data.get('duration', 45)

    success = start_hidden_device_hunt(interface, duration)
    if success:
        return jsonify({
            'status': 'started',
            'interface': interface,
            'duration': duration,
            'message': 'Hidden device hunt running in background - targeting phones/watches'
        })
    else:
        return jsonify({
            'status': 'error',
            'message': 'Hidden device hunt already running'
        }), 409


@app.route('/api/scan/hidden/stop', methods=['POST'])
@login_required
def stop_hidden():
    """Stop the running hidden device hunt."""
    stop_hidden_device_hunt()
    return jsonify({'status': 'stopped'})


@app.route('/api/scan/hidden/status', methods=['GET'])
@login_required
def hidden_scan_status():
    """Get status of hidden device hunt."""
    return jsonify({
        'active': hidden_scan_active
    })


@app.route('/api/scan/target_survey', methods=['POST'])
@login_required
def target_survey_endpoint():
    """
    Run target survey - actively probe all targets to determine presence.

    This scan uses multiple techniques against each target BD address:
    - L2CAP ping (l2ping)
    - Name request
    - SDP service probe
    - RSSI measurement

    POST body:
    {
        "interface": "hci0",     // HCI interface (default: hci0)
        "continuous": true,      // Run continuously (default: false)
        "interval": 30           // Seconds between sweeps in continuous mode (default: 30, min: 10)
    }
    """
    data = request.json or {}
    interface = data.get('interface', 'hci0')
    continuous = data.get('continuous', False)
    interval = data.get('interval', 30)

    # Get target count first
    db_targets = get_targets_from_db()
    if not db_targets:
        return jsonify({
            'status': 'no_targets',
            'message': 'No targets defined. Add targets first.'
        })

    success = start_target_survey(interface, continuous=continuous, interval=interval)
    if success:
        mode_str = "continuous" if continuous else "single sweep"
        return jsonify({
            'status': 'started',
            'target_count': len(db_targets),
            'continuous': continuous,
            'interval': max(10, interval) if continuous else None,
            'message': f'Probing {len(db_targets)} target(s) ({mode_str})...'
        })
    else:
        return jsonify({
            'status': 'already_running',
            'message': 'Target survey already in progress'
        })


@app.route('/api/scan/target_survey/stop', methods=['POST'])
@login_required
def stop_target_survey_endpoint():
    """Stop the running target survey."""
    stop_target_survey()
    return jsonify({'status': 'stopped'})


@app.route('/api/scan/target_survey/status', methods=['GET'])
@login_required
def target_survey_status():
    """Get status of target survey."""
    found_count = sum(1 for r in target_survey_results.values() if r.get('present'))
    return jsonify({
        'active': target_survey_active,
        'continuous': target_survey_continuous,
        'interval': target_survey_interval,
        'sweep_count': target_survey_sweep_count,
        'results_count': len(target_survey_results),
        'found_count': found_count,
        'results': list(target_survey_results.values()) if target_survey_results else []
    })


@app.route('/api/scan/advanced/status', methods=['GET'])
@login_required
def advanced_scan_status():
    """Get status of advanced scan."""
    return jsonify({
        'active': advanced_scan_active,
        'sdp_cache_size': len(sdp_probe_cache),
        'sweep_results': len(address_sweep_results)
    })


@app.route('/api/device/<bd_address>/deep_scan', methods=['POST'])
@login_required
def api_deep_scan(bd_address):
    """
    Deep scan a specific BD address using all available techniques.
    Use for targeted scanning of suspected devices.
    """
    data = request.json or {}
    interface = data.get('interface', 'hci0')

    result = deep_scan_device(bd_address, interface)

    # If device confirmed, add to survey
    if result['confirmed']:
        device_data = {
            'bd_address': bd_address,
            'device_name': result.get('device_name'),
            'device_type': 'classic',
            'manufacturer': result['manufacturer'],
            'rssi': result.get('rssi'),
            'services': result.get('services', [])
        }
        process_found_device(device_data)

    return jsonify(result)


@app.route('/api/device/<bd_address>/sdp', methods=['GET'])
@login_required
def api_sdp_probe(bd_address):
    """
    Probe a device for SDP services.
    Returns list of available services and protocols.
    """
    interface = request.args.get('interface', 'hci0')
    result = sdp_probe(bd_address, interface)
    return jsonify(result)


@app.route('/api/device/<bd_address>/l2ping', methods=['POST'])
@login_required
def api_l2ping(bd_address):
    """
    L2CAP ping a device to check presence.
    """
    data = request.json or {}
    interface = data.get('interface', 'hci0')

    result = l2cap_ping_sweep(bd_address, interface)
    return jsonify({
        'bd_address': bd_address,
        'responded': result['responded'],
        'psms': result['psms']
    })


@app.route('/api/scan/sweep', methods=['POST'])
@login_required
def api_address_sweep():
    """
    Sweep a range of BD addresses based on OUI prefix.

    POST body:
    {
        "oui": "D0:03:4B",       // OUI prefix (first 3 bytes)
        "interface": "hci0",
        "range_size": 16         // How many addresses to sweep
    }
    """
    data = request.json or {}
    oui = data.get('oui')
    interface = data.get('interface', 'hci0')
    range_size = data.get('range_size', 16)

    if not oui:
        return jsonify({'error': 'OUI prefix required'}), 400

    # Get known addresses for smarter sweep
    known = set(devices.keys())

    results = address_sweep(oui, interface, range_size, known)

    for dev in results:
        process_found_device(dev)

    return jsonify({
        'status': 'completed',
        'oui': oui,
        'found': len(results),
        'devices': results
    })


@app.route('/api/radio/<interface>/optimize', methods=['POST'])
@login_required
def api_optimize_radio(interface):
    """
    Optimize HCI parameters for maximum device detection.

    POST body:
    {
        "inquiry_mode": "extended",   // standard, rssi, or extended
        "page_scan_type": "interlaced"  // standard or interlaced
    }
    """
    data = request.json or {}
    inquiry_mode = data.get('inquiry_mode', 'extended')
    page_scan_type = data.get('page_scan_type', 'interlaced')

    success = set_hci_scan_parameters(interface, inquiry_mode, page_scan_type)

    if success:
        return jsonify({
            'status': 'optimized',
            'interface': interface,
            'inquiry_mode': inquiry_mode,
            'page_scan_type': page_scan_type
        })
    else:
        return jsonify({
            'status': 'error',
            'message': 'Failed to optimize radio parameters'
        }), 500


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


@app.route('/api/device/<bd_address>/geo/track', methods=['POST'])
@login_required
def start_device_geo_track(bd_address):
    """Start active geo tracking with selectable methods."""
    bd_address = bd_address.upper()
    data = request.get_json(silent=True) or {}
    interface = data.get('interface', 'hci0')
    methods = data.get('methods', ['l2ping', 'rssi'])

    # Check device type to determine compatible methods
    device_type = get_device_type(bd_address)

    # BLE devices don't support l2ping - it uses L2CAP which is Classic-only
    if device_type == 'ble':
        # Filter out l2ping for BLE devices
        original_methods = methods.copy()
        methods = [m for m in methods if m != 'l2ping']
        if 'l2ping' in original_methods:
            add_log(f"l2ping not supported for BLE device {bd_address} - using RSSI only", "WARNING")
            socketio.emit('log_update', {
                'message': f"Note: l2ping not available for BLE devices. Using RSSI-based tracking for {bd_address}",
                'level': 'WARNING'
            })
        if not methods:
            methods = ['rssi']  # Fallback to RSSI-only for BLE

    # Validate methods
    valid_methods = ['l2ping', 'rssi']
    methods = [m for m in methods if m in valid_methods]
    if not methods:
        methods = ['l2ping', 'rssi']

    result = start_active_geo(bd_address, interface, methods)
    add_log(f"Active geo tracking started for {bd_address} ({'+'.join(methods)})", "INFO")
    return jsonify(result)


@app.route('/api/device/<bd_address>/geo/stop', methods=['POST'])
@login_required
def stop_device_geo_track(bd_address):
    """Stop active geo tracking for a device."""
    bd_address = bd_address.upper()

    result = stop_active_geo(bd_address)
    return jsonify(result)


@app.route('/api/device/<bd_address>/direction')
@login_required
def get_device_direction(bd_address):
    """Get direction finding data for a device being tracked."""
    bd_address = bd_address.upper()

    direction = calculate_direction_to_target(bd_address)
    if direction:
        return jsonify(direction)
    else:
        return jsonify({
            'bearing': None,
            'confidence': 0,
            'trend': 'unknown',
            'message': 'Not enough data - start tracking and move around'
        })


@app.route('/api/device/<bd_address>/type', methods=['POST'])
@login_required
def set_device_type(bd_address):
    """Manually set device type (classic/ble) for a device."""
    bd_address = bd_address.upper()
    data = request.get_json() or {}
    new_type = data.get('device_type', '').lower()

    if new_type not in ['classic', 'ble', 'unknown']:
        return jsonify({'error': 'Invalid device type. Must be: classic, ble, or unknown'}), 400

    # Update in-memory cache
    if bd_address in devices:
        devices[bd_address]['device_type'] = new_type
        devices[bd_address]['type_manual'] = True  # Flag as manually set
        socketio.emit('device_update', devices[bd_address])
        add_log(f"Device type manually set for {bd_address}: {new_type}", "INFO")

        # Update in database
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('UPDATE devices SET device_type = ? WHERE bd_address = ?',
                      (new_type, bd_address))
            conn.commit()
        except Exception as e:
            add_log(f"Failed to update device type in DB: {e}", "WARNING")

        return jsonify({'status': 'updated', 'device_type': new_type})

    return jsonify({'error': 'Device not found'}), 404


@app.route('/api/geo/active', methods=['GET'])
@login_required
def get_active_geo_sessions():
    """Get list of active geo tracking sessions."""
    sessions = []
    for bd_addr, session in active_geo_sessions.items():
        if session.get('active'):
            sessions.append({
                'bd_address': bd_addr,
                'interface': session.get('interface', 'hci0')
            })
    return jsonify(sessions)


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

    # Notify clients to clear geo visuals
    socketio.emit('data_cleared', {'type': 'geo'})

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
    """Clear all breadcrumb/heatmap data."""
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM rssi_history')
    deleted = c.rowcount
    conn.commit()
    conn.close()

    add_log(f"Breadcrumbs reset: {deleted} points cleared", "INFO")

    # Notify clients to clear heatmap visuals
    socketio.emit('data_cleared', {'type': 'heatmap'})

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
    """Clear system trail data."""
    # System trail is derived from rssi_history, but we only clear the trail visualization
    # The actual data is shared with breadcrumbs, so we notify clients to clear trail only
    socketio.emit('data_cleared', {'type': 'trail'})
    add_log("System trail reset", "INFO")
    return jsonify({'status': 'reset'})


# ==================== ANALYSIS TOOLS API ====================

@app.route('/api/tools/sdp/<bd_address>')
@login_required
def sdp_browse(bd_address):
    """Run SDP service discovery on a Bluetooth device."""
    bd_address = bd_address.upper()
    add_log(f"SDP browse requested for {bd_address}", "INFO")

    try:
        # Use sdptool browse to get services
        result = subprocess.run(
            ['sdptool', 'browse', bd_address],
            capture_output=True,
            text=True,
            timeout=30
        )

        services = []
        current_service = {}

        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('Service Name:'):
                if current_service.get('name'):
                    services.append(current_service)
                current_service = {'name': line.split(':', 1)[1].strip()}
            elif line.startswith('Protocol Descriptor List:'):
                current_service['protocol'] = 'Multiple'
            elif '"RFCOMM"' in line or 'RFCOMM' in line:
                current_service['protocol'] = 'RFCOMM'
            elif '"L2CAP"' in line or 'L2CAP' in line:
                if current_service.get('protocol') != 'RFCOMM':
                    current_service['protocol'] = 'L2CAP'
            elif line.startswith('Channel:'):
                current_service['channel'] = line.split(':')[1].strip()
            elif line.startswith('Service RecHandle:'):
                current_service['handle'] = line.split(':')[1].strip()
            elif 'UUID' in line and ':' in line:
                uuid_part = line.split(':')[-1].strip()
                if len(uuid_part) > 4:
                    current_service['uuid'] = uuid_part

        if current_service.get('name'):
            services.append(current_service)

        if services:
            add_log(f"SDP found {len(services)} services on {bd_address}", "INFO")
            return jsonify({'status': 'success', 'services': services})
        elif result.returncode != 0:
            error_msg = result.stderr or 'Device not responding'
            add_log(f"SDP browse failed for {bd_address}: {error_msg}", "WARNING")
            return jsonify({'status': 'error', 'error': error_msg})
        else:
            return jsonify({'status': 'success', 'services': []})

    except subprocess.TimeoutExpired:
        add_log(f"SDP browse timeout for {bd_address}", "WARNING")
        return jsonify({'status': 'error', 'error': 'Connection timeout'})
    except Exception as e:
        add_log(f"SDP browse error for {bd_address}: {e}", "ERROR")
        return jsonify({'status': 'error', 'error': str(e)})


@app.route('/api/tools/pbap/<bd_address>/<book_type>')
@login_required
def pbap_read(bd_address, book_type):
    """
    Attempt to read phone book from a Bluetooth device using PBAP.
    Note: This requires the device to be paired and PBAP service accessible.
    """
    bd_address = bd_address.upper()
    add_log(f"PBAP read requested: {book_type} from {bd_address}", "INFO")

    # Valid phone book types
    valid_books = ['pb', 'ich', 'och', 'mch', 'cch']
    if book_type not in valid_books:
        return jsonify({'status': 'error', 'error': f'Invalid book type. Use: {", ".join(valid_books)}'})

    try:
        # First check if device has PBAP support via SDP
        sdp_result = subprocess.run(
            ['sdptool', 'search', '--bdaddr', bd_address, 'PBAP'],
            capture_output=True,
            text=True,
            timeout=15
        )

        # Check for PBAP service
        if 'PBAP' not in sdp_result.stdout and 'PhoneBook' not in sdp_result.stdout:
            # Try OBEX Push as fallback check
            obex_result = subprocess.run(
                ['sdptool', 'search', '--bdaddr', bd_address, 'OPUSH'],
                capture_output=True,
                text=True,
                timeout=15
            )

            if 'OBEX' not in obex_result.stdout:
                add_log(f"PBAP: No phone book service found on {bd_address}", "WARNING")
                return jsonify({
                    'status': 'error',
                    'error': 'Device does not support Phone Book Access Profile (PBAP). Device may need to be paired first.'
                })

        # Try to use obexftp or similar to access phone book
        # Note: This requires proper pairing and authorization
        # Most phones require explicit user approval for phonebook access

        # Use bluez-tools pbap client if available
        try:
            # Try with bt-obex
            pbap_cmd = ['bt-obex', '-p', bd_address, '-d', f'/telecom/{book_type}.vcf']
            pbap_result = subprocess.run(
                pbap_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if pbap_result.returncode == 0 and pbap_result.stdout:
                # Parse vCard format
                entries = parse_vcard_entries(pbap_result.stdout)
                add_log(f"PBAP: Retrieved {len(entries)} entries from {bd_address}", "INFO")
                return jsonify({'status': 'success', 'entries': entries})

        except FileNotFoundError:
            pass  # bt-obex not available

        # Return informational error
        return jsonify({
            'status': 'error',
            'error': 'PBAP access requires device pairing and user authorization. The target device must approve the connection.'
        })

    except subprocess.TimeoutExpired:
        add_log(f"PBAP timeout for {bd_address}", "WARNING")
        return jsonify({'status': 'error', 'error': 'Connection timeout'})
    except Exception as e:
        add_log(f"PBAP error for {bd_address}: {e}", "ERROR")
        return jsonify({'status': 'error', 'error': str(e)})


def parse_vcard_entries(vcard_data):
    """Parse vCard format data into list of name/number entries."""
    entries = []
    current_entry = {}

    for line in vcard_data.split('\n'):
        line = line.strip()
        if line.startswith('BEGIN:VCARD'):
            current_entry = {}
        elif line.startswith('END:VCARD'):
            if current_entry.get('name') or current_entry.get('number'):
                entries.append(current_entry)
            current_entry = {}
        elif line.startswith('FN:'):
            current_entry['name'] = line[3:]
        elif line.startswith('N:'):
            if not current_entry.get('name'):
                parts = line[2:].split(';')
                current_entry['name'] = ' '.join(reversed([p for p in parts if p]))
        elif line.startswith('TEL'):
            # Handle TEL;TYPE=CELL: or TEL: formats
            if ':' in line:
                current_entry['number'] = line.split(':', 1)[1]

    return entries


@app.route('/api/tools/analyze/<bd_address>')
@login_required
def analyze_device(bd_address):
    """Run comprehensive device analysis."""
    bd_address = bd_address.upper()
    do_oui = request.args.get('oui', 'true').lower() == 'true'
    do_class = request.args.get('class', 'true').lower() == 'true'
    do_services = request.args.get('services', 'false').lower() == 'true'

    add_log(f"Device analysis requested for {bd_address}", "INFO")

    result = {'status': 'success'}

    # OUI Lookup
    if do_oui:
        oui_prefix = bd_address.replace(':', '')[:6].upper()
        result['oui'] = lookup_oui(oui_prefix)

    # Device class analysis
    if do_class:
        device = devices.get(bd_address, {})
        device_class = device.get('device_class')
        if device_class:
            result['device_class'] = parse_device_class(device_class)
        else:
            result['device_class'] = {'major': 'Unknown', 'minor': 'Unknown', 'services': []}

    # Service discovery (optional, slow)
    if do_services:
        try:
            sdp_result = subprocess.run(
                ['sdptool', 'browse', bd_address],
                capture_output=True,
                text=True,
                timeout=30
            )

            services = []
            for line in sdp_result.stdout.split('\n'):
                if line.strip().startswith('Service Name:'):
                    services.append({'name': line.split(':', 1)[1].strip()})

            result['services'] = services
        except:
            result['services'] = []

    # Risk assessment
    result['risk'] = assess_device_risk(bd_address, result)

    add_log(f"Device analysis complete for {bd_address}", "INFO")
    return jsonify(result)


def lookup_oui(oui_prefix):
    """Look up OUI (first 3 bytes of MAC) to find manufacturer."""
    # Common OUI prefixes
    oui_db = {
        'A4C138': {'company': 'Apple Inc.', 'country': 'USA'},
        '001451': {'company': 'Apple Inc.', 'country': 'USA'},
        '0C74C2': {'company': 'Apple Inc.', 'country': 'USA'},
        '8C8590': {'company': 'Apple Inc.', 'country': 'USA'},
        'F0D1A9': {'company': 'Apple Inc.', 'country': 'USA'},
        '88E9FE': {'company': 'Apple Inc.', 'country': 'USA'},
        '98D6BB': {'company': 'Apple Inc.', 'country': 'USA'},
        '7014A6': {'company': 'Apple Inc.', 'country': 'USA'},
        'DC2B2A': {'company': 'Apple Inc.', 'country': 'USA'},
        '001E52': {'company': 'Apple Inc.', 'country': 'USA'},
        'AC3743': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        '78D6F0': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        '94350A': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        '308454': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        'BC765E': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        '00E0DC': {'company': 'Nextel Communications', 'country': 'USA'},
        '001124': {'company': 'Google Inc.', 'country': 'USA'},
        '94EB2C': {'company': 'Google Inc.', 'country': 'USA'},
        'F4F5D8': {'company': 'Google Inc.', 'country': 'USA'},
        '001D43': {'company': 'Shenzhen Huawei', 'country': 'China'},
        '001E10': {'company': 'Shenzhen Huawei', 'country': 'China'},
        'E0191D': {'company': 'Huawei Technologies', 'country': 'China'},
        'F8E811': {'company': 'Motorola Mobility', 'country': 'USA'},
        '9C2A83': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        'EC1F72': {'company': 'Samsung Electronics', 'country': 'South Korea'},
        '00037A': {'company': 'Taiyo Yuden Co.', 'country': 'Japan'},
        '0015AF': {'company': 'AzureWave Technologies', 'country': 'Taiwan'},
        '38B8EB': {'company': 'Murata Manufacturing', 'country': 'Japan'},
        '001DD8': {'company': 'Microsoft Corporation', 'country': 'USA'},
        '001517': {'company': 'Intel Corporation', 'country': 'USA'},
        '00A0C6': {'company': 'Qualcomm Inc.', 'country': 'USA'},
        'DC0C5C': {'company': 'OnePlus Technology', 'country': 'China'},
        'B0E235': {'company': 'Xiaomi Communications', 'country': 'China'},
        '60A423': {'company': 'Xiaomi Communications', 'country': 'China'},
    }

    if oui_prefix in oui_db:
        return {**oui_db[oui_prefix], 'prefix': oui_prefix}

    # Check partial match
    for prefix, info in oui_db.items():
        if oui_prefix.startswith(prefix[:4]):
            return {**info, 'prefix': oui_prefix, 'partial_match': True}

    return {'company': 'Unknown', 'prefix': oui_prefix}


def parse_device_class(device_class):
    """Parse Bluetooth device class into human-readable format."""
    try:
        if isinstance(device_class, str):
            device_class = int(device_class, 16) if device_class.startswith('0x') else int(device_class)
    except:
        return {'major': 'Unknown', 'minor': 'Unknown', 'services': []}

    # Major device classes
    major_classes = {
        0: 'Miscellaneous',
        1: 'Computer',
        2: 'Phone',
        3: 'LAN/Network',
        4: 'Audio/Video',
        5: 'Peripheral',
        6: 'Imaging',
        7: 'Wearable',
        8: 'Toy',
        9: 'Health',
        31: 'Uncategorized'
    }

    # Minor classes for phones
    phone_minor = {
        0: 'Uncategorized',
        1: 'Cellular',
        2: 'Cordless',
        3: 'Smartphone',
        4: 'Wired Modem',
        5: 'Common ISDN'
    }

    # Service classes
    service_flags = {
        13: 'Limited Discoverable Mode',
        16: 'Positioning',
        17: 'Networking',
        18: 'Rendering',
        19: 'Capturing',
        20: 'Object Transfer',
        21: 'Audio',
        22: 'Telephony',
        23: 'Information'
    }

    major = (device_class >> 8) & 0x1F
    minor = (device_class >> 2) & 0x3F

    major_name = major_classes.get(major, f'Unknown ({major})')
    minor_name = str(minor)

    if major == 2:
        minor_name = phone_minor.get(minor, f'Unknown ({minor})')

    services = []
    for bit, name in service_flags.items():
        if device_class & (1 << bit):
            services.append(name)

    return {
        'major': major_name,
        'minor': minor_name,
        'services': services,
        'raw': hex(device_class)
    }


def assess_device_risk(bd_address, analysis_data):
    """Assess potential risk level of a device based on analysis."""
    risk_level = 'low'
    notes = []

    device = devices.get(bd_address, {})

    # Check device type
    device_class = analysis_data.get('device_class', {})
    major = device_class.get('major', '')

    if major == 'Phone':
        risk_level = 'medium'
        notes.append('Mobile phone - may contain sensitive data')

    if major == 'Computer':
        risk_level = 'medium'
        notes.append('Computer device - potential data access')

    # Check for interesting services
    services = analysis_data.get('services', [])
    interesting_services = ['OBEX', 'FTP', 'PBAP', 'MAP', 'HFP', 'A2DP']
    for svc in services:
        svc_name = svc.get('name', '') if isinstance(svc, dict) else str(svc)
        for interesting in interesting_services:
            if interesting in svc_name.upper():
                notes.append(f'Exposes {interesting} service')
                if risk_level == 'low':
                    risk_level = 'medium'

    # Check if it's a target
    if bd_address in targets:
        risk_level = 'high'
        notes.insert(0, 'DESIGNATED TARGET')

    # Check signal strength (close device = higher risk)
    rssi = device.get('rssi')
    if rssi and rssi > -50:
        notes.append(f'Very close proximity (RSSI: {rssi} dBm)')

    return {'level': risk_level, 'notes': notes}


def calculate_geolocation(observations):
    """
    ACTIVE GEO: Calculate emitter geolocation from active tracking data.

    This is used by target tracking (L2PING, connected RSSI) which provides
    more reliable, frequent measurements. Can achieve 10-50m CEP with good
    spatial diversity and strong signal.

    Algorithm:
    1. Convert RSSI to estimated distance with uncertainty bounds
    2. Weight observations by signal quality (stronger = more reliable)
    3. Calculate weighted centroid of all system positions
    4. Estimate CEP95 based on measurement quality and spatial diversity

    For 95% confidence to achieve 10m CEP, we need:
    - Strong signal (RSSI > -60 dBm)
    - Good spatial diversity (>50m spread in observations)
    - Multiple observations (>10)
    - Consistent RSSI readings (low variance)
    """
    import math

    if len(observations) < 3:
        return None

    # Path loss parameters
    TX_POWER = -59  # RSSI at 1 meter (typical for BT)
    PATH_LOSS_EXP = 2.5  # Path loss exponent

    weighted_lat = 0
    weighted_lon = 0
    total_weight = 0
    distances = []
    rssi_values = []

    for obs in observations:
        lat, lon, rssi, _ = obs

        if rssi >= 0 or not lat or not lon:
            continue

        rssi_values.append(rssi)

        # Convert RSSI to distance estimate with uncertainty
        dist, dist_min, dist_max = calculate_distance_from_rssi(rssi, TX_POWER)
        distances.append((dist, dist_min, dist_max))

        # Weight by inverse distance squared AND signal strength
        # Stronger signals are more reliable
        signal_quality = max(0.1, (rssi + 100) / 50)  # -50dBm=1.0, -100dBm=0
        weight = signal_quality / (dist * dist + 1)

        weighted_lat += lat * weight
        weighted_lon += lon * weight
        total_weight += weight

    if total_weight == 0 or len(distances) < 3:
        return None

    # Calculate weighted centroid
    est_lat = weighted_lat / total_weight
    est_lon = weighted_lon / total_weight

    # Build observation coordinates for spatial diversity calculation
    obs_coords = [(o[0], o[1], o[2], o[3]) for o in observations if o[0] and o[1]]
    spatial_diversity = calculate_spatial_diversity(obs_coords)

    # === ACTIVE GEO CEP CALCULATION ===
    #
    # Active tracking can achieve tighter CEPs than passive because:
    # 1. More frequent, controlled measurements
    # 2. Connected RSSI is more stable than inquiry RSSI
    # 3. Operator can intentionally create spatial diversity

    avg_distance = sum(d[0] for d in distances) / len(distances)
    avg_uncertainty = sum(d[2] - d[1] for d in distances) / len(distances)

    # RSSI consistency (lower variance = more reliable)
    rssi_mean = sum(rssi_values) / len(rssi_values)
    rssi_variance = sum((r - rssi_mean) ** 2 for r in rssi_values) / len(rssi_values)
    rssi_std = math.sqrt(rssi_variance)

    # Base CEP from distance uncertainty
    base_cep = avg_uncertainty * 0.8  # Active tracking is more reliable

    # RSSI consistency factor (consistent readings = better geo)
    if rssi_std < 3:
        consistency_factor = 0.7  # Very consistent
    elif rssi_std < 6:
        consistency_factor = 0.85  # Good
    elif rssi_std < 10:
        consistency_factor = 1.0  # Normal
    else:
        consistency_factor = 1.3  # High variance, less reliable

    # Spatial diversity factor for active tracking
    if spatial_diversity < 10:
        diversity_factor = 2.5  # Poor - need to move more
    elif spatial_diversity < 30:
        diversity_factor = 1.8
    elif spatial_diversity < 50:
        diversity_factor = 1.3
    elif spatial_diversity < 100:
        diversity_factor = 1.0  # Good
    else:
        diversity_factor = 0.8  # Excellent spread

    # Observation count factor (more observations = better, up to a point)
    if len(observations) >= 20:
        obs_factor = 0.7
    elif len(observations) >= 15:
        obs_factor = 0.8
    elif len(observations) >= 10:
        obs_factor = 0.9
    else:
        obs_factor = 1.0 + (10 - len(observations)) * 0.1

    # Signal strength factor (stronger = more accurate distance estimate)
    strongest_rssi = max(rssi_values)
    if strongest_rssi > -50:
        signal_factor = 0.6  # Very strong - excellent
    elif strongest_rssi > -60:
        signal_factor = 0.8  # Strong
    elif strongest_rssi > -70:
        signal_factor = 1.0  # Normal
    elif strongest_rssi > -80:
        signal_factor = 1.3  # Weak
    else:
        signal_factor = 1.6  # Very weak

    # Calculate final CEP
    cep = base_cep * consistency_factor * diversity_factor * obs_factor * signal_factor

    # Distance-based floor (can't be more accurate than ~20% of distance for active)
    distance_floor = avg_distance * 0.15
    cep = max(cep, distance_floor)

    # ACTIVE GEO can achieve 10m with excellent conditions
    # Minimum 10m only achievable with: diversity>100m, obs>15, rssi>-60, variance<5
    if spatial_diversity > 100 and len(observations) > 15 and strongest_rssi > -60 and rssi_std < 5:
        min_cep = 10
    elif spatial_diversity > 50 and len(observations) > 10 and strongest_rssi > -70:
        min_cep = 15
    elif spatial_diversity > 30 and len(observations) > 5:
        min_cep = 20
    else:
        min_cep = 25  # Need better data for tighter CEP

    cep = max(min_cep, cep)

    # Cap at reasonable maximum
    cep = min(150, cep)

    # Calculate confidence level based on data quality
    confidence = 50
    if len(observations) >= 10:
        confidence += 15
    if spatial_diversity > 50:
        confidence += 15
    if rssi_std < 6:
        confidence += 10
    if strongest_rssi > -65:
        confidence += 5
    confidence = min(95, confidence)

    return {
        'lat': est_lat,
        'lon': est_lon,
        'cep': round(cep, 1),
        'confidence': confidence,
        'method': 'active_rssi_weighted',
        'observations': len(observations),
        'spatial_diversity': round(spatial_diversity, 1),
        'rssi_std': round(rssi_std, 1),
        'strongest_rssi': strongest_rssi
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


# ==================== UBERTOOTH API ROUTES ====================

@app.route('/api/ubertooth/status')
@login_required
def ubertooth_status():
    """Get Ubertooth status and info."""
    info = get_ubertooth_info()
    info['running'] = ubertooth_running
    info['piconets_detected'] = len(ubertooth_data)
    return jsonify(info)


@app.route('/api/ubertooth/start', methods=['POST'])
@login_required
def start_ubertooth_route():
    """Start Ubertooth scanner."""
    success, message = start_ubertooth()
    return jsonify({'status': 'started' if success else 'failed', 'message': message})


@app.route('/api/ubertooth/stop', methods=['POST'])
@login_required
def stop_ubertooth_route():
    """Stop Ubertooth scanner."""
    success, message = stop_ubertooth()
    return jsonify({'status': 'stopped' if success else 'failed', 'message': message})


@app.route('/api/ubertooth/data')
@login_required
def get_ubertooth_data():
    """Get captured piconet data from Ubertooth."""
    # Convert sets to lists for JSON serialization
    data_list = []
    for lap, info in ubertooth_data.items():
        data_copy = info.copy()
        data_copy['channels'] = list(info.get('channels', set()))
        data_list.append(data_copy)
    return jsonify({
        'running': ubertooth_running,
        'piconets': data_list
    })


@app.route('/api/ubertooth/clear', methods=['POST'])
@login_required
def clear_ubertooth_data():
    """Clear captured Ubertooth data."""
    global ubertooth_data
    ubertooth_data = {}
    add_log("Ubertooth data cleared", "INFO")
    return jsonify({'status': 'cleared'})


# ==================== WARHAMMER NETWORK (MESH NETWORK) ====================

# UDP-based BlueK9 peer discovery (more reliable than HTTP for mesh networks)
BLUEK9_UDP_PORT = 5001  # Port for BlueK9 peer discovery
udp_listener_running = False
udp_listener_thread = None

# BlueK9 peer discovery - tracks which peers are running BlueK9
bluek9_peers = {}  # peer_ip -> {system_id, system_name, last_checkin, location, targets}

# iperf3 Speed Test state
speedtest_state = {
    'running': False,
    'target_ip': None,
    'target_name': None,
    'start_time': None,
    'progress': 0,
    'results': [],
    'current_bandwidth': 0,
    'final_result': None,
    'error': None
}
speedtest_process = None
speedtest_thread = None


def start_udp_listener():
    """Start UDP listener for BlueK9 peer announcements."""
    global udp_listener_running, udp_listener_thread

    if udp_listener_running:
        return

    udp_listener_running = True
    udp_listener_thread = threading.Thread(target=udp_listener_loop, daemon=True)
    udp_listener_thread.start()
    add_log(f"BlueK9 UDP listener started on port {BLUEK9_UDP_PORT}", "INFO")


def stop_udp_listener():
    """Stop UDP listener."""
    global udp_listener_running
    udp_listener_running = False
    add_log("BlueK9 UDP listener stopped", "INFO")


def udp_listener_loop():
    """Listen for UDP announcements from other BlueK9 peers."""
    global bluek9_peers, peer_locations, targets

    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', BLUEK9_UDP_PORT))
        sock.settimeout(1.0)  # 1 second timeout for checking udp_listener_running

        add_log(f"UDP listener active on port {BLUEK9_UDP_PORT}", "INFO")

        while udp_listener_running:
            try:
                data, addr = sock.recvfrom(65535)
                peer_ip = addr[0]

                try:
                    message = json.loads(data.decode('utf-8'))

                    if message.get('type') == 'bluek9_announce':
                        # Peer announcement received
                        system_id = message.get('system_id', 'Unknown')
                        system_name = message.get('system_name', system_id)

                        # Check if this is a new peer
                        is_new_peer = peer_ip not in bluek9_peers

                        # Update peer info
                        bluek9_peers[peer_ip] = {
                            'system_id': system_id,
                            'system_name': system_name,
                            'version': message.get('version', 'unknown'),
                            'last_checkin': datetime.utcnow().isoformat() + 'Z',
                            'ip': peer_ip
                        }

                        if is_new_peer:
                            add_log(f"*** BlueK9 peer discovered: {system_name} ({peer_ip}) ***", "INFO")

                        # Update location if provided
                        if message.get('location'):
                            loc = message['location']
                            if loc.get('lat') and loc.get('lon'):
                                peer_locations[system_id] = {
                                    'system_id': system_id,
                                    'system_name': system_name,
                                    'lat': loc['lat'],
                                    'lon': loc['lon'],
                                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                                }

                        # Process shared targets if provided
                        if message.get('targets'):
                            new_targets = 0
                            for target in message['targets']:
                                bd_addr = target.get('bd_address', '').upper()
                                if bd_addr and bd_addr not in targets:
                                    targets[bd_addr] = {
                                        'bd_address': bd_addr,
                                        'alias': target.get('alias', ''),
                                        'added_timestamp': datetime.utcnow().isoformat() + 'Z',
                                        'source': system_id
                                    }
                                    new_targets += 1
                            if new_targets > 0:
                                add_log(f"Received {new_targets} new target(s) from {system_name}", "INFO")
                                socketio.emit('targets_update', list(targets.values()))

                except json.JSONDecodeError:
                    pass  # Ignore malformed messages
                except Exception as e:
                    add_log(f"Error processing UDP message: {e}", "DEBUG")

            except socket.timeout:
                continue  # Normal timeout, check if we should keep running
            except Exception as e:
                if udp_listener_running:
                    add_log(f"UDP listener error: {e}", "WARNING")
                break

        sock.close()

    except Exception as e:
        add_log(f"Failed to start UDP listener: {e}", "ERROR")


def send_udp_announcement(peer_ips=None):
    """Send UDP announcement to all NetBird peers or specific IPs."""
    import socket

    # Build announcement message
    message = {
        'type': 'bluek9_announce',
        'system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
        'system_name': CONFIG.get('SYSTEM_NAME', 'BlueK9'),
        'version': 'v3.1.0'
    }

    # Include location if available
    if current_location.get('lat') and current_location.get('lon'):
        message['location'] = {
            'lat': current_location['lat'],
            'lon': current_location['lon']
        }

    # Include targets for sharing
    if targets:
        message['targets'] = list(targets.values())

    data = json.dumps(message).encode('utf-8')

    # Get peer IPs from NetBird if not provided
    if peer_ips is None:
        peer_ips = []
        for peer in warhammer_peers.values():
            if peer.get('connected') and peer.get('ip'):
                peer_ips.append(peer['ip'])

    if not peer_ips:
        return 0

    # Send to each peer
    sent_count = 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)

        for peer_ip in peer_ips:
            try:
                sock.sendto(data, (peer_ip, BLUEK9_UDP_PORT))
                sent_count += 1
            except Exception as e:
                add_log(f"Failed to send UDP to {peer_ip}: {e}", "DEBUG")

        sock.close()

    except Exception as e:
        add_log(f"UDP send error: {e}", "WARNING")

    if sent_count > 0:
        add_log(f"Sent BlueK9 announcement to {sent_count} peer(s)", "DEBUG")

    return sent_count


def parse_netbird_status():
    """Parse netbird status -d output to get peer information.

    Format expected:
    Peers detail:
     hostname.netbird.selfhosted:
      NetBird IP: 100.74.x.x
      Status: Connected
      ...
    """
    global warhammer_peers

    try:
        result = subprocess.run(
            ['netbird', 'status', '-d'],
            capture_output=True, text=True, timeout=10
        )

        if result.returncode != 0:
            add_log(f"netbird status failed: {result.stderr}", "WARNING")
            return []

        output = result.stdout
        peers = []
        current_peer = None
        our_ip = None
        in_peers_section = False

        lines = output.split('\n')
        for raw_line in lines:
            # Check for "Peers detail:" section start
            if raw_line.strip() == 'Peers detail:':
                in_peers_section = True
                continue

            # Check for end of peers section (lines without leading space that aren't peer names)
            if in_peers_section and raw_line and not raw_line.startswith(' '):
                # End of peers section
                in_peers_section = False
                if current_peer and current_peer.get('ip'):
                    peers.append(current_peer)
                    current_peer = None

            # Get our NetBird IP from the summary section at bottom
            stripped = raw_line.strip()
            if stripped.startswith('NetBird IP:') and not in_peers_section:
                our_ip = stripped.split(':', 1)[1].strip().split('/')[0]

            if in_peers_section:
                # Peer hostname line: single space + hostname + colon
                # Example: " wh-00111.netbird.selfhosted:"
                if raw_line.startswith(' ') and not raw_line.startswith('  ') and raw_line.strip().endswith(':'):
                    # Save previous peer if exists
                    if current_peer and current_peer.get('ip'):
                        peers.append(current_peer)

                    # Start new peer
                    hostname = raw_line.strip().rstrip(':')
                    current_peer = {
                        'id': hostname,
                        'hostname': hostname,
                        'name': hostname.split('.')[0],  # Short name (e.g., wh-00111)
                        'ip': '',
                        'connected': False,
                        'connection_type': '',
                        'latency': '',
                        'last_handshake': '',
                        'transfer_rx': '',
                        'transfer_tx': '',
                        'is_bluek9': False
                    }

                # Peer detail lines: two spaces + field
                elif current_peer and raw_line.startswith('  '):
                    line = raw_line.strip()
                    if line.startswith('NetBird IP:'):
                        current_peer['ip'] = line.split(':', 1)[1].strip()
                    elif line.startswith('Status:'):
                        status = line.split(':', 1)[1].strip().lower()
                        current_peer['connected'] = status == 'connected'
                    elif line.startswith('Connection type:'):
                        conn_type = line.split(':', 1)[1].strip()
                        current_peer['connection_type'] = conn_type if conn_type != '-' else ''
                    elif line.startswith('Latency:'):
                        current_peer['latency'] = line.split(':', 1)[1].strip()
                    elif 'WireGuard handshake:' in line:
                        current_peer['last_handshake'] = line.split(':', 1)[1].strip()
                    elif 'Transfer status' in line:
                        match = re.search(r'\(received/sent\)\s*(.+)/(.+)', line)
                        if match:
                            current_peer['transfer_rx'] = match.group(1).strip()
                            current_peer['transfer_tx'] = match.group(2).strip()

        # Don't forget the last peer
        if current_peer and current_peer.get('ip'):
            peers.append(current_peer)

        # Update global state
        warhammer_peers = {}
        for peer in peers:
            peer_id = peer['hostname']
            warhammer_peers[peer_id] = peer

            # Check if this peer is a known BlueK9 instance
            peer_ip = peer['ip']
            if peer_ip in bluek9_peers:
                peer['is_bluek9'] = True
                peer['system_id'] = bluek9_peers[peer_ip].get('system_id')
                peer['system_name'] = bluek9_peers[peer_ip].get('system_name')

        return peers

    except subprocess.TimeoutExpired:
        add_log("netbird status timeout", "WARNING")
        return []
    except FileNotFoundError:
        add_log("netbird command not found", "WARNING")
        return []
    except Exception as e:
        add_log(f"netbird status parse error: {e}", "WARNING")
        return []


def get_netbird_routes():
    """Get routes from netbird status."""
    global warhammer_routes

    try:
        result = subprocess.run(
            ['netbird', 'routes', 'list'],
            capture_output=True, text=True, timeout=10
        )

        routes = []
        if result.returncode == 0:
            # Parse route list output
            lines = result.stdout.strip().split('\n')
            for line in lines:
                if line and not line.startswith('ID') and not line.startswith('-'):
                    parts = line.split()
                    if len(parts) >= 3:
                        route = {
                            'id': parts[0] if len(parts) > 0 else '',
                            'network': parts[1] if len(parts) > 1 else '',
                            'description': ' '.join(parts[2:]) if len(parts) > 2 else '',
                            'enabled': True,
                            'persistent': 'persistent' in line.lower()
                        }
                        routes.append(route)

        warhammer_routes = {r['id']: r for r in routes}
        return routes

    except Exception as e:
        add_log(f"netbird routes error: {e}", "WARNING")
        return []


def check_bluek9_peer(peer_ip, timeout=3):
    """Check if a peer is running BlueK9 by attempting to connect."""
    import urllib.request
    import urllib.error
    import socket

    add_log(f"Checking if peer {peer_ip} is running BlueK9...", "DEBUG")

    try:
        url = f"http://{peer_ip}:5000/api/network/checkin"
        req = urllib.request.Request(url, method='GET')
        req.add_header('X-BlueK9-Checkin', 'true')
        req.add_header('User-Agent', 'BlueK9-PeerCheck/3.1.0')

        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get('is_bluek9'):
                result = {
                    'is_bluek9': True,
                    'system_id': data.get('system_id'),
                    'system_name': data.get('system_name'),
                    'version': data.get('version')
                }
                # Include location data if available
                if data.get('location'):
                    result['location'] = data['location']
                add_log(f"Found BlueK9 peer: {data.get('system_name', data.get('system_id'))} at {peer_ip}", "INFO")
                return result
            else:
                add_log(f"Peer {peer_ip} responded but is not BlueK9", "DEBUG")
    except urllib.error.HTTPError as e:
        add_log(f"Peer {peer_ip} HTTP error: {e.code} {e.reason}", "DEBUG")
    except urllib.error.URLError as e:
        add_log(f"Peer {peer_ip} connection failed: {e.reason}", "DEBUG")
    except socket.timeout:
        add_log(f"Peer {peer_ip} connection timed out", "DEBUG")
    except Exception as e:
        add_log(f"Error checking peer {peer_ip}: {type(e).__name__}: {e}", "DEBUG")

    return {'is_bluek9': False}


def broadcast_location_to_peers():
    """Broadcast our location to all known BlueK9 peers."""
    import urllib.request
    import urllib.error

    if not current_location.get('lat') or not current_location.get('lon'):
        return

    location_data = {
        'system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
        'system_name': CONFIG.get('SYSTEM_NAME', 'BlueK9'),
        'lat': current_location['lat'],
        'lon': current_location['lon'],
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }

    # Update our own location in peer_locations
    peer_locations[location_data['system_id']] = location_data

    # Send to all connected BlueK9 peers and request their location back
    for peer_ip, peer_info in list(bluek9_peers.items()):
        try:
            url = f"http://{peer_ip}:5000/api/network/location"
            data = json.dumps(location_data).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-BlueK9-Location', 'true')

            with urllib.request.urlopen(req, timeout=2) as response:
                # Parse response which should include their location
                resp_data = json.loads(response.read().decode('utf-8'))
                if resp_data.get('location'):
                    loc = resp_data['location']
                    if loc.get('lat') and loc.get('lon'):
                        peer_locations[loc['system_id']] = loc
                        add_log(f"Received location from {loc.get('system_name', loc['system_id'])}", "DEBUG")
        except Exception as e:
            pass  # Peer may be offline or unreachable


def warhammer_monitor_loop():
    """Background loop to monitor WARHAMMER network status.

    Timing:
    - Discovery mode (no BlueK9 peers): announce every 20 seconds
    - Connected mode (BlueK9 peers found): announce every 2 seconds for real-time updates
    """
    global warhammer_running, warhammer_peers, peer_locations, bluek9_peers

    add_log("WARHAMMER network monitor started", "INFO")

    # Start UDP listener for peer announcements
    start_udp_listener()

    # Timing configuration
    DISCOVERY_INTERVAL = 20  # Seconds between announcements when no BlueK9 peers
    CONNECTED_INTERVAL = 2   # Seconds between announcements when BlueK9 peers connected
    STALE_TIMEOUT = 30       # Seconds before marking a peer as offline

    last_announce_time = 0
    last_netbird_check = 0
    NETBIRD_CHECK_INTERVAL = 10  # Check netbird status less frequently

    while warhammer_running:
        try:
            current_time = time.time()

            # Check netbird status periodically (not every loop)
            if current_time - last_netbird_check >= NETBIRD_CHECK_INTERVAL:
                peers = parse_netbird_status()
                routes = get_netbird_routes()
                last_netbird_check = current_time
            else:
                # Use cached data
                peers = list(warhammer_peers.values())
                routes = list(warhammer_routes.values())

            # Determine interval based on whether we have BlueK9 peers
            has_bluek9_peers = len(bluek9_peers) > 0
            announce_interval = CONNECTED_INTERVAL if has_bluek9_peers else DISCOVERY_INTERVAL

            # Send UDP announcement at appropriate interval
            if current_time - last_announce_time >= announce_interval:
                connected_peer_ips = [p['ip'] for p in peers if p.get('connected') and p.get('ip')]
                if connected_peer_ips:
                    sent = send_udp_announcement(connected_peer_ips)
                    if not has_bluek9_peers and sent > 0:
                        add_log(f"Searching for BlueK9 peers... (sent to {sent} network peers)", "DEBUG")
                last_announce_time = current_time

            # Mark peers as BlueK9 if we've received UDP announcements from them
            for peer in peers:
                peer_ip = peer.get('ip')
                if peer_ip and peer_ip in bluek9_peers:
                    peer['is_bluek9'] = True
                    peer['system_id'] = bluek9_peers[peer_ip].get('system_id')
                    peer['system_name'] = bluek9_peers[peer_ip].get('system_name')

            # Clean up stale BlueK9 peers (no announcement received recently)
            stale_peers = []
            for peer_ip, peer_info in bluek9_peers.items():
                last_checkin = peer_info.get('last_checkin', '')
                if last_checkin:
                    try:
                        last_dt = datetime.fromisoformat(last_checkin.replace('Z', '+00:00'))
                        age = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
                        if age > STALE_TIMEOUT:
                            stale_peers.append(peer_ip)
                    except Exception:
                        pass

            for peer_ip in stale_peers:
                peer_name = bluek9_peers[peer_ip].get('system_name', peer_ip)
                system_id = bluek9_peers[peer_ip].get('system_id')
                del bluek9_peers[peer_ip]
                # Also remove from peer_locations
                if system_id and system_id in peer_locations:
                    del peer_locations[system_id]
                add_log(f"BlueK9 peer {peer_name} went offline", "INFO")

            # Emit updates to connected web clients
            socketio.emit('warhammer_update', {
                'peers': peers,
                'peer_locations': list(peer_locations.values()),
                'routes': routes,
                'bluek9_count': len(bluek9_peers)
            })

        except Exception as e:
            add_log(f"WARHAMMER monitor error: {e}", "WARNING")

        # Short sleep for responsive loop
        time.sleep(0.5)

    # Stop UDP listener when monitor stops
    stop_udp_listener()
    add_log("WARHAMMER network monitor stopped", "INFO")


def get_our_location_data():
    """Get our current location data for sharing."""
    if current_location.get('lat') and current_location.get('lon'):
        return {
            'system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
            'system_name': CONFIG.get('SYSTEM_NAME', 'BlueK9'),
            'lat': current_location['lat'],
            'lon': current_location['lon'],
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }
    return None


def broadcast_targets_to_peers():
    """Broadcast our targets to all BlueK9 peers and receive their targets."""
    import urllib.request
    import urllib.error

    # Get targets from database (not in-memory dict)
    db_targets = get_targets_from_db()

    if not db_targets:
        add_log("No targets to broadcast", "DEBUG")
        return {'sent': 0, 'received': 0}

    if not bluek9_peers:
        add_log("No BlueK9 peers to broadcast targets to", "DEBUG")
        return {'sent': 0, 'received': 0}

    add_log(f"Broadcasting {len(db_targets)} target(s) to {len(bluek9_peers)} peer(s)", "INFO")

    targets_data = {
        'system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
        'targets': db_targets
    }

    sent_count = 0
    received_count = 0

    # Get current target BD addresses for duplicate checking
    current_bd_addresses = {t['bd_address'].upper() for t in db_targets}

    for peer_ip, peer_info in list(bluek9_peers.items()):
        peer_name = peer_info.get('system_name', peer_ip)
        try:
            url = f"http://{peer_ip}:5000/api/network/targets"
            data = json.dumps(targets_data).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-BlueK9-Targets', 'true')
            req.add_header('User-Agent', 'BlueK9-TargetSync/3.1.0')

            with urllib.request.urlopen(req, timeout=3) as response:
                resp_data = json.loads(response.read().decode('utf-8'))
                sent_count += 1

                # Check if peer sent back targets - save to database
                if resp_data.get('targets'):
                    peer_source = peer_info.get('system_id', peer_ip)
                    for target in resp_data['targets']:
                        bd_addr = target.get('bd_address', '').upper()
                        if bd_addr and bd_addr not in current_bd_addresses:
                            # Save to database
                            if save_target_to_db(
                                bd_addr,
                                alias=target.get('alias', ''),
                                notes=target.get('notes', f'Synced from {peer_source}'),
                                priority=target.get('priority', 1),
                                source=peer_source
                            ):
                                current_bd_addresses.add(bd_addr)
                                received_count += 1

                add_log(f"Synced targets with {peer_name}", "DEBUG")

        except urllib.error.HTTPError as e:
            add_log(f"Target sync to {peer_name} failed: HTTP {e.code}", "WARNING")
        except urllib.error.URLError as e:
            add_log(f"Target sync to {peer_name} failed: {e.reason}", "WARNING")
        except Exception as e:
            add_log(f"Target sync to {peer_name} error: {e}", "WARNING")

    if sent_count > 0:
        add_log(f"Target sync: sent to {sent_count} peer(s), received {received_count} new target(s)", "INFO")

    return {'sent': sent_count, 'received': received_count}


# API endpoint for BlueK9 peer check-in (other BlueK9 instances call this)
@app.route('/api/network/checkin')
def network_checkin():
    """Respond to BlueK9 peer check-in requests with our info and location."""
    # No login required - this is for peer discovery
    if request.headers.get('X-BlueK9-Checkin') != 'true':
        return jsonify({'error': 'Invalid request'}), 400

    response_data = {
        'is_bluek9': True,
        'system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
        'system_name': CONFIG.get('SYSTEM_NAME', 'BlueK9'),
        'version': 'v3.1.0'
    }

    # Include our location if available
    location = get_our_location_data()
    if location:
        response_data['location'] = location

    return jsonify(response_data)


# API endpoint to receive location from other BlueK9 peers
@app.route('/api/network/location', methods=['POST'])
def receive_peer_location():
    """Receive location update from a BlueK9 peer and return our location."""
    global peer_locations

    if request.headers.get('X-BlueK9-Location') != 'true':
        return jsonify({'error': 'Invalid request'}), 400

    data = request.json
    if not data or not data.get('system_id'):
        return jsonify({'error': 'Invalid data'}), 400

    # Store their location
    peer_locations[data['system_id']] = {
        'system_id': data['system_id'],
        'system_name': data.get('system_name', data['system_id']),
        'lat': data.get('lat'),
        'lon': data.get('lon'),
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }

    # Emit to web clients
    socketio.emit('peer_location_update', peer_locations[data['system_id']])

    # Return our location in response
    response = {'status': 'received'}
    our_location = get_our_location_data()
    if our_location:
        response['location'] = our_location

    return jsonify(response)


# API endpoint to receive targets from other BlueK9 peers (bidirectional sync)
@app.route('/api/network/targets', methods=['POST'])
def receive_peer_targets():
    """Receive shared targets from a BlueK9 peer and return our targets."""
    if request.headers.get('X-BlueK9-Targets') != 'true':
        return jsonify({'error': 'Invalid request'}), 400

    data = request.json
    if not data:
        return jsonify({'error': 'Invalid data'}), 400

    source_system = data.get('system_id', 'Unknown')
    new_targets = 0

    # Process incoming targets - save to database
    for target in data.get('targets', []):
        bd_addr = target.get('bd_address', '').upper()
        if bd_addr:
            # Save to database (handles duplicate checking internally)
            if save_target_to_db(
                bd_addr,
                alias=target.get('alias', ''),
                notes=target.get('notes', f'Synced from {source_system}'),
                priority=target.get('priority', 1),
                source=source_system
            ):
                new_targets += 1

    if new_targets > 0:
        add_log(f"Received {new_targets} new target(s) from {source_system}", "INFO")
        # Emit updated targets to web clients
        db_targets = get_targets_from_db()
        socketio.emit('targets_update', db_targets)

    # Return our targets from database for bidirectional sync
    our_targets = get_targets_from_db()
    response = {
        'status': 'received',
        'new_targets': new_targets,
        'targets': our_targets  # Send back our targets from database
    }
    return jsonify(response)


# API endpoint to manually sync targets with peers
@app.route('/api/network/sync_targets', methods=['POST'])
@login_required
def sync_targets_with_peers():
    """Manually trigger target sync with all BlueK9 peers."""
    if not bluek9_peers:
        add_log("Target sync failed: no BlueK9 peers connected", "WARNING")
        return jsonify({'status': 'no_peers', 'peer_count': 0, 'message': 'No BlueK9 peers connected'})

    result = broadcast_targets_to_peers()

    # Emit updated targets to web clients from database
    db_targets = get_targets_from_db()
    socketio.emit('targets_update', db_targets)

    return jsonify({
        'status': 'synced',
        'peer_count': len(bluek9_peers),
        'sent_to': result.get('sent', 0),
        'received': result.get('received', 0),
        'total_targets': len(db_targets)
    })


@app.route('/api/network/status')
@login_required
def get_network_status():
    """Get WARHAMMER network status."""
    return jsonify({
        'network_name': WARHAMMER_CONFIG['NETWORK_NAME'],
        'running': warhammer_running,
        'peer_count': len(warhammer_peers),
        'connected_peers': sum(1 for p in warhammer_peers.values() if p.get('connected')),
        'bluek9_peers': len(bluek9_peers),
        'route_count': len(warhammer_routes)
    })


@app.route('/api/network/diagnostic')
def get_network_diagnostic():
    # No login required - diagnostic endpoint for troubleshooting
    """Get detailed diagnostic info for WARHAMMER network troubleshooting."""
    # Get fresh netbird status
    peers = parse_netbird_status()
    connected_ips = [p['ip'] for p in peers if p.get('connected') and p.get('ip')]

    return jsonify({
        'warhammer_running': warhammer_running,
        'udp_listener_running': udp_listener_running,
        'udp_port': BLUEK9_UDP_PORT,
        'netbird_peers': {
            'total': len(peers),
            'connected': len([p for p in peers if p.get('connected')]),
            'connected_ips': connected_ips
        },
        'bluek9_peers': {
            'count': len(bluek9_peers),
            'peers': list(bluek9_peers.values())
        },
        'peer_locations': list(peer_locations.values()),
        'our_system_id': CONFIG.get('SYSTEM_ID', 'BK9-001'),
        'our_system_name': CONFIG.get('SYSTEM_NAME', 'BlueK9'),
        'our_location': {
            'lat': current_location.get('lat'),
            'lon': current_location.get('lon')
        }
    })


@app.route('/api/network/test_udp', methods=['POST'])
def test_udp_send():
    # No login required - diagnostic endpoint for troubleshooting
    """Test UDP announcement to all peers."""
    peers = parse_netbird_status()
    connected_ips = [p['ip'] for p in peers if p.get('connected') and p.get('ip')]

    if not connected_ips:
        return jsonify({
            'status': 'error',
            'message': 'No connected NetBird peers found',
            'netbird_peers': len(peers)
        })

    sent = send_udp_announcement(connected_ips)
    add_log(f"Test UDP: Sent announcement to {sent}/{len(connected_ips)} peers", "INFO")

    return jsonify({
        'status': 'sent',
        'sent_to': sent,
        'total_peers': len(connected_ips),
        'peer_ips': connected_ips,
        'bluek9_peers_found': len(bluek9_peers)
    })


@app.route('/api/network/peers')
@login_required
def get_network_peers():
    """Get list of WARHAMMER network peers."""
    peers = parse_netbird_status()
    return jsonify({
        'peers': peers,
        'peer_locations': list(peer_locations.values()),
        'bluek9_peers': list(bluek9_peers.values())
    })


@app.route('/api/network/routes')
@login_required
def get_network_routes():
    """Get list of WARHAMMER network routes."""
    routes = get_netbird_routes()
    return jsonify({'routes': routes})


@app.route('/api/network/routes', methods=['POST'])
@login_required
def add_network_route():
    """Add a new network route - requires NetBird management dashboard."""
    # Route creation requires NetBird management dashboard access
    # Local CLI doesn't support adding routes
    add_log("Route creation requires NetBird management dashboard", "WARNING")
    return jsonify({
        'error': 'Route creation requires NetBird management dashboard',
        'hint': 'Use the NetBird dashboard at https://app.netbird.io to add routes'
    }), 501


@app.route('/api/network/routes/<route_id>', methods=['DELETE'])
@login_required
def delete_network_route(route_id):
    """Delete a network route - requires NetBird management dashboard."""
    route = warhammer_routes.get(route_id)
    if route and route.get('persistent'):
        return jsonify({'error': 'Cannot delete persistent routes'}), 403

    # Route deletion requires NetBird management dashboard access
    add_log("Route deletion requires NetBird management dashboard", "WARNING")
    return jsonify({
        'error': 'Route deletion requires NetBird management dashboard',
        'hint': 'Use the NetBird dashboard at https://app.netbird.io to manage routes'
    }), 501


@app.route('/api/network/routes/<route_id>/toggle', methods=['POST'])
@login_required
def toggle_network_route(route_id):
    """Toggle a network route using netbird routes select/deselect."""
    route = warhammer_routes.get(route_id)
    if not route:
        return jsonify({'error': 'Route not found'}), 404

    if route.get('persistent'):
        return jsonify({'error': 'Cannot modify persistent routes'}), 403

    try:
        # Use netbird CLI to select/deselect routes
        network = route.get('network', '')
        current_enabled = route.get('enabled', True)

        if current_enabled:
            # Deselect (disable) the route
            result = subprocess.run(
                ['netbird', 'routes', 'deselect', network],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                route['enabled'] = False
                add_log(f"WARHAMMER route disabled: {network}", "INFO")
                return jsonify({'status': 'toggled', 'enabled': False})
        else:
            # Select (enable) the route
            result = subprocess.run(
                ['netbird', 'routes', 'select', network],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                route['enabled'] = True
                add_log(f"WARHAMMER route enabled: {network}", "INFO")
                return jsonify({'status': 'toggled', 'enabled': True})

        add_log(f"Route toggle failed: {result.stderr}", "ERROR")
        return jsonify({'error': f'Failed to toggle route: {result.stderr}'}), 500

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Command timeout'}), 500
    except Exception as e:
        add_log(f"Route toggle error: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500


# ==================== iperf3 SPEED TEST ====================

def run_speedtest(target_ip, target_name, duration=10):
    """Run iperf3 speed test in background thread with real-time updates."""
    global speedtest_state, speedtest_process

    try:
        speedtest_state['running'] = True
        speedtest_state['target_ip'] = target_ip
        speedtest_state['target_name'] = target_name
        speedtest_state['start_time'] = datetime.now().isoformat()
        speedtest_state['progress'] = 0
        speedtest_state['results'] = []
        speedtest_state['current_bandwidth'] = 0
        speedtest_state['final_result'] = None
        speedtest_state['error'] = None

        add_log(f"Speed test started to {target_name} ({target_ip})", "INFO")

        # Emit start event
        socketio.emit('speedtest_update', {
            'status': 'running',
            'target_ip': target_ip,
            'target_name': target_name,
            'progress': 0,
            'bandwidth': 0,
            'results': []
        })

        # Run iperf3 with human-readable output for real-time parsing
        # -f m = format in Mbits, -i 1 = report every second
        cmd = ['iperf3', '-c', target_ip, '-t', str(duration), '-i', '1', '-f', 'm']
        speedtest_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered for real-time output
        )

        interval_count = 0
        total_bytes = 0
        total_retransmits = 0

        # Parse output line by line in real-time
        for line in iter(speedtest_process.stdout.readline, ''):
            if not speedtest_state['running']:
                break

            line = line.strip()
            if not line:
                continue

            # Parse interval lines like: "[  5]   0.00-1.00   sec   112 MBytes   941 Mbits/sec    0   sender"
            # or "[  5]   0.00-1.00   sec  95.1 MBytes   798 Mbits/sec"
            if 'Mbits/sec' in line and 'sec' in line and '-' in line:
                # Skip summary lines (they have longer time ranges)
                parts = line.split()
                try:
                    # Find the Mbits/sec value
                    for i, part in enumerate(parts):
                        if part == 'Mbits/sec' and i > 0:
                            mbps = float(parts[i-1])
                            interval_count += 1
                            progress = min(int((interval_count / duration) * 100), 99)

                            speedtest_state['progress'] = progress
                            speedtest_state['current_bandwidth'] = round(mbps, 2)
                            speedtest_state['results'].append({
                                'time': interval_count,
                                'mbps': round(mbps, 2)
                            })

                            # Emit real-time update
                            socketio.emit('speedtest_update', {
                                'status': 'running',
                                'target_ip': target_ip,
                                'target_name': target_name,
                                'progress': progress,
                                'bandwidth': round(mbps, 2),
                                'results': list(speedtest_state['results'])
                            })
                            break

                    # Track bytes for final result
                    for i, part in enumerate(parts):
                        if part == 'MBytes' and i > 0:
                            total_bytes += float(parts[i-1]) * 1024 * 1024
                        if part in ['sender', 'receiver'] and i > 1:
                            try:
                                total_retransmits += int(parts[i-1])
                            except ValueError:
                                pass

                except (ValueError, IndexError):
                    continue

        speedtest_process.wait()

        if speedtest_process.returncode == 0:
            # Calculate final results from collected data
            avg_mbps = 0
            if speedtest_state['results']:
                avg_mbps = sum(r['mbps'] for r in speedtest_state['results']) / len(speedtest_state['results'])

            final_result = {
                'upload_mbps': round(avg_mbps, 2),
                'download_mbps': round(avg_mbps, 2),  # Same as upload for single direction test
                'bytes_sent': int(total_bytes),
                'bytes_received': 0,
                'retransmits': total_retransmits,
                'duration': duration,
                'target_ip': target_ip,
                'target_name': target_name,
                'timestamp': datetime.now().isoformat()
            }

            speedtest_state['final_result'] = final_result
            speedtest_state['progress'] = 100

            add_log(f"Speed test complete: {final_result['upload_mbps']} Mbps avg throughput", "INFO")

            # Emit completion
            socketio.emit('speedtest_update', {
                'status': 'complete',
                'target_ip': target_ip,
                'target_name': target_name,
                'progress': 100,
                'final_result': final_result,
                'results': list(speedtest_state['results'])
            })

        else:
            stderr = speedtest_process.stderr.read()
            speedtest_state['error'] = f"iperf3 failed: {stderr}"
            add_log(f"Speed test failed: {stderr}", "ERROR")
            socketio.emit('speedtest_update', {
                'status': 'error',
                'error': speedtest_state['error']
            })

    except Exception as e:
        speedtest_state['error'] = str(e)
        add_log(f"Speed test error: {e}", "ERROR")
        socketio.emit('speedtest_update', {
            'status': 'error',
            'error': str(e)
        })

    finally:
        speedtest_state['running'] = False
        speedtest_process = None


@app.route('/api/network/speedtest/start', methods=['POST'])
@login_required
def start_speedtest():
    """Start iperf3 speed test to a peer."""
    global speedtest_thread

    if speedtest_state['running']:
        return jsonify({'error': 'Speed test already running'}), 400

    data = request.json or {}
    target_ip = data.get('target_ip')
    target_name = data.get('target_name', target_ip)
    duration = min(data.get('duration', 10), 30)  # Max 30 seconds

    if not target_ip:
        return jsonify({'error': 'No target IP specified'}), 400

    # Validate target is a BlueK9 peer or NetBird peer
    valid_target = False
    if target_ip in bluek9_peers:
        valid_target = True
        target_name = bluek9_peers[target_ip].get('system_name', target_ip)

    # Also allow NetBird peers
    netbird_peers = parse_netbird_status()
    for peer in netbird_peers:
        if peer.get('ip') == target_ip and peer.get('connected'):
            valid_target = True
            if not target_name or target_name == target_ip:
                target_name = peer.get('hostname', target_ip)
            break

    if not valid_target:
        return jsonify({'error': 'Target must be a connected peer'}), 400

    # Start test in background
    speedtest_thread = threading.Thread(
        target=run_speedtest,
        args=(target_ip, target_name, duration),
        daemon=True
    )
    speedtest_thread.start()

    return jsonify({
        'status': 'started',
        'target_ip': target_ip,
        'target_name': target_name,
        'duration': duration
    })


@app.route('/api/network/speedtest/stop', methods=['POST'])
@login_required
def stop_speedtest():
    """Stop running speed test."""
    global speedtest_process

    if not speedtest_state['running']:
        return jsonify({'status': 'not_running'})

    if speedtest_process:
        speedtest_process.terminate()
        speedtest_process = None

    speedtest_state['running'] = False
    speedtest_state['error'] = 'Test cancelled by user'

    socketio.emit('speedtest_update', {
        'status': 'cancelled',
        'message': 'Speed test cancelled'
    })

    add_log("Speed test cancelled by user", "INFO")
    return jsonify({'status': 'stopped'})


@app.route('/api/network/speedtest/status')
@login_required
def get_speedtest_status():
    """Get current speed test status."""
    return jsonify({
        'running': speedtest_state['running'],
        'target_ip': speedtest_state['target_ip'],
        'target_name': speedtest_state['target_name'],
        'progress': speedtest_state['progress'],
        'current_bandwidth': speedtest_state['current_bandwidth'],
        'results': speedtest_state['results'],
        'final_result': speedtest_state['final_result'],
        'error': speedtest_state['error']
    })


@app.route('/api/network/speedtest/peers')
@login_required
def get_speedtest_peers():
    """Get list of peers available for speed testing (running iperf3 server)."""
    peers = []

    # Add BlueK9 peers
    for ip, info in bluek9_peers.items():
        peers.append({
            'ip': ip,
            'name': info.get('system_name', ip),
            'type': 'bluek9',
            'system_id': info.get('system_id', 'Unknown')
        })

    # Add connected NetBird peers (they should be running iperf3 server)
    netbird_peers = parse_netbird_status()
    bluek9_ips = set(bluek9_peers.keys())
    for peer in netbird_peers:
        ip = peer.get('ip')
        if ip and peer.get('connected') and ip not in bluek9_ips:
            peers.append({
                'ip': ip,
                'name': peer.get('hostname', ip),
                'type': 'netbird',
                'latency': peer.get('latency', 'N/A')
            })

    return jsonify({'peers': peers})


@app.route('/api/network/monitor/start', methods=['POST'])
@login_required
def start_network_monitor():
    """Start WARHAMMER network monitoring."""
    global warhammer_running, warhammer_monitor_thread

    if warhammer_running:
        return jsonify({'status': 'already_running'})

    warhammer_running = True
    warhammer_monitor_thread = threading.Thread(target=warhammer_monitor_loop, daemon=True)
    warhammer_monitor_thread.start()

    # Initial data fetch
    peers = parse_netbird_status()
    routes = get_netbird_routes()

    # Emit initial data
    socketio.emit('warhammer_update', {
        'peers': peers,
        'peer_locations': list(peer_locations.values()),
        'routes': routes,
        'bluek9_count': 0
    })

    add_log("WARHAMMER network monitoring started", "INFO")
    return jsonify({'status': 'started'})


@app.route('/api/network/monitor/stop', methods=['POST'])
@login_required
def stop_network_monitor():
    """Stop WARHAMMER network monitoring."""
    global warhammer_running

    if not warhammer_running:
        return jsonify({'status': 'not_running'})

    warhammer_running = False
    add_log("WARHAMMER network monitoring stopped", "INFO")
    return jsonify({'status': 'stopped'})


@app.route('/api/network/peer_location', methods=['POST'])
@login_required
def update_peer_location():
    """Receive location update from a peer."""
    global peer_locations
    data = request.json
    if not data or not data.get('system_id'):
        return jsonify({'error': 'Invalid data'}), 400

    peer_locations[data['system_id']] = {
        'system_id': data['system_id'],
        'system_name': data.get('system_name', data['system_id']),
        'lat': data.get('lat'),
        'lon': data.get('lon'),
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }

    # Broadcast to all connected clients
    socketio.emit('peer_location_update', peer_locations[data['system_id']])

    return jsonify({'status': 'updated'})


# ==================== CELLULAR SIGNAL MONITORING ====================

def get_cellular_modem_info():
    """Get cellular modem information using mmcli."""
    global cellular_signal

    try:
        # Find modem
        result = subprocess.run(['mmcli', '-L'], capture_output=True, text=True, timeout=5)
        modem_match = re.search(r'/org/freedesktop/ModemManager1/Modem/(\d+)', result.stdout)

        if not modem_match:
            return None

        modem_id = modem_match.group(1)

        # Get modem info
        result = subprocess.run(['mmcli', '-m', modem_id], capture_output=True, text=True, timeout=5)
        modem_output = result.stdout

        # Parse modem info
        info = {
            'modem_id': modem_id,
            'bars': 0,
            'rssi': None,
            'quality': 0,
            'technology': None,
            'operator': None,
            'imei': None,
            'phone_number': None,
            'state': None
        }

        # Parse state
        state_match = re.search(r'state:\s*\'([^\']+)\'', modem_output, re.IGNORECASE)
        if state_match:
            info['state'] = state_match.group(1)

        # Parse signal quality
        quality_match = re.search(r'signal quality:\s*\'?(\d+)\'?', modem_output, re.IGNORECASE)
        if quality_match:
            info['quality'] = int(quality_match.group(1))
            # Convert quality percentage to bars (0-5)
            quality = info['quality']
            if quality >= 80:
                info['bars'] = 5
            elif quality >= 60:
                info['bars'] = 4
            elif quality >= 40:
                info['bars'] = 3
            elif quality >= 20:
                info['bars'] = 2
            elif quality > 0:
                info['bars'] = 1
            else:
                info['bars'] = 0

        # Parse access technology
        tech_match = re.search(r'access tech(?:nologies)?:\s*\'?([^\'\\n]+)\'?', modem_output, re.IGNORECASE)
        if tech_match:
            info['technology'] = tech_match.group(1).strip()

        # Parse operator
        operator_match = re.search(r'operator name:\s*\'?([^\'\\n]+)\'?', modem_output, re.IGNORECASE)
        if operator_match:
            info['operator'] = operator_match.group(1).strip()

        # Parse IMEI
        imei_match = re.search(r'imei:\s*\'?(\d+)\'?', modem_output, re.IGNORECASE)
        if imei_match:
            info['imei'] = imei_match.group(1)

        # Get SIM info
        result = subprocess.run(['mmcli', '-m', modem_id, '--sim=0'], capture_output=True, text=True, timeout=5)
        sim_output = result.stdout

        # Parse phone number
        phone_match = re.search(r'own number:\s*\'?([^\'\\n]+)\'?', sim_output, re.IGNORECASE)
        if not phone_match:
            phone_match = re.search(r'number:\s*\'?(\+?\d+)\'?', sim_output, re.IGNORECASE)
        if phone_match:
            info['phone_number'] = phone_match.group(1).strip()

        # Parse IMSI
        imsi_match = re.search(r'imsi:\s*\'?(\d+)\'?', sim_output, re.IGNORECASE)
        if imsi_match:
            info['imsi'] = imsi_match.group(1)

        # Parse ICCID
        iccid_match = re.search(r'iccid:\s*\'?(\d+)\'?', sim_output, re.IGNORECASE)
        if iccid_match:
            info['iccid'] = iccid_match.group(1)

        # Get signal details
        result = subprocess.run(['mmcli', '-m', modem_id, '--signal-get'], capture_output=True, text=True, timeout=5)
        signal_output = result.stdout

        # Parse RSSI
        rssi_match = re.search(r'rssi:\s*(-?\d+(?:\.\d+)?)', signal_output, re.IGNORECASE)
        if rssi_match:
            info['rssi'] = float(rssi_match.group(1))

        return info

    except subprocess.TimeoutExpired:
        add_log("Cellular modem query timeout", "WARNING")
        return None
    except Exception as e:
        add_log(f"Cellular modem query error: {e}", "WARNING")
        return None


def cellular_monitor_loop():
    """Background loop to monitor cellular signal."""
    global cellular_running, cellular_signal

    add_log("Cellular signal monitoring started", "INFO")

    while cellular_running:
        try:
            modem_info = get_cellular_modem_info()
            if modem_info:
                cellular_signal.update(modem_info)
                socketio.emit('cellular_update', cellular_signal)
        except Exception as e:
            add_log(f"Cellular monitor error: {e}", "WARNING")

        time.sleep(5)  # Update every 5 seconds

    add_log("Cellular signal monitoring stopped", "INFO")


@app.route('/api/cellular/status')
@login_required
def get_cellular_status():
    """Get cellular modem status and signal quality."""
    modem_info = get_cellular_modem_info()
    if modem_info:
        cellular_signal.update(modem_info)
        return jsonify(cellular_signal)
    return jsonify(cellular_signal)


@app.route('/api/cellular/monitor/start', methods=['POST'])
@login_required
def start_cellular_monitor():
    """Start cellular signal monitoring."""
    global cellular_running, cellular_monitor_thread

    if cellular_running:
        return jsonify({'status': 'already_running'})

    cellular_running = True
    cellular_monitor_thread = threading.Thread(target=cellular_monitor_loop, daemon=True)
    cellular_monitor_thread.start()

    add_log("Cellular monitoring started", "INFO")
    return jsonify({'status': 'started'})


@app.route('/api/cellular/monitor/stop', methods=['POST'])
@login_required
def stop_cellular_monitor():
    """Stop cellular signal monitoring."""
    global cellular_running

    if not cellular_running:
        return jsonify({'status': 'not_running'})

    cellular_running = False
    add_log("Cellular monitoring stopped", "INFO")
    return jsonify({'status': 'stopped'})


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
            'timezone': 'TIMEZONE',
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
        'timezone': CONFIG.get('TIMEZONE', 'UTC'),
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
    """Export comprehensive collection logs for offline analysis."""
    try:
        export_format = request.args.get('format', 'json')
        conn = get_db()
        c = conn.cursor()

        # Get all devices with full details
        try:
            c.execute('''
                SELECT bd_address, device_name, manufacturer, device_type, rssi,
                       first_seen, last_seen, system_lat, system_lon,
                       emitter_lat, emitter_lon, emitter_accuracy, is_target, raw_data
                FROM devices
                ORDER BY last_seen DESC
            ''')
            devices_log = [dict(row) for row in c.fetchall()]
        except Exception as e:
            add_log(f"Export: devices query failed: {e}", "WARNING")
            devices_log = []

        # Get all RSSI history for geolocation data
        try:
            c.execute('''
                SELECT bd_address, rssi, system_lat, system_lon, timestamp
                FROM rssi_history
                ORDER BY timestamp DESC
                LIMIT 10000
            ''')
            rssi_history = [dict(row) for row in c.fetchall()]
        except Exception as e:
            add_log(f"Export: rssi_history query failed: {e}", "WARNING")
            rssi_history = []

        # Get targets list
        try:
            c.execute('SELECT bd_address, alias, notes, priority, created_at FROM targets')
            targets_list = [dict(row) for row in c.fetchall()]
        except Exception as e:
            add_log(f"Export: targets query failed: {e}", "WARNING")
            targets_list = []

        # Get system settings
        try:
            c.execute('SELECT key, value FROM system_settings')
            settings = {row['key']: row['value'] for row in c.fetchall()}
        except Exception as e:
            add_log(f"Export: settings query failed: {e}", "WARNING")
            settings = {}

        conn.close()

        # Build export data (all timestamps in UTC)
        export_data = {
            'export_timestamp': datetime.utcnow().isoformat() + 'Z',
            'system_id': settings.get('SYSTEM_ID', 'UNKNOWN'),
            'devices': devices_log,
            'rssi_history': rssi_history,
            'targets': targets_list,
            'summary': {
                'total_devices': len(devices_log),
                'total_rssi_readings': len(rssi_history),
                'total_targets': len(targets_list),
                'devices_with_location': sum(1 for d in devices_log if d.get('emitter_lat')),
                'targets_detected': sum(1 for d in devices_log if d.get('is_target'))
            }
        }

        if export_format == 'csv':
            # Generate CSV for devices
            import io
            output = io.StringIO()
            output.write('# BlueK9 Collection Export\n')
            output.write(f'# Exported: {export_data["export_timestamp"]}\n')
            output.write(f'# System ID: {export_data["system_id"]}\n')
            output.write('# NOTE: All timestamps are in UTC\n')
            output.write('#\n')
            output.write('# DEVICES\n')
            output.write('bd_address,device_name,manufacturer,device_type,rssi,first_seen,last_seen,')
            output.write('system_lat,system_lon,emitter_lat,emitter_lon,emitter_accuracy,is_target\n')

            for d in devices_log:
                name = (d.get("device_name") or "").replace(",", ";")
                mfr = (d.get("manufacturer") or "").replace(",", ";")
                output.write(f'{d.get("bd_address","")},{name},{mfr},')
                output.write(f'{d.get("device_type","")},{d.get("rssi","")},{d.get("first_seen","")},')
                output.write(f'{d.get("last_seen","")},{d.get("system_lat","")},{d.get("system_lon","")},')
                output.write(f'{d.get("emitter_lat","")},{d.get("emitter_lon","")},{d.get("emitter_accuracy","")},')
                output.write(f'{1 if d.get("is_target") else 0}\n')

            output.write('#\n# RSSI HISTORY\n')
            output.write('bd_address,rssi,system_lat,system_lon,timestamp\n')
            for r in rssi_history:
                output.write(f'{r.get("bd_address","")},{r.get("rssi","")},{r.get("system_lat","")},')
                output.write(f'{r.get("system_lon","")},{r.get("timestamp","")}\n')

            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = f'attachment; filename=bluek9_collection_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            return response

        # Default JSON export - handle any non-serializable values
        def json_serializer(obj):
            if isinstance(obj, bytes):
                return obj.decode('utf-8', errors='replace')
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            return str(obj)

        response = make_response(json.dumps(export_data, indent=2, default=json_serializer))
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = f'attachment; filename=bluek9_collection_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        return response

    except Exception as e:
        add_log(f"Export failed: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


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
    """Handle request for device info and log to database."""
    bd_address = data.get('bd_address')
    interface = data.get('interface', 'hci0')
    if bd_address:
        info = get_device_info(bd_address, interface)

        # Log the device info analysis to database for post-mission analysis
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('''INSERT INTO device_info_logs
                (bd_address, device_name, bluetooth_version, version_description,
                 manufacturer, device_class, device_type_class, features,
                 capabilities, analysis, raw_output, system_lat, system_lon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                bd_address,
                info.get('device_name'),
                info.get('bluetooth_version'),
                info.get('version_description'),
                info.get('manufacturer_info'),
                info.get('device_class'),
                info.get('parsed', {}).get('device_type_class'),
                json.dumps(info.get('features', [])),
                json.dumps(info.get('capabilities', {})),
                json.dumps(info.get('analysis', [])),
                info.get('raw_info'),
                current_location.get('lat') if current_location else None,
                current_location.get('lon') if current_location else None
            ))
            conn.commit()
            add_log(f"Device info logged for {bd_address}", "INFO")
        except Exception as e:
            add_log(f"Failed to log device info: {e}", "WARNING")

        emit('device_info', info)


# ==================== MAIN ====================

def run_app():
    """Main entry point."""
    init_database()

    # Load persisted settings from database
    load_settings_from_db()

    # Start GPS thread
    global gps_thread
    gps_thread = threading.Thread(target=gps_update_loop, daemon=True)
    gps_thread.start()

    add_log("BlueK9 Client starting...", "INFO")
    add_log(f"System ID: {CONFIG.get('SYSTEM_ID')}, GPS Source: {CONFIG.get('GPS_SOURCE')}", "INFO")

    # Run the app
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    run_app()
