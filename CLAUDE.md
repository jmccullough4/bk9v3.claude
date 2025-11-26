# CLAUDE.md - BlueK9 AI Assistant Guidelines

This file provides guidance for AI assistants working with the BlueK9 Bluetooth Surveillance System.

## Project Overview

**Project:** BlueK9
**Purpose:** Tactical Bluetooth detection and tracking system for security/LE operations
**Architecture:** Client/Server model (Client implemented, Server planned)
**Tech Stack:** Python (Flask/SocketIO), JavaScript, HTML/CSS, Docker

## Repository Structure

```
bk9v3.claude/
├── CLAUDE.md              # AI assistant guidelines (this file)
├── README.md              # Project description
├── Dockerfile             # Docker container definition
├── docker-compose.yml     # Docker Compose configuration
├── client/                # BlueK9 Client application
│   ├── app.py             # Main Flask application
│   ├── requirements.txt   # Python dependencies
│   ├── templates/         # HTML templates
│   │   ├── login.html     # Login page
│   │   └── main.html      # Main application UI
│   └── static/            # Static assets
│       ├── css/
│       │   └── main.css   # Military/LE themed styles
│       ├── js/
│       │   └── main.js    # Frontend JavaScript
│       └── sounds/        # Alert sounds
├── scripts/               # Installation and startup scripts
│   ├── install.sh         # System installation script
│   └── start.sh           # Application start script
├── logs/                  # Application logs
└── data/                  # Persistent data (database)
```

## Key Components

### Backend (client/app.py)

The Flask-based backend handles:

- **Bluetooth Scanning**: Classic and BLE device detection using BlueZ tools
- **GPS Tracking**: Location via gpsd or direct serial GPS devices
- **Geolocation Algorithm**: Weighted centroid estimation with CEP plotting
- **SMS Alerts**: Via ModemManager (mmcli) for SIMCOM7600
- **WebSocket Updates**: Real-time device updates to UI
- **Target Management**: Track specific BD addresses of interest
- **Radio Management**: Enable/disable Bluetooth and WiFi adapters
- **Data Logging**: SQLite database for device history

### Frontend (client/static/js/main.js)

JavaScript handles:

- **Mapbox Integration**: Dark/Streets/Satellite views
- **Real-time Updates**: WebSocket communication
- **Device Survey Table**: Sortable, target-highlighted
- **CEP Circle Plotting**: Circular error probable visualization
- **Alert System**: Audio/visual target alerts
- **Statistics Charts**: Device type distribution

### Styling (client/static/css/main.css)

Military/LE themed UI with:

- Dark color scheme with cyan accents
- Monospace fonts for tactical look
- Target highlighting in red
- Responsive three-panel layout

## Commands

### Installation

```bash
# Install all dependencies
sudo ./scripts/install.sh
```

### Running the Application

```bash
# Option 1: Direct (recommended for development)
cd client
source venv/bin/activate
sudo python app.py

# Option 2: Start script
sudo ./scripts/start.sh

# Option 3: Docker
sudo docker-compose up -d

# Option 4: Systemd service
sudo systemctl start bluek9
```

### Access

- **URL**: http://localhost:5000
- **Login**: bluek9 / warhammer

## Development Guidelines

### Code Style

- **Python**: Follow PEP 8, use type hints where helpful
- **JavaScript**: ES6+, use const/let appropriately
- **CSS**: BEM-like naming, CSS custom properties for theming
- **Comments**: Only where logic isn't self-evident

### Key Files to Understand

1. `client/app.py` - Core backend logic, Bluetooth scanning, API routes
2. `client/static/js/main.js` - Frontend state management, map handling
3. `client/static/css/main.css` - UI theming and layout

### Adding New Features

1. Backend APIs go in `app.py` with `/api/` prefix
2. WebSocket events for real-time data
3. Update frontend JS to handle new data
4. Maintain military/LE aesthetic in CSS

### Bluetooth Scanning Functions

- `scan_classic_bluetooth()` - Standard inquiry scan
- `scan_ble_devices()` - Low Energy scan via btmgmt
- `stimulate_bluetooth_classic()` - Enhanced inquiry modes
- `stimulate_ble_devices()` - Active BLE scanning
- `get_device_info()` - Detailed device query

### Geolocation Algorithm

Located in `estimate_emitter_location()`:
- Uses RSSI history from multiple positions
- Weighted centroid calculation (inverse distance weighting)
- Returns (lat, lon, CEP_radius)

## Dependencies

### System (installed via install.sh)

- `bluez`, `bluetooth` - Bluetooth stack
- `gpsd` - GPS daemon
- `modemmanager` - SMS via cellular modem
- `docker`, `docker-compose` - Containerization

### Python (requirements.txt)

- `Flask` - Web framework
- `Flask-SocketIO` - WebSocket support
- `pyserial` - GPS serial communication
- `eventlet` - Async support

### Frontend (CDN)

- Mapbox GL JS - Map rendering
- Socket.IO client - WebSocket
- Chart.js - Statistics charts

## Hardware Requirements

- **Bluetooth Adapter**: Sena UD100 (primary), any BlueZ-compatible
- **GPS**: USB GPS receiver (gpsd-compatible)
- **Cellular Modem**: SIMCOM7600 for SMS alerts
- **WiFi Adapter**: Optional, for future WiFi scanning

## API Reference

### Scan Control
- `POST /api/scan/start` - Start scanning
- `POST /api/scan/stop` - Stop scanning
- `POST /api/scan/stimulate` - Run stimulation scan

### Devices
- `GET /api/devices` - List all detected devices
- `POST /api/devices/clear` - Clear device list
- `GET /api/device/<bd_address>/info` - Get device details

### Targets
- `GET /api/targets` - List targets
- `POST /api/targets` - Add target
- `DELETE /api/targets/<bd_address>` - Remove target

### SMS
- `GET /api/sms/numbers` - List SMS numbers
- `POST /api/sms/numbers` - Add number
- `DELETE /api/sms/numbers/<id>` - Remove number

### Radios
- `GET /api/radios` - List available radios
- `POST /api/radios/<interface>/enable` - Enable radio
- `POST /api/radios/<interface>/disable` - Disable radio

### GPS
- `GET /api/gps` - Get current location
- `POST /api/gps/follow` - Toggle GPS following

### Logs
- `GET /api/logs` - Get recent logs
- `GET /api/logs/export` - Export device logs

## WebSocket Events

### Server -> Client
- `device_update` - Device detected/updated
- `devices_list` - Full device list
- `devices_cleared` - Devices cleared
- `gps_update` - Location update
- `log_update` - New log entry
- `target_alert` - Target detected
- `device_info` - Device info response

### Client -> Server
- `request_device_info` - Request device details

## Future Development (Server)

The server component (planned) will handle:
- Target deck synchronization across clients
- Centralized logging and analysis
- Multi-client coordination
- TLS/SSL secure communication
- Web-based administration

## Security Notes

- Runs as root for Bluetooth hardware access
- Change default credentials in production
- Use TLS when exposing to network
- Review mmcli SMS permissions

---

*Last updated: 2025-11-26*
