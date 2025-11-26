# BlueK9 - Bluetooth Surveillance System

A tactical Bluetooth detection and tracking system designed for security and law enforcement operations.

## Features

- **Dual-Mode Bluetooth Detection**: Scan for both Classic Bluetooth and BLE devices
- **Real-time Mapping**: Mapbox integration with Dark/Streets/Satellite views
- **Geolocation Algorithm**: CEP (Circular Error Probable) plotting for emitter location estimation
- **Target Management**: Track specific devices with visual/audio alerts
- **SMS Notifications**: Alert up to 10 phone numbers via SIMCOM7600 cellular modem
- **Radio Management**: Add/remove Bluetooth and WiFi adapters dynamically
- **Comprehensive Logging**: Full device history for post-mission analysis
- **Military/LE Themed UI**: Professional, clean interface optimized for operations

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/jmccullough4/bk9v3.claude.git
cd bk9v3.claude

# Run the installer (Debian/Ubuntu)
sudo ./scripts/install.sh
```

### Running

```bash
# Using start script
sudo ./scripts/start.sh

# Or using Docker
sudo docker-compose up -d
```

### Access

- **URL**: http://localhost:5000
- **Username**: bluek9
- **Password**: warhammer

## Hardware Requirements

- Bluetooth adapter (Sena UD100 recommended)
- GPS receiver (gpsd-compatible)
- SIMCOM7600 cellular modem (for SMS alerts)
- Linux system with BlueZ support

## Documentation

See [CLAUDE.md](CLAUDE.md) for detailed technical documentation.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      BlueK9 Client                          │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Web UI     │  │  Flask API  │  │  Bluetooth Scanner  │  │
│  │  (Mapbox)   │◄─┤  (SocketIO) │◄─┤  (BlueZ/hcitool)    │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         │               │                    │              │
│         ▼               ▼                    ▼              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Charts &   │  │  SQLite DB  │  │  GPS & SMS Module   │  │
│  │  Survey     │  │  (Logging)  │  │  (gpsd/mmcli)       │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## License

Proprietary - For authorized use only.

## Disclaimer

This tool is intended for authorized security testing, law enforcement operations, and educational purposes only. Users are responsible for ensuring compliance with all applicable laws and regulations.
