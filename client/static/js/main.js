/**
 * BlueK9 Client - Frontend JavaScript
 * Tactical Bluetooth Surveillance System
 */

// Global state
let map = null;
let socket = null;
let devices = {};
let targets = {};
let markers = {};
let cepCircles = {};
let systemMarker = null;
let followGps = true;
let showCep = true;
let scanning = false;
let deviceTypeChart = null;
let currentMapStyle = 'dark';

// Map styles
const MAP_STYLES = {
    dark: 'mapbox://styles/mapbox/dark-v11',
    streets: 'mapbox://styles/mapbox/streets-v12',
    satellite: 'mapbox://styles/mapbox/satellite-streets-v12'
};

/**
 * Initialize the application
 */
function initApp() {
    // Initialize Mapbox
    mapboxgl.accessToken = MAPBOX_TOKEN;

    map = new mapboxgl.Map({
        container: 'map',
        style: MAP_STYLES.dark,
        center: [-98.5795, 39.8283], // US center as default
        zoom: 4
    });

    map.addControl(new mapboxgl.NavigationControl());
    map.addControl(new mapboxgl.ScaleControl());

    // Initialize WebSocket
    initWebSocket();

    // Initialize chart
    initChart();

    // Load initial data
    loadTargets();
    loadSmsNumbers();
    loadRadios();
    loadLogs();
    loadGpsConfig();

    // Update time display
    setInterval(updateTime, 1000);
    updateTime();

    // Map click handler
    map.on('click', (e) => {
        // Clicked on empty map area
        document.getElementById('selectedDevice').textContent = 'NONE';
    });
}

/**
 * Initialize WebSocket connection
 */
function initWebSocket() {
    socket = io();

    socket.on('connect', () => {
        addLogEntry('Connected to server', 'INFO');
    });

    socket.on('disconnect', () => {
        addLogEntry('Disconnected from server', 'WARNING');
        updateScanStatus(false);
    });

    socket.on('device_update', (device) => {
        updateDevice(device);
    });

    socket.on('devices_list', (deviceList) => {
        deviceList.forEach(device => updateDevice(device));
    });

    socket.on('devices_cleared', () => {
        clearAllDevices();
    });

    socket.on('gps_update', (location) => {
        updateSystemLocation(location);
    });

    socket.on('log_update', (entry) => {
        addLogEntry(entry.message, entry.level);
    });

    socket.on('target_alert', (data) => {
        showTargetAlert(data.device);
    });

    socket.on('device_info', (info) => {
        showDeviceInfo(info);
    });
}

/**
 * Initialize statistics chart
 */
function initChart() {
    const ctx = document.getElementById('deviceTypeChart').getContext('2d');
    deviceTypeChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Classic', 'BLE', 'Targets'],
            datasets: [{
                data: [0, 0, 0],
                backgroundColor: [
                    'rgba(0, 212, 255, 0.8)',
                    'rgba(48, 209, 88, 0.8)',
                    'rgba(255, 59, 48, 0.8)'
                ],
                borderColor: [
                    'rgba(0, 212, 255, 1)',
                    'rgba(48, 209, 88, 1)',
                    'rgba(255, 59, 48, 1)'
                ],
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: {
                        color: '#8b949e',
                        font: {
                            family: 'Share Tech Mono',
                            size: 10
                        }
                    }
                }
            }
        }
    });
}

/**
 * Update device in the UI
 */
function updateDevice(device) {
    const bdAddr = device.bd_address;
    devices[bdAddr] = device;

    // Update survey table
    updateSurveyTable();

    // Update map marker
    updateDeviceMarker(device);

    // Update statistics
    updateStats();

    // Update counters
    document.getElementById('deviceCount').textContent = Object.keys(devices).length;
    document.getElementById('surveyCount').textContent = Object.keys(devices).length;

    const targetCount = Object.values(devices).filter(d => d.is_target).length;
    document.getElementById('targetCount').textContent = targetCount;
}

/**
 * Update the survey table
 */
function updateSurveyTable() {
    const tbody = document.getElementById('surveyTableBody');
    tbody.innerHTML = '';

    // Sort devices: targets first, then by last_seen
    const sortedDevices = Object.values(devices).sort((a, b) => {
        if (a.is_target && !b.is_target) return -1;
        if (!a.is_target && b.is_target) return 1;
        return new Date(b.last_seen) - new Date(a.last_seen);
    });

    sortedDevices.forEach(device => {
        const row = document.createElement('tr');
        if (device.is_target) {
            row.classList.add('target-row');
        }

        const emitterLoc = device.emitter_lat && device.emitter_lon
            ? `${device.emitter_lat.toFixed(4)}, ${device.emitter_lon.toFixed(4)}`
            : '--';

        const lastSeen = device.last_seen
            ? new Date(device.last_seen).toLocaleTimeString()
            : '--';

        row.innerHTML = `
            <td class="bd-address">${device.bd_address}</td>
            <td title="${device.device_name || 'Unknown'}">${truncate(device.device_name || 'Unknown', 12)}</td>
            <td title="${device.manufacturer || 'Unknown'}">${truncate(device.manufacturer || '?', 8)}</td>
            <td>${device.rssi || '--'}</td>
            <td>${emitterLoc}</td>
            <td>${lastSeen}</td>
            <td>
                <button class="action-btn" onclick="getDeviceInfo('${device.bd_address}')" title="Get Info">i</button>
                ${!device.is_target ? `<button class="action-btn" onclick="quickAddTarget('${device.bd_address}')" title="Add as Target">+</button>` : ''}
            </td>
        `;

        row.addEventListener('click', (e) => {
            if (e.target.tagName !== 'BUTTON') {
                selectDevice(device);
            }
        });

        // Right-click context menu
        row.addEventListener('contextmenu', (e) => {
            showContextMenu(e, device.bd_address);
        });

        tbody.appendChild(row);
    });
}

/**
 * Update device marker on map
 */
function updateDeviceMarker(device) {
    const bdAddr = device.bd_address;

    // Remove existing marker
    if (markers[bdAddr]) {
        markers[bdAddr].remove();
    }

    // Remove existing CEP circle
    if (cepCircles[bdAddr] && map.getSource(`cep-${bdAddr}`)) {
        map.removeLayer(`cep-${bdAddr}`);
        map.removeSource(`cep-${bdAddr}`);
        delete cepCircles[bdAddr];
    }

    // Only add marker if we have location
    if (!device.emitter_lat || !device.emitter_lon) return;

    // Create marker element
    const el = document.createElement('div');
    el.className = device.is_target ? 'target-marker' : 'device-marker';

    // Create marker
    const marker = new mapboxgl.Marker(el)
        .setLngLat([device.emitter_lon, device.emitter_lat])
        .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML(`
            <strong>${device.bd_address}</strong><br>
            Name: ${device.device_name || 'Unknown'}<br>
            RSSI: ${device.rssi || '--'} dBm<br>
            Last Seen: ${device.last_seen || '--'}
        `))
        .addTo(map);

    markers[bdAddr] = marker;

    // Add CEP circle if enabled and we have accuracy
    if (showCep && device.emitter_accuracy) {
        addCepCircle(device);
    }

    // Click handler
    el.addEventListener('click', () => selectDevice(device));
}

/**
 * Add CEP (Circular Error Probable) circle
 */
function addCepCircle(device) {
    const bdAddr = device.bd_address;
    const radius = device.emitter_accuracy || 50; // meters

    // Create circle GeoJSON
    const circle = createGeoJSONCircle(
        [device.emitter_lon, device.emitter_lat],
        radius / 1000 // Convert to km
    );

    const sourceId = `cep-${bdAddr}`;
    const color = device.is_target ? '#ff3b30' : '#00d4ff';

    map.addSource(sourceId, {
        type: 'geojson',
        data: circle
    });

    map.addLayer({
        id: sourceId,
        type: 'fill',
        source: sourceId,
        paint: {
            'fill-color': color,
            'fill-opacity': 0.15,
            'fill-outline-color': color
        }
    });

    cepCircles[bdAddr] = true;
}

/**
 * Create a GeoJSON circle
 */
function createGeoJSONCircle(center, radiusKm, points = 64) {
    const coords = {
        latitude: center[1],
        longitude: center[0]
    };

    const km = radiusKm;
    const ret = [];
    const distanceX = km / (111.32 * Math.cos(coords.latitude * Math.PI / 180));
    const distanceY = km / 110.574;

    for (let i = 0; i < points; i++) {
        const theta = (i / points) * (2 * Math.PI);
        const x = distanceX * Math.cos(theta);
        const y = distanceY * Math.sin(theta);
        ret.push([coords.longitude + x, coords.latitude + y]);
    }
    ret.push(ret[0]);

    return {
        type: 'Feature',
        geometry: {
            type: 'Polygon',
            coordinates: [ret]
        }
    };
}

/**
 * Update system location marker
 */
function updateSystemLocation(location) {
    if (!location.lat || !location.lon) return;

    document.getElementById('sysLocation').textContent =
        `${location.lat.toFixed(5)}, ${location.lon.toFixed(5)}`;

    // Update or create system marker
    if (systemMarker) {
        systemMarker.setLngLat([location.lon, location.lat]);
    } else {
        const el = document.createElement('div');
        el.className = 'system-marker';

        systemMarker = new mapboxgl.Marker(el)
            .setLngLat([location.lon, location.lat])
            .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML('<strong>SYSTEM LOCATION</strong>'))
            .addTo(map);
    }

    // Follow GPS if enabled
    if (followGps) {
        map.flyTo({
            center: [location.lon, location.lat],
            zoom: Math.max(map.getZoom(), 15),
            duration: 1000
        });
    }
}

/**
 * Select a device
 */
function selectDevice(device) {
    document.getElementById('selectedDevice').textContent = device.bd_address;

    // Center map on device if it has location
    if (device.emitter_lat && device.emitter_lon) {
        map.flyTo({
            center: [device.emitter_lon, device.emitter_lat],
            zoom: 17,
            duration: 1000
        });

        // Open popup
        if (markers[device.bd_address]) {
            markers[device.bd_address].togglePopup();
        }
    }
}

/**
 * Update statistics
 */
function updateStats() {
    let classic = 0;
    let ble = 0;
    let targetCount = 0;

    Object.values(devices).forEach(device => {
        if (device.is_target) targetCount++;
        if (device.device_type === 'classic') classic++;
        else if (device.device_type === 'ble') ble++;
    });

    deviceTypeChart.data.datasets[0].data = [classic, ble, targetCount];
    deviceTypeChart.update();
}

/**
 * Clear all devices
 */
function clearAllDevices() {
    // Remove all markers
    Object.values(markers).forEach(marker => marker.remove());
    markers = {};

    // Remove all CEP circles
    Object.keys(cepCircles).forEach(bdAddr => {
        const sourceId = `cep-${bdAddr}`;
        if (map.getLayer(sourceId)) map.removeLayer(sourceId);
        if (map.getSource(sourceId)) map.removeSource(sourceId);
    });
    cepCircles = {};

    // Clear devices
    devices = {};

    // Update UI
    updateSurveyTable();
    updateStats();
    document.getElementById('deviceCount').textContent = '0';
    document.getElementById('surveyCount').textContent = '0';
    document.getElementById('targetCount').textContent = '0';
}

// ==================== SCAN CONTROLS ====================

function startScan() {
    fetch('/api/scan/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            updateScanStatus(true);
            addLogEntry('Scan started', 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to start scan: ' + error, 'ERROR');
        });
}

function stopScan() {
    fetch('/api/scan/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            updateScanStatus(false);
            addLogEntry('Scan stopped', 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to stop scan: ' + error, 'ERROR');
        });
}

function stimulateScan(type) {
    addLogEntry(`Starting ${type} stimulation scan...`, 'INFO');
    fetch('/api/scan/stimulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: type })
    })
        .then(response => response.json())
        .then(data => {
            addLogEntry(`Stimulation complete: ${data.count} devices found`, 'INFO');
        })
        .catch(error => {
            addLogEntry('Stimulation failed: ' + error, 'ERROR');
        });
}

function clearResults() {
    fetch('/api/devices/clear', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            addLogEntry('Results cleared', 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to clear results: ' + error, 'ERROR');
        });
}

function updateScanStatus(isScanning) {
    scanning = isScanning;
    const indicator = document.getElementById('statusIndicator');
    const statusText = indicator.querySelector('.status-text');
    const startBtn = document.getElementById('btnStartScan');
    const stopBtn = document.getElementById('btnStopScan');

    if (isScanning) {
        indicator.classList.add('active');
        statusText.textContent = 'SCANNING';
        startBtn.disabled = true;
        stopBtn.disabled = false;
    } else {
        indicator.classList.remove('active');
        statusText.textContent = 'STANDBY';
        startBtn.disabled = false;
        stopBtn.disabled = true;
    }
}

// ==================== MAP CONTROLS ====================

function setMapStyle(style) {
    if (currentMapStyle === style) return;

    currentMapStyle = style;
    map.setStyle(MAP_STYLES[style]);

    // Update button states
    document.querySelectorAll('.btn-map').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.style === style);
    });

    // Re-add markers after style loads
    map.once('styledata', () => {
        // Re-add system marker
        if (systemMarker) {
            const lngLat = systemMarker.getLngLat();
            systemMarker.remove();
            const el = document.createElement('div');
            el.className = 'system-marker';
            systemMarker = new mapboxgl.Marker(el)
                .setLngLat(lngLat)
                .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML('<strong>SYSTEM LOCATION</strong>'))
                .addTo(map);
        }

        // Re-add device markers
        Object.values(devices).forEach(device => {
            if (device.emitter_lat && device.emitter_lon) {
                updateDeviceMarker(device);
            }
        });
    });
}

function toggleFollowGps() {
    followGps = document.getElementById('toggleFollowGps').checked;
    fetch('/api/gps/follow', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ follow: followGps })
    });
}

function toggleShowCep() {
    showCep = document.getElementById('toggleShowCep').checked;

    if (showCep) {
        // Add CEP circles for all devices
        Object.values(devices).forEach(device => {
            if (device.emitter_lat && device.emitter_lon && device.emitter_accuracy) {
                addCepCircle(device);
            }
        });
    } else {
        // Remove all CEP circles
        Object.keys(cepCircles).forEach(bdAddr => {
            const sourceId = `cep-${bdAddr}`;
            if (map.getLayer(sourceId)) map.removeLayer(sourceId);
            if (map.getSource(sourceId)) map.removeSource(sourceId);
        });
        cepCircles = {};
    }
}

// ==================== TARGET MANAGEMENT ====================

function loadTargets() {
    fetch('/api/targets')
        .then(response => response.json())
        .then(data => {
            targets = {};
            data.forEach(target => {
                targets[target.bd_address] = target;
            });
            updateTargetList();
        });
}

function updateTargetList() {
    const list = document.getElementById('targetList');
    list.innerHTML = '';

    Object.values(targets).forEach(target => {
        const item = document.createElement('div');
        item.className = 'target-item';
        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary">${target.bd_address}</span>
                <span class="item-secondary">${target.alias || 'No alias'}</span>
            </div>
            <button class="item-delete" onclick="deleteTarget('${target.bd_address}')">&times;</button>
        `;
        list.appendChild(item);
    });
}

function addTarget() {
    const bdAddress = document.getElementById('targetBdAddress').value.trim().toUpperCase();
    const alias = document.getElementById('targetAlias').value.trim();

    if (!bdAddress.match(/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/)) {
        addLogEntry('Invalid BD Address format', 'ERROR');
        return;
    }

    fetch('/api/targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bd_address: bdAddress, alias: alias })
    })
        .then(response => response.json())
        .then(data => {
            loadTargets();
            document.getElementById('targetBdAddress').value = '';
            document.getElementById('targetAlias').value = '';
            addLogEntry(`Target added: ${bdAddress}`, 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to add target: ' + error, 'ERROR');
        });
}

function quickAddTarget(bdAddress) {
    fetch('/api/targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bd_address: bdAddress })
    })
        .then(response => response.json())
        .then(data => {
            loadTargets();
            // Update device in local cache
            if (devices[bdAddress]) {
                devices[bdAddress].is_target = true;
                updateDevice(devices[bdAddress]);
            }
            addLogEntry(`Target added: ${bdAddress}`, 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to add target: ' + error, 'ERROR');
        });
}

function deleteTarget(bdAddress) {
    fetch(`/api/targets/${bdAddress}`, { method: 'DELETE' })
        .then(response => response.json())
        .then(data => {
            delete targets[bdAddress];
            updateTargetList();
            // Update device in local cache
            if (devices[bdAddress]) {
                devices[bdAddress].is_target = false;
                updateDevice(devices[bdAddress]);
            }
            addLogEntry(`Target removed: ${bdAddress}`, 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to remove target: ' + error, 'ERROR');
        });
}

// ==================== SMS MANAGEMENT ====================

function loadSmsNumbers() {
    fetch('/api/sms/numbers')
        .then(response => response.json())
        .then(data => {
            updateSmsList(data);
        });
}

function updateSmsList(numbers) {
    const list = document.getElementById('smsList');
    list.innerHTML = '';

    numbers.forEach(num => {
        const item = document.createElement('div');
        item.className = 'sms-item';
        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary">${num.phone_number}</span>
            </div>
            <button class="item-delete" onclick="deleteSmsNumber(${num.id})">&times;</button>
        `;
        list.appendChild(item);
    });
}

function addSmsNumber() {
    const phone = document.getElementById('smsNumber').value.trim();
    if (!phone) return;

    fetch('/api/sms/numbers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone_number: phone })
    })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                addLogEntry(data.error, 'ERROR');
            } else {
                loadSmsNumbers();
                document.getElementById('smsNumber').value = '';
                addLogEntry(`SMS number added: ${phone}`, 'INFO');
            }
        })
        .catch(error => {
            addLogEntry('Failed to add SMS number: ' + error, 'ERROR');
        });
}

function deleteSmsNumber(id) {
    fetch(`/api/sms/numbers/${id}`, { method: 'DELETE' })
        .then(response => response.json())
        .then(data => {
            loadSmsNumbers();
            addLogEntry('SMS number removed', 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to remove SMS number: ' + error, 'ERROR');
        });
}

// ==================== RADIO MANAGEMENT ====================

function loadRadios() {
    fetch('/api/radios')
        .then(response => response.json())
        .then(data => {
            updateRadioList(data);
        });
}

function updateRadioList(radios) {
    const list = document.getElementById('radioList');
    list.innerHTML = '';

    // Bluetooth radios
    radios.bluetooth.forEach(radio => {
        const item = document.createElement('div');
        item.className = `radio-item ${radio.status === 'up' ? '' : 'inactive'}`;
        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary">${radio.interface} (BT)</span>
                <span class="item-secondary">${radio.bd_address}</span>
            </div>
            <button class="action-btn" onclick="toggleRadio('${radio.interface}', 'bluetooth', '${radio.status}')">
                ${radio.status === 'up' ? 'OFF' : 'ON'}
            </button>
        `;
        list.appendChild(item);
    });

    // WiFi radios
    radios.wifi.forEach(radio => {
        const item = document.createElement('div');
        item.className = `radio-item ${radio.status === 'up' ? '' : 'inactive'}`;
        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary">${radio.interface} (WiFi)</span>
                <span class="item-secondary">${radio.mac_address}</span>
            </div>
            <button class="action-btn" onclick="toggleRadio('${radio.interface}', 'wifi', '${radio.status}')">
                ${radio.status === 'up' ? 'OFF' : 'ON'}
            </button>
        `;
        list.appendChild(item);
    });
}

function toggleRadio(iface, type, currentStatus) {
    const action = currentStatus === 'up' ? 'disable' : 'enable';
    fetch(`/api/radios/${iface}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: type })
    })
        .then(response => response.json())
        .then(data => {
            loadRadios();
            addLogEntry(`Radio ${iface} ${action}d`, 'INFO');
        })
        .catch(error => {
            addLogEntry(`Failed to ${action} radio: ` + error, 'ERROR');
        });
}

function refreshRadios() {
    loadRadios();
    addLogEntry('Radio list refreshed', 'INFO');
}

// ==================== GPS CONFIGURATION ====================

function loadGpsConfig() {
    fetch('/api/gps/config')
        .then(response => response.json())
        .then(config => {
            // Set source dropdown
            document.getElementById('gpsSource').value = config.source;

            // Set NMEA settings
            document.getElementById('nmeaHost').value = config.nmea_host;
            document.getElementById('nmeaPort').value = config.nmea_port;

            // Set GPSD settings
            document.getElementById('gpsdHost').value = config.gpsd_host;
            document.getElementById('gpsdPort').value = config.gpsd_port;

            // Set Serial settings
            document.getElementById('serialPort').value = config.serial_port;
            document.getElementById('serialBaud').value = config.serial_baud;

            // Show correct settings panel
            updateGpsFields();

            // Update GPS status
            updateGpsStatus(config.current_location);
        })
        .catch(error => {
            addLogEntry('Failed to load GPS config: ' + error, 'ERROR');
        });
}

function updateGpsFields() {
    const source = document.getElementById('gpsSource').value;

    // Hide all settings panels
    document.getElementById('nmeaSettings').classList.add('hidden');
    document.getElementById('gpsdSettings').classList.add('hidden');
    document.getElementById('serialSettings').classList.add('hidden');

    // Show the selected one
    if (source === 'nmea_tcp') {
        document.getElementById('nmeaSettings').classList.remove('hidden');
    } else if (source === 'gpsd') {
        document.getElementById('gpsdSettings').classList.remove('hidden');
    } else if (source === 'serial') {
        document.getElementById('serialSettings').classList.remove('hidden');
    }
}

function saveGpsConfig() {
    const source = document.getElementById('gpsSource').value;

    const config = {
        source: source,
        nmea_host: document.getElementById('nmeaHost').value,
        nmea_port: parseInt(document.getElementById('nmeaPort').value),
        gpsd_host: document.getElementById('gpsdHost').value,
        gpsd_port: parseInt(document.getElementById('gpsdPort').value),
        serial_port: document.getElementById('serialPort').value,
        serial_baud: parseInt(document.getElementById('serialBaud').value)
    };

    fetch('/api/gps/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
    })
        .then(response => response.json())
        .then(data => {
            addLogEntry(`GPS config saved: ${source}`, 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to save GPS config: ' + error, 'ERROR');
        });
}

function testGpsConnection() {
    const statusEl = document.getElementById('gpsStatus');
    statusEl.textContent = 'TESTING...';
    statusEl.className = 'gps-status';

    addLogEntry('Testing GPS connection...', 'INFO');

    fetch('/api/gps/test', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'success') {
                statusEl.textContent = 'OK';
                statusEl.className = 'gps-status connected';
                addLogEntry(`GPS test OK: ${data.location.lat.toFixed(6)}, ${data.location.lon.toFixed(6)}`, 'INFO');
            } else {
                statusEl.textContent = 'FAIL';
                statusEl.className = 'gps-status error';
                addLogEntry(`GPS test failed: ${data.error}`, 'WARNING');
            }
        })
        .catch(error => {
            statusEl.textContent = 'ERROR';
            statusEl.className = 'gps-status error';
            addLogEntry('GPS test error: ' + error, 'ERROR');
        });
}

function updateGpsStatus(location) {
    const statusEl = document.getElementById('gpsStatus');
    if (location && location.lat && location.lat !== 0) {
        statusEl.textContent = 'OK';
        statusEl.className = 'gps-status connected';
    } else {
        statusEl.textContent = 'NO FIX';
        statusEl.className = 'gps-status';
    }
}

// ==================== DEVICE INFO ====================

function getDeviceInfo(bdAddress) {
    addLogEntry(`Getting info for ${bdAddress}...`, 'INFO');
    socket.emit('request_device_info', { bd_address: bdAddress });
}

function showDeviceInfo(info) {
    const content = document.getElementById('deviceInfoContent');
    const device = devices[info.bd_address] || {};

    let html = '<div class="device-info-content">';

    // Add all available info
    const fields = [
        ['BD Address', info.bd_address],
        ['Device Name', info.device_name || device.device_name || 'Unknown'],
        ['Manufacturer', device.manufacturer || 'Unknown'],
        ['Device Type', device.device_type || 'Unknown'],
        ['Device Class', info.device_class || 'N/A'],
        ['RSSI', device.rssi ? `${device.rssi} dBm` : 'N/A'],
        ['First Seen', device.first_seen || 'N/A'],
        ['Last Seen', device.last_seen || 'N/A'],
        ['System Location', device.system_lat && device.system_lon ?
            `${device.system_lat.toFixed(6)}, ${device.system_lon.toFixed(6)}` : 'N/A'],
        ['Emitter Location', device.emitter_lat && device.emitter_lon ?
            `${device.emitter_lat.toFixed(6)}, ${device.emitter_lon.toFixed(6)}` : 'N/A'],
        ['CEP Radius', device.emitter_accuracy ? `${device.emitter_accuracy.toFixed(1)} m` : 'N/A'],
        ['Is Target', device.is_target ? 'YES' : 'NO']
    ];

    fields.forEach(([label, value]) => {
        html += `
            <div class="info-row">
                <span class="info-label-modal">${label}:</span>
                <span class="info-value-modal">${value}</span>
            </div>
        `;
    });

    // Add raw info if available
    if (info.raw_info) {
        html += `
            <div class="info-row">
                <span class="info-label-modal">Raw Info:</span>
            </div>
            <pre style="font-size: 10px; color: #8b949e; white-space: pre-wrap; word-break: break-all;">
${info.raw_info}
            </pre>
        `;
    }

    html += '</div>';
    content.innerHTML = html;

    document.getElementById('deviceInfoModal').classList.remove('hidden');
    addLogEntry(`Device info loaded for ${info.bd_address}`, 'INFO');
}

function closeDeviceInfoModal() {
    document.getElementById('deviceInfoModal').classList.add('hidden');
}

// ==================== ALERTS ====================

function showTargetAlert(device) {
    // Play alert sound
    const audio = document.getElementById('alertSound');
    audio.currentTime = 0;
    audio.play().catch(e => console.log('Audio play failed:', e));

    // Update modal content
    document.getElementById('alertBdAddress').textContent = device.bd_address;
    document.getElementById('alertDeviceName').textContent = device.device_name || 'Unknown Device';

    let details = '';
    if (device.first_seen) details += `First Seen: ${device.first_seen}\n`;
    if (device.last_seen) details += `Last Seen: ${device.last_seen}\n`;
    if (device.rssi) details += `RSSI: ${device.rssi} dBm\n`;
    if (device.emitter_lat && device.emitter_lon) {
        details += `Emitter Location: ${device.emitter_lat.toFixed(6)}, ${device.emitter_lon.toFixed(6)}\n`;
    }

    document.getElementById('alertDetails').textContent = details;

    // Show modal
    document.getElementById('targetAlertModal').classList.remove('hidden');
}

function closeAlertModal() {
    document.getElementById('targetAlertModal').classList.add('hidden');
    const audio = document.getElementById('alertSound');
    audio.pause();
}

// ==================== LOGS ====================

function loadLogs() {
    fetch('/api/logs')
        .then(response => response.json())
        .then(data => {
            data.forEach(entry => {
                addLogEntry(entry.message, entry.level, false);
            });
        });
}

function addLogEntry(message, level = 'INFO', scroll = true) {
    const container = document.getElementById('logContainer');
    const entry = document.createElement('div');
    entry.className = 'log-entry';

    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `
        <span class="log-time">${time}</span>
        <span class="log-level ${level}">${level}</span>
        <span class="log-message">${message}</span>
    `;

    container.appendChild(entry);

    // Limit log entries
    while (container.children.length > 200) {
        container.removeChild(container.firstChild);
    }

    // Scroll to bottom
    if (scroll) {
        container.scrollTop = container.scrollHeight;
    }
}

// ==================== UTILITIES ====================

function updateTime() {
    const now = new Date();
    document.getElementById('sysTime').textContent = now.toLocaleTimeString();
}

function truncate(str, length) {
    if (!str) return '';
    return str.length > length ? str.substring(0, length) + '...' : str;
}

// Export for logs download
function exportLogs() {
    fetch('/api/logs/export')
        .then(response => response.json())
        .then(data => {
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `bluek9_logs_${new Date().toISOString().slice(0, 10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
        });
}

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
    // Escape to close modals
    if (e.key === 'Escape') {
        closeAlertModal();
        closeDeviceInfoModal();
        hideContextMenu();
    }

    // Ctrl+S to start/stop scan
    if (e.ctrlKey && e.key === 's') {
        e.preventDefault();
        if (scanning) {
            stopScan();
        } else {
            startScan();
        }
    }
});

// ==================== CONTEXT MENU ====================

let contextMenuTarget = null;

function createContextMenu() {
    // Remove existing menu if any
    const existing = document.getElementById('deviceContextMenu');
    if (existing) existing.remove();

    const menu = document.createElement('div');
    menu.id = 'deviceContextMenu';
    menu.className = 'context-menu hidden';
    menu.innerHTML = `
        <div class="context-menu-item" onclick="contextMenuAction('info')">
            <i>ℹ</i> Get Device Info
        </div>
        <div class="context-menu-item" onclick="contextMenuAction('name')">
            <i>📝</i> Get Device Name
        </div>
        <div class="context-menu-item" onclick="contextMenuAction('locate')">
            <i>📍</i> Geolocate Device
        </div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item" onclick="contextMenuAction('target')">
            <i>🎯</i> Add as Target
        </div>
        <div class="context-menu-item" onclick="contextMenuAction('copy')">
            <i>📋</i> Copy BD Address
        </div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item" onclick="contextMenuAction('zoom')">
            <i>🗺</i> Zoom to Location
        </div>
    `;
    document.body.appendChild(menu);
    return menu;
}

function showContextMenu(e, bdAddress) {
    e.preventDefault();
    contextMenuTarget = bdAddress;

    let menu = document.getElementById('deviceContextMenu');
    if (!menu) {
        menu = createContextMenu();
    }

    // Update menu items based on device state
    const device = devices[bdAddress];
    const targetItem = menu.querySelector('[onclick*="target"]');
    if (device && device.is_target) {
        targetItem.innerHTML = '<i>🎯</i> Remove from Targets';
    } else {
        targetItem.innerHTML = '<i>🎯</i> Add as Target';
    }

    // Position the menu
    menu.style.left = e.clientX + 'px';
    menu.style.top = e.clientY + 'px';
    menu.classList.remove('hidden');

    // Adjust position if menu goes off screen
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
        menu.style.left = (window.innerWidth - rect.width - 10) + 'px';
    }
    if (rect.bottom > window.innerHeight) {
        menu.style.top = (window.innerHeight - rect.height - 10) + 'px';
    }
}

function hideContextMenu() {
    const menu = document.getElementById('deviceContextMenu');
    if (menu) {
        menu.classList.add('hidden');
    }
    contextMenuTarget = null;
}

function contextMenuAction(action) {
    if (!contextMenuTarget) return;

    const bdAddress = contextMenuTarget;
    const device = devices[bdAddress];

    switch (action) {
        case 'info':
            getDeviceInfo(bdAddress);
            break;
        case 'name':
            requestDeviceName(bdAddress);
            break;
        case 'locate':
            requestGeolocation(bdAddress);
            break;
        case 'target':
            if (device && device.is_target) {
                removeTarget(bdAddress);
            } else {
                quickAddTarget(bdAddress);
            }
            break;
        case 'copy':
            navigator.clipboard.writeText(bdAddress).then(() => {
                addLogEntry(`Copied ${bdAddress} to clipboard`, 'INFO');
            });
            break;
        case 'zoom':
            if (device && device.emitter_lat && device.emitter_lon) {
                map.flyTo({
                    center: [device.emitter_lon, device.emitter_lat],
                    zoom: 18
                });
            } else {
                addLogEntry('No location data for this device', 'WARNING');
            }
            break;
    }

    hideContextMenu();
}

function requestDeviceName(bdAddress) {
    addLogEntry(`Requesting name for ${bdAddress}...`, 'INFO');
    fetch(`/api/device/${bdAddress}/name`, { method: 'GET' })
        .then(r => r.json())
        .then(data => {
            if (data.name) {
                addLogEntry(`Device name: ${data.name}`, 'INFO');
                if (devices[bdAddress]) {
                    devices[bdAddress].device_name = data.name;
                    updateSurveyTable();
                }
            } else {
                addLogEntry(`Could not get name for ${bdAddress}`, 'WARNING');
            }
        })
        .catch(e => addLogEntry(`Name request failed: ${e}`, 'ERROR'));
}

function requestGeolocation(bdAddress) {
    addLogEntry(`Requesting geolocation for ${bdAddress}...`, 'INFO');
    fetch(`/api/device/${bdAddress}/locate`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.location) {
                addLogEntry(`Location: ${data.location.lat.toFixed(6)}, ${data.location.lon.toFixed(6)} (CEP: ${data.location.cep}m)`, 'INFO');
                if (devices[bdAddress]) {
                    devices[bdAddress].emitter_lat = data.location.lat;
                    devices[bdAddress].emitter_lon = data.location.lon;
                    devices[bdAddress].emitter_accuracy = data.location.cep;
                    updateDeviceMarker(devices[bdAddress]);
                    updateSurveyTable();
                }
            } else {
                addLogEntry(data.message || 'Insufficient data for geolocation', 'WARNING');
            }
        })
        .catch(e => addLogEntry(`Geolocation failed: ${e}`, 'ERROR'));
}

// Click anywhere to close context menu
document.addEventListener('click', (e) => {
    if (!e.target.closest('.context-menu')) {
        hideContextMenu();
    }
});
