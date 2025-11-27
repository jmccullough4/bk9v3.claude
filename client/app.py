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
btmon_device_cache = {}  # BD Address -> full device info from btmon

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


def stimulate_ble_devices(interface='hci0'):
    """
    Stimulate BLE devices to respond using active LE scanning.
    Uses hcitool lescan which sends SCAN_REQ to get SCAN_RSP from devices.
    """
    try:
        add_log("Stimulating BLE devices (active LE scan)...", "INFO")
        devices_found = []
        seen_addresses = set()

        # Ensure interface is up
        subprocess.run(['hciconfig', interface, 'up'], capture_output=True)
        subprocess.run(['hciconfig', interface, 'noscan'], capture_output=True)

        # Method 1: Use hcitool lescan with timeout
        # lescan performs active scanning which solicits SCAN_RSP from devices
        try:
            result = subprocess.run(
                ['timeout', '8', 'hcitool', '-i', interface, 'lescan'],
                capture_output=True,
                text=True
            )

            # Parse lescan output: "AA:BB:CC:DD:EE:FF DeviceName" or "AA:BB:CC:DD:EE:FF (unknown)"
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue

                # Match BD address at start of line
                match = re.match(r'([0-9A-Fa-f:]{17})\s*(.*)', line)
                if match:
                    bd_addr = match.group(1).upper()
                    name_part = match.group(2).strip()

                    if bd_addr in seen_addresses:
                        continue
                    seen_addresses.add(bd_addr)

                    # Determine name
                    if name_part and name_part != '(unknown)':
                        device_name = name_part
                    else:
                        device_name = 'BLE Device'

                    devices_found.append({
                        'bd_address': bd_addr,
                        'device_name': device_name,
                        'device_type': 'ble',
                        'rssi': None,  # lescan doesn't provide RSSI
                        'manufacturer': get_manufacturer(bd_addr)
                    })

        except Exception as e:
            add_log(f"hcitool lescan failed: {e}", "WARNING")

        # Method 2: Also try bluetoothctl for additional devices
        if len(devices_found) < 3:
            try:
                # Use bluetoothctl scan for 6 seconds
                proc = subprocess.Popen(
                    ['bluetoothctl'],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                commands = "scan on\n"
                time.sleep(0.5)
                proc.stdin.write(commands)
                proc.stdin.flush()
                time.sleep(6)
                proc.stdin.write("scan off\nexit\n")
                proc.stdin.flush()
                stdout, _ = proc.communicate(timeout=3)

                # Parse bluetoothctl output for NEW devices
                # Format: [NEW] Device AA:BB:CC:DD:EE:FF DeviceName
                for line in stdout.splitlines():
                    match = re.search(r'\[NEW\]\s+Device\s+([0-9A-Fa-f:]{17})\s*(.*)', line)
                    if match:
                        bd_addr = match.group(1).upper()
                        name_part = match.group(2).strip()

                        if bd_addr in seen_addresses:
                            continue
                        seen_addresses.add(bd_addr)

                        devices_found.append({
                            'bd_address': bd_addr,
                            'device_name': name_part if name_part else 'BLE Device',
                            'device_type': 'ble',
                            'rssi': None,
                            'manufacturer': get_manufacturer(bd_addr)
                        })

            except Exception as e:
                add_log(f"bluetoothctl scan failed: {e}", "WARNING")

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

        # Parse device name if available
        name_match = re.search(r'Device Name:\s*(.+)', result.stdout)
        if name_match:
            info['device_name'] = name_match.group(1).strip()
            device_responded = True

        # Parse device class if available
        class_match = re.search(r'Class:\s*(0x[0-9A-Fa-f]+)', result.stdout)
        if class_match:
            info['device_class'] = class_match.group(1)
            device_responded = True

        # Parse manufacturer
        mfr_match = re.search(r'Manufacturer:\s*(.+)', result.stdout)
        if mfr_match:
            info['manufacturer_info'] = mfr_match.group(1).strip()
            device_responded = True

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
    # Enforce minimum of 10m to ensure containment
    cep_radius = sorted(distances)[len(distances) // 2] if distances else 50
    cep_radius = max(10, cep_radius)

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
    session = active_geo_sessions.get(bd_address, {})

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

                # Update RSSI in devices dictionary for survey table
                if bd_address in devices:
                    devices[bd_address]['rssi'] = rssi
                    devices[bd_address]['last_seen'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                # Emit real-time ping data to UI
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
                    'methods': methods
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

    # Also add device to survey if not present
    if bd_address not in devices:
        device_data = {
            'bd_address': bd_address,
            'device_name': None,
            'device_type': 'classic',
            'manufacturer': get_manufacturer(bd_address)
        }
        process_found_device(device_data)

    return {'status': 'started', 'bd_address': bd_address, 'methods': methods}


def stop_active_geo(bd_address):
    """Stop active geo tracking for a device."""
    global active_geo_sessions

    if bd_address in active_geo_sessions:
        active_geo_sessions[bd_address]['active'] = False
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


@app.route('/api/device/<bd_address>/geo/track', methods=['POST'])
@login_required
def start_device_geo_track(bd_address):
    """Start active geo tracking with selectable methods."""
    bd_address = bd_address.upper()
    data = request.get_json(silent=True) or {}
    interface = data.get('interface', 'hci0')
    methods = data.get('methods', ['l2ping', 'rssi'])

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
    cep = min(avg_distance * 0.5 + std_dev * 2.0, 200)

    # Reduce CEP with more observations (better confidence)
    cep = cep * math.sqrt(10 / len(observations))

    # Enforce minimum CEP of 10m to ensure containment
    cep = max(10, cep)

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
    export_format = request.args.get('format', 'json')
    conn = get_db()
    c = conn.cursor()

    # Get all devices with full details
    c.execute('''
        SELECT bd_address, device_name, manufacturer, device_type, rssi,
               first_seen, last_seen, system_lat, system_lon,
               emitter_lat, emitter_lon, emitter_accuracy, is_target, raw_data
        FROM devices
        ORDER BY last_seen DESC
    ''')
    devices_log = [dict(row) for row in c.fetchall()]

    # Get all RSSI history for geolocation data
    c.execute('''
        SELECT bd_address, rssi, system_lat, system_lon, timestamp
        FROM rssi_history
        ORDER BY timestamp DESC
    ''')
    rssi_history = [dict(row) for row in c.fetchall()]

    # Get targets list
    c.execute('SELECT bd_address, notes, added_at FROM targets')
    targets_list = [dict(row) for row in c.fetchall()]

    # Get system settings
    c.execute('SELECT key, value FROM system_settings')
    settings = {row['key']: row['value'] for row in c.fetchall()}

    conn.close()

    # Build export data
    export_data = {
        'export_timestamp': datetime.now().isoformat(),
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
        output.write('#\n')
        output.write('# DEVICES\n')
        output.write('bd_address,device_name,manufacturer,device_type,rssi,first_seen,last_seen,')
        output.write('system_lat,system_lon,emitter_lat,emitter_lon,emitter_accuracy,is_target\n')

        for d in devices_log:
            output.write(f'{d.get("bd_address","")},{d.get("device_name","").replace(",", ";") if d.get("device_name") else ""},')
            output.write(f'{d.get("manufacturer","").replace(",", ";") if d.get("manufacturer") else ""},')
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

    # Default JSON export
    response = make_response(json.dumps(export_data, indent=2))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Content-Disposition'] = f'attachment; filename=bluek9_collection_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    return response


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
