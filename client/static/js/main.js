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
let showBreadcrumbs = false;
let scanning = false;
let deviceTypeChart = null;
let currentMapStyle = 'dark';
let breadcrumbMarkers = [];

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
        showTargetAlert(data);
    });

    socket.on('device_info', (info) => {
        showDeviceInfo(info);
    });

    socket.on('name_result', (data) => {
        handleNameResult(data);
    });
}

function handleNameResult(data) {
    const bdAddress = data.bd_address;
    const attempt = data.attempt;

    if (data.status === 'found' && data.name) {
        addLogEntry(`[Attempt ${attempt}] Got name for ${bdAddress}: ${data.name}`, 'INFO');
        if (devices[bdAddress]) {
            devices[bdAddress].device_name = data.name;
            updateSurveyTable();
        }
    } else if (data.status === 'no_response') {
        addLogEntry(`[Attempt ${attempt}] No name response from ${bdAddress}`, 'DEBUG');
    } else if (data.status === 'timeout') {
        addLogEntry(`[Attempt ${attempt}] Name query timeout for ${bdAddress}`, 'DEBUG');
    } else if (data.status === 'error') {
        addLogEntry(`[Attempt ${attempt}] Name query error for ${bdAddress}: ${data.error}`, 'WARNING');
    }
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

        const cep = device.emitter_accuracy
            ? `${device.emitter_accuracy.toFixed(0)}m`
            : '--';

        const lastSeen = device.last_seen
            ? new Date(device.last_seen).toLocaleTimeString()
            : '--';

        row.innerHTML = `
            <td class="bd-address">${device.bd_address}</td>
            <td title="${device.device_name || 'Unknown'}">${truncate(device.device_name || 'Unknown', 12)}</td>
            <td title="${device.manufacturer || 'Unknown'}">${truncate(device.manufacturer || '?', 8)}</td>
            <td>${device.rssi || '--'}</td>
            <td class="cep-cell">${cep}</td>
            <td title="${emitterLoc}">${emitterLoc !== '--' ? emitterLoc.split(',')[0] + '...' : '--'}</td>
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

// ==================== BREADCRUMB / HEATMAP ====================

// Target Heatworm - detection points
// Target Heatmap - RSSI-based amoeba (red=hot/center, blue=cold/outside)

function toggleBreadcrumbs() {
    showBreadcrumbs = document.getElementById('toggleBreadcrumbs').checked;

    if (showBreadcrumbs) {
        loadBreadcrumbs();
    } else {
        clearBreadcrumbs();
    }
}

function loadBreadcrumbs() {
    fetch('/api/breadcrumbs')
        .then(r => r.json())
        .then(points => {
            clearBreadcrumbs();

            if (points.length === 0) {
                addLogEntry('No heatmap data available', 'INFO');
                return;
            }

            // Create GeoJSON for heatmap layer
            const geojsonData = {
                type: 'FeatureCollection',
                features: points.filter(p => p.lat && p.lon).map(point => ({
                    type: 'Feature',
                    properties: {
                        // Convert RSSI to weight (stronger signal = higher weight)
                        // RSSI typically ranges from -30 (very strong) to -100 (very weak)
                        weight: Math.max(0, (point.rssi + 100) / 70),
                        rssi: point.rssi,
                        bd_address: point.bd_address
                    },
                    geometry: {
                        type: 'Point',
                        coordinates: [point.lon, point.lat]
                    }
                }))
            };

            // Add or update heatmap source
            if (map.getSource('rssi-heatmap')) {
                map.getSource('rssi-heatmap').setData(geojsonData);
            } else {
                map.addSource('rssi-heatmap', {
                    type: 'geojson',
                    data: geojsonData
                });

                // Add heatmap layer - amoeba effect with red=hot, blue=cold
                map.addLayer({
                    id: 'rssi-heatmap-layer',
                    type: 'heatmap',
                    source: 'rssi-heatmap',
                    paint: {
                        // Weight based on RSSI (stronger = more weight)
                        'heatmap-weight': ['get', 'weight'],
                        // Intensity increases with zoom
                        'heatmap-intensity': [
                            'interpolate', ['linear'], ['zoom'],
                            10, 0.5,
                            15, 1,
                            20, 2
                        ],
                        // Radius increases with zoom
                        'heatmap-radius': [
                            'interpolate', ['linear'], ['zoom'],
                            10, 15,
                            15, 30,
                            20, 50
                        ],
                        // Color ramp: blue (cold) -> cyan -> green -> yellow -> red (hot)
                        'heatmap-color': [
                            'interpolate', ['linear'], ['heatmap-density'],
                            0, 'rgba(0, 0, 255, 0)',        // Transparent
                            0.1, 'rgba(0, 0, 255, 0.4)',   // Blue (cold/far)
                            0.3, 'rgba(0, 255, 255, 0.5)', // Cyan
                            0.5, 'rgba(0, 255, 0, 0.6)',   // Green
                            0.7, 'rgba(255, 255, 0, 0.7)', // Yellow
                            0.9, 'rgba(255, 128, 0, 0.8)', // Orange
                            1.0, 'rgba(255, 0, 0, 0.9)'    // Red (hot/close)
                        ],
                        // Opacity
                        'heatmap-opacity': 0.7
                    }
                });
            }

            // Add detection markers (heatworm points) on top of heatmap
            points.forEach((point, index) => {
                if (!point.lat || !point.lon) return;

                const el = document.createElement('div');
                el.className = 'breadcrumb-marker heatworm-point';

                // Small white dot with colored ring based on RSSI
                const rssi = point.rssi || -70;
                const intensity = Math.min(1, Math.max(0, (rssi + 100) / 70));

                // Color: blue (weak) -> green -> yellow -> red (strong)
                let color;
                if (intensity < 0.33) {
                    color = `rgb(0, ${Math.round(intensity * 3 * 255)}, 255)`;
                } else if (intensity < 0.66) {
                    const t = (intensity - 0.33) * 3;
                    color = `rgb(${Math.round(t * 255)}, 255, ${Math.round((1 - t) * 255)})`;
                } else {
                    const t = (intensity - 0.66) * 3;
                    color = `rgb(255, ${Math.round((1 - t) * 255)}, 0)`;
                }

                el.style.width = '8px';
                el.style.height = '8px';
                el.style.backgroundColor = '#fff';
                el.style.border = `2px solid ${color}`;
                el.style.boxShadow = `0 0 4px ${color}`;
                el.title = `${point.bd_address}\nRSSI: ${rssi} dBm`;

                const marker = new mapboxgl.Marker(el)
                    .setLngLat([point.lon, point.lat])
                    .addTo(map);

                breadcrumbMarkers.push(marker);
            });

            addLogEntry(`Loaded heatmap: ${points.length} detection points`, 'INFO');
        })
        .catch(e => addLogEntry(`Failed to load heatmap: ${e}`, 'ERROR'));
}

function clearBreadcrumbs() {
    // Clear markers
    breadcrumbMarkers.forEach(m => m.remove());
    breadcrumbMarkers = [];

    // Clear heatmap layer and source
    if (map && map.getLayer('rssi-heatmap-layer')) {
        map.removeLayer('rssi-heatmap-layer');
    }
    if (map && map.getSource('rssi-heatmap')) {
        map.removeSource('rssi-heatmap');
    }
}

function resetBreadcrumbs() {
    if (!confirm('Reset all heatmap data? This will clear all RSSI history.')) return;

    fetch('/api/breadcrumbs/reset', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            clearBreadcrumbs();
            clearSystemTrail();
            addLogEntry(`Heatmap reset: ${data.cleared} points cleared`, 'INFO');
        })
        .catch(e => addLogEntry(`Failed to reset heatmap: ${e}`, 'ERROR'));
}

// System Trail - where the system has been
let showSystemTrail = false;
let systemTrailMarkers = [];

function toggleSystemTrail() {
    showSystemTrail = document.getElementById('toggleSystemTrail').checked;

    if (showSystemTrail) {
        loadSystemTrail();
    } else {
        clearSystemTrail();
    }
}

function loadSystemTrail() {
    fetch('/api/system_trail')
        .then(r => r.json())
        .then(points => {
            clearSystemTrail();

            // System trail - show in CYAN (where system has been)
            points.forEach((point, index) => {
                if (!point.lat || !point.lon) return;

                const el = document.createElement('div');
                el.className = 'breadcrumb-marker system-trail';

                // Fade older points
                const opacity = Math.max(0.2, 1 - (index / points.length) * 0.8);
                el.style.backgroundColor = `rgba(0, 212, 255, ${opacity})`;
                el.style.width = '6px';
                el.style.height = '6px';

                const marker = new mapboxgl.Marker(el)
                    .setLngLat([point.lon, point.lat])
                    .addTo(map);

                systemTrailMarkers.push(marker);
            });

            addLogEntry(`Loaded ${points.length} system trail points`, 'INFO');
        })
        .catch(e => addLogEntry(`Failed to load system trail: ${e}`, 'ERROR'));
}

function clearSystemTrail() {
    systemTrailMarkers.forEach(m => m.remove());
    systemTrailMarkers = [];
}

function resetSystemTrail() {
    if (!confirm('Reset system trail? This will clear all position history.')) return;

    fetch('/api/system_trail/reset', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            clearSystemTrail();
            addLogEntry(`System trail reset`, 'INFO');
        })
        .catch(e => addLogEntry(`Failed to reset trail: ${e}`, 'ERROR'));
}

// ==================== GEO RESET ====================

function resetAllGeo() {
    if (!confirm('Reset all geolocation data? This will clear all RSSI history and emitter estimates.')) return;

    fetch('/api/geo/reset_all', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            addLogEntry(`All geo data reset: ${data.cleared} observations cleared`, 'INFO');
            // Clear all CEP circles
            Object.keys(cepCircles).forEach(bdAddr => {
                const sourceId = `cep-${bdAddr}`;
                if (map.getLayer(sourceId)) map.removeLayer(sourceId);
                if (map.getSource(sourceId)) map.removeSource(sourceId);
            });
            cepCircles = {};
            // Remove device markers with geo
            Object.keys(markers).forEach(bdAddr => {
                markers[bdAddr].remove();
                delete markers[bdAddr];
            });
        })
        .catch(e => addLogEntry(`Failed to reset geo: ${e}`, 'ERROR'));
}

function resetDeviceGeo(bdAddress) {
    fetch(`/api/device/${bdAddress}/geo/reset`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            addLogEntry(`Geo reset for ${bdAddress}: ${data.cleared} observations cleared`, 'INFO');
            // Remove marker and CEP
            if (markers[bdAddress]) {
                markers[bdAddress].remove();
                delete markers[bdAddress];
            }
            if (cepCircles[bdAddress]) {
                const sourceId = `cep-${bdAddress}`;
                if (map.getLayer(sourceId)) map.removeLayer(sourceId);
                if (map.getSource(sourceId)) map.removeSource(sourceId);
                delete cepCircles[bdAddress];
            }
        })
        .catch(e => addLogEntry(`Failed to reset geo for ${bdAddress}: ${e}`, 'ERROR'));
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

        // Check if target is in detected devices
        const detected = devices[target.bd_address];
        const statusClass = detected ? 'target-detected' : 'target-not-detected';

        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary ${statusClass}">${target.bd_address}</span>
                <span class="item-secondary">${target.alias || 'No alias'}</span>
            </div>
            <button class="item-delete" onclick="deleteTarget('${target.bd_address}')">&times;</button>
        `;

        // Right-click context menu for targets
        item.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            showTargetContextMenu(e, target.bd_address);
        });

        list.appendChild(item);
    });
}

function showTargetContextMenu(event, bdAddress) {
    contextMenuTarget = bdAddress;

    let menu = document.getElementById('targetContextMenu');
    if (!menu) {
        menu = createTargetContextMenu();
    }

    // Position menu at click location
    menu.style.left = event.pageX + 'px';
    menu.style.top = event.pageY + 'px';
    menu.classList.remove('hidden');

    event.preventDefault();
}

function createTargetContextMenu() {
    const existing = document.getElementById('targetContextMenu');
    if (existing) existing.remove();

    const menu = document.createElement('div');
    menu.id = 'targetContextMenu';
    menu.className = 'context-menu hidden';
    menu.innerHTML = `
        <div class="context-menu-item" onclick="targetContextAction('info')">
            <i>ℹ</i> Get Device Info
        </div>
        <div class="context-menu-item" onclick="targetContextAction('name')">
            <i>📝</i> Get Device Name
        </div>
        <div class="context-menu-item" onclick="targetContextAction('stimulate')">
            <i>📡</i> Stimulate (BT Classic)
        </div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item" onclick="targetContextAction('locate')">
            <i>📍</i> Geolocate Device
        </div>
        <div class="context-menu-item" onclick="targetContextAction('georeset')">
            <i>🔄</i> Reset Device Geo
        </div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item" onclick="targetContextAction('copy')">
            <i>📋</i> Copy BD Address
        </div>
        <div class="context-menu-item" onclick="targetContextAction('zoom')">
            <i>🗺</i> Zoom to Location
        </div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item target-remove" onclick="targetContextAction('remove')">
            <i>🗑</i> Remove Target
        </div>
    `;
    document.body.appendChild(menu);
    return menu;
}

function targetContextAction(action) {
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
        case 'stimulate':
            stimulateForDevice(bdAddress);
            break;
        case 'locate':
            requestGeolocation(bdAddress);
            break;
        case 'georeset':
            resetDeviceGeo(bdAddress);
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
                addLogEntry('No location data for this target', 'WARNING');
            }
            break;
        case 'remove':
            deleteTarget(bdAddress);
            break;
    }

    hideTargetContextMenu();
}

function hideTargetContextMenu() {
    const menu = document.getElementById('targetContextMenu');
    if (menu) menu.classList.add('hidden');
    contextMenuTarget = null;
}

function stimulateForDevice(bdAddress) {
    addLogEntry(`Stimulating to find ${bdAddress}...`, 'INFO');
    fetch('/api/scan/stimulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'classic' })
    })
        .then(r => r.json())
        .then(data => {
            addLogEntry(`Stimulation complete. Found ${data.devices_found || 0} devices.`, 'INFO');
        })
        .catch(e => addLogEntry(`Stimulation failed: ${e}`, 'ERROR'));
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
    addLogEntry(`Querying hcitool info for ${bdAddress}...`, 'INFO');

    // Show modal with loading spinner
    const content = document.getElementById('deviceInfoContent');
    content.innerHTML = `
        <div class="loading-container">
            <div class="loading-spinner"></div>
            <div class="loading-text">Running hcitool info ${bdAddress}...</div>
            <div class="loading-subtext">This may take a few seconds</div>
        </div>
    `;
    document.getElementById('deviceInfoModal').classList.remove('hidden');

    socket.emit('request_device_info', { bd_address: bdAddress });
}

function showDeviceInfo(info) {
    const content = document.getElementById('deviceInfoContent');
    const device = devices[info.bd_address] || {};

    let html = '<div class="device-info-content">';

    // Show hcitool raw output prominently at top
    if (info.raw_info && info.raw_info.trim()) {
        html += `
            <div class="info-section-header">HCITOOL INFO OUTPUT</div>
            <pre class="hcitool-output">${info.raw_info}</pre>
            <div class="info-section-header" style="margin-top: 15px;">DEVICE SUMMARY</div>
        `;
    } else {
        html += `
            <div class="info-section-header">HCITOOL INFO OUTPUT</div>
            <pre class="hcitool-output hcitool-empty">No response from device.
Device may be out of range or not responding.</pre>
            <div class="info-section-header" style="margin-top: 15px;">CACHED DATA</div>
        `;
    }

    // Add summary info
    const fields = [
        ['BD Address', info.bd_address],
        ['Device Name', info.device_name || device.device_name || 'Unknown'],
        ['Manufacturer', device.manufacturer || 'Unknown'],
        ['Device Type', device.device_type || 'Unknown'],
        ['Device Class', info.device_class || 'N/A'],
        ['RSSI', device.rssi ? `${device.rssi} dBm` : 'N/A'],
        ['First Seen', device.first_seen || 'N/A'],
        ['Last Seen', device.last_seen || 'N/A'],
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

    html += '</div>';
    content.innerHTML = html;

    addLogEntry(`Device info loaded for ${info.bd_address}`, 'INFO');
}

function closeDeviceInfoModal() {
    document.getElementById('deviceInfoModal').classList.add('hidden');
}

// ==================== ALERTS ====================

// Store current alert data for copy function
let currentAlertData = null;

function showTargetAlert(data) {
    const device = data.device || data;
    const location = data.location || currentLocation;
    const systemId = data.system_id || 'Unknown';
    const systemName = data.system_name || 'BlueK9';

    // Play alert sound
    const audio = document.getElementById('alertSound');
    audio.currentTime = 0;
    audio.play().catch(e => console.log('Audio play failed:', e));

    // Update modal content
    document.getElementById('alertBdAddress').textContent = device.bd_address;
    document.getElementById('alertDeviceName').textContent = device.device_name || 'Unknown Device';

    let details = '';
    details += `System: ${systemId} (${systemName})\n`;
    details += `Time: ${new Date().toLocaleString()}\n`;
    if (device.first_seen) details += `First Seen: ${device.first_seen}\n`;
    if (device.last_seen) details += `Last Seen: ${device.last_seen}\n`;
    if (device.rssi) details += `RSSI: ${device.rssi} dBm\n`;
    if (device.device_type) details += `Type: ${device.device_type}\n`;
    if (device.manufacturer) details += `Manufacturer: ${device.manufacturer}\n`;
    if (device.emitter_lat && device.emitter_lon) {
        details += `Emitter Est: ${device.emitter_lat.toFixed(6)}, ${device.emitter_lon.toFixed(6)}\n`;
        if (device.emitter_accuracy) details += `CEP: ${device.emitter_accuracy.toFixed(1)}m\n`;
    }

    document.getElementById('alertDetails').textContent = details;

    // System location
    const sysLocSpan = document.getElementById('alertSystemLocation');
    const mapLink = document.getElementById('alertMapLink');
    if (location && location.lat && location.lon) {
        sysLocSpan.textContent = `${location.lat.toFixed(6)}, ${location.lon.toFixed(6)}`;
        mapLink.href = `https://maps.google.com/?q=${location.lat.toFixed(6)},${location.lon.toFixed(6)}`;
        mapLink.style.display = 'inline';
    } else {
        sysLocSpan.textContent = 'No GPS Fix';
        mapLink.style.display = 'none';
    }

    // Store for copy
    currentAlertData = {
        device: device,
        location: location,
        systemId: systemId,
        systemName: systemName,
        timestamp: new Date().toISOString()
    };

    // Show modal
    document.getElementById('targetAlertModal').classList.remove('hidden');
}

function copyAlertDetails() {
    if (!currentAlertData) {
        addLogEntry('No alert data to copy', 'WARNING');
        return;
    }

    const d = currentAlertData.device;
    const loc = currentAlertData.location;

    let text = `=== TARGET ALERT ===\n`;
    text += `System: ${currentAlertData.systemId} (${currentAlertData.systemName})\n`;
    text += `Time: ${currentAlertData.timestamp}\n`;
    text += `\n--- Device ---\n`;
    text += `BD Address: ${d.bd_address}\n`;
    text += `Name: ${d.device_name || 'Unknown'}\n`;
    if (d.rssi) text += `RSSI: ${d.rssi} dBm\n`;
    if (d.device_type) text += `Type: ${d.device_type}\n`;
    if (d.manufacturer) text += `Manufacturer: ${d.manufacturer}\n`;
    if (d.first_seen) text += `First Seen: ${d.first_seen}\n`;
    if (d.last_seen) text += `Last Seen: ${d.last_seen}\n`;

    if (loc && loc.lat) {
        text += `\n--- System Location ---\n`;
        text += `Lat: ${loc.lat.toFixed(6)}\n`;
        text += `Lon: ${loc.lon.toFixed(6)}\n`;
        text += `Google Maps: https://maps.google.com/?q=${loc.lat.toFixed(6)},${loc.lon.toFixed(6)}\n`;
    }

    if (d.emitter_lat && d.emitter_lon) {
        text += `\n--- Estimated Emitter Location ---\n`;
        text += `Lat: ${d.emitter_lat.toFixed(6)}\n`;
        text += `Lon: ${d.emitter_lon.toFixed(6)}\n`;
        if (d.emitter_accuracy) text += `CEP: ${d.emitter_accuracy.toFixed(1)}m\n`;
        text += `Google Maps: https://maps.google.com/?q=${d.emitter_lat.toFixed(6)},${d.emitter_lon.toFixed(6)}\n`;
    }

    // Try clipboard API first, fallback to textarea method
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            addLogEntry('Alert details copied to clipboard', 'INFO');
        }).catch(err => {
            // Fallback
            copyTextFallback(text);
        });
    } else {
        copyTextFallback(text);
    }
}

function copyTextFallback(text) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand('copy');
        addLogEntry('Alert details copied to clipboard', 'INFO');
    } catch (err) {
        addLogEntry('Failed to copy: ' + err, 'ERROR');
    }
    document.body.removeChild(textarea);
}

function closeAlertModal() {
    document.getElementById('targetAlertModal').classList.add('hidden');
    const audio = document.getElementById('alertSound');
    audio.pause();
    currentAlertData = null;
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
        <div class="context-menu-item" onclick="contextMenuAction('georeset')">
            <i>🔄</i> Reset Device Geo
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
        case 'georeset':
            resetDeviceGeo(bdAddress);
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

// Track continuous name retrieval state
const nameRetrievalActive = {};

function requestDeviceName(bdAddress) {
    // Start continuous name retrieval
    if (nameRetrievalActive[bdAddress]) {
        // Stop if already running
        stopNameRetrieval(bdAddress);
    } else {
        startNameRetrieval(bdAddress);
    }
}

function startNameRetrieval(bdAddress) {
    addLogEntry(`Starting continuous name query for ${bdAddress}...`, 'INFO');
    nameRetrievalActive[bdAddress] = true;

    fetch(`/api/device/${bdAddress}/name/start`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'started') {
                addLogEntry(`Name retrieval started for ${bdAddress}`, 'INFO');
            } else if (data.status === 'already_running') {
                addLogEntry(`Name retrieval already running for ${bdAddress}`, 'WARNING');
            }
        })
        .catch(e => {
            addLogEntry(`Failed to start name retrieval: ${e}`, 'ERROR');
            nameRetrievalActive[bdAddress] = false;
        });
}

function stopNameRetrieval(bdAddress) {
    addLogEntry(`Stopping name query for ${bdAddress}...`, 'INFO');

    fetch(`/api/device/${bdAddress}/name/stop`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            nameRetrievalActive[bdAddress] = false;
            addLogEntry(`Name retrieval stopped for ${bdAddress}`, 'INFO');
        })
        .catch(e => {
            addLogEntry(`Failed to stop name retrieval: ${e}`, 'ERROR');
        });
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

// Click anywhere to close context menus
document.addEventListener('click', (e) => {
    if (!e.target.closest('.context-menu')) {
        hideContextMenu();
        hideTargetContextMenu();
    }
});

// ==================== SETTINGS MODAL ====================

function openSettingsModal() {
    loadSettings();
    loadUsers();
    document.getElementById('settingsModal').classList.remove('hidden');
}

function closeSettingsModal() {
    document.getElementById('settingsModal').classList.add('hidden');
}

function showSettingsTab(tabName) {
    // Hide all tabs
    document.querySelectorAll('.settings-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.settings-tab').forEach(el => el.classList.remove('active'));

    // Show selected tab
    document.getElementById('settings' + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add('active');
    event.target.classList.add('active');
}

function updateGpsSettingsFields() {
    const source = document.getElementById('settingGpsSource').value;
    document.getElementById('gpsNmeaSettings').classList.toggle('hidden', source !== 'nmea_tcp');
    document.getElementById('gpsGpsdSettings').classList.toggle('hidden', source !== 'gpsd');
    document.getElementById('gpsSerialSettings').classList.toggle('hidden', source !== 'serial');
}

function loadSettings() {
    fetch('/api/settings')
        .then(r => r.json())
        .then(data => {
            // General settings
            document.getElementById('settingSystemId').value = data.system_id || 'BK9-001';
            document.getElementById('settingSystemName').value = data.system_name || 'BlueK9 Unit 1';
            document.getElementById('settingScanInterval').value = data.scan_interval || 2;
            document.getElementById('settingSmsInterval').value = data.sms_alert_interval || 60;

            // GPS settings
            document.getElementById('settingGpsSource').value = data.gps_source || 'nmea_tcp';
            document.getElementById('settingNmeaHost').value = data.nmea_tcp_host || '127.0.0.1';
            document.getElementById('settingNmeaPort').value = data.nmea_tcp_port || 10110;
            document.getElementById('settingGpsdHost').value = data.gpsd_host || '127.0.0.1';
            document.getElementById('settingGpsdPort').value = data.gpsd_port || 2947;
            document.getElementById('settingSerialPort').value = data.gps_serial_port || '/dev/ttyUSB0';
            document.getElementById('settingSerialBaud').value = data.gps_serial_baud || 9600;

            updateGpsSettingsFields();

            // UI settings
            document.getElementById('settingLeftPanelWidth').value = data.left_panel_width || 280;
            document.getElementById('settingRightPanelWidth').value = data.right_panel_width || 420;
        })
        .catch(e => addLogEntry(`Failed to load settings: ${e}`, 'ERROR'));
}

function saveAllSettings() {
    const settings = {
        system_id: document.getElementById('settingSystemId').value,
        system_name: document.getElementById('settingSystemName').value,
        scan_interval: parseInt(document.getElementById('settingScanInterval').value),
        sms_alert_interval: parseInt(document.getElementById('settingSmsInterval').value),
        gps_source: document.getElementById('settingGpsSource').value,
        nmea_tcp_host: document.getElementById('settingNmeaHost').value,
        nmea_tcp_port: parseInt(document.getElementById('settingNmeaPort').value),
        gpsd_host: document.getElementById('settingGpsdHost').value,
        gpsd_port: parseInt(document.getElementById('settingGpsdPort').value),
        gps_serial_port: document.getElementById('settingSerialPort').value,
        gps_serial_baud: parseInt(document.getElementById('settingSerialBaud').value),
        left_panel_width: parseInt(document.getElementById('settingLeftPanelWidth').value),
        right_panel_width: parseInt(document.getElementById('settingRightPanelWidth').value)
    };

    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'saved') {
                addLogEntry('Settings saved successfully', 'INFO');
                applyLayoutSettings(settings);
                closeSettingsModal();
            } else {
                addLogEntry('Failed to save settings', 'ERROR');
            }
        })
        .catch(e => addLogEntry(`Save settings error: ${e}`, 'ERROR'));
}

function applyLayoutSettings(settings) {
    const leftPanel = document.querySelector('.panel-left');
    const rightPanel = document.querySelector('.panel-right');

    if (leftPanel && settings.left_panel_width) {
        leftPanel.style.width = settings.left_panel_width + 'px';
    }
    if (rightPanel && settings.right_panel_width) {
        rightPanel.style.width = settings.right_panel_width + 'px';
    }
}

function testGpsSettings() {
    const source = document.getElementById('settingGpsSource').value;
    let config = { source: source };

    if (source === 'nmea_tcp') {
        config.host = document.getElementById('settingNmeaHost').value;
        config.port = parseInt(document.getElementById('settingNmeaPort').value);
    } else if (source === 'gpsd') {
        config.host = document.getElementById('settingGpsdHost').value;
        config.port = parseInt(document.getElementById('settingGpsdPort').value);
    } else if (source === 'serial') {
        config.port = document.getElementById('settingSerialPort').value;
        config.baud = parseInt(document.getElementById('settingSerialBaud').value);
    }

    addLogEntry('Testing GPS connection...', 'INFO');
    fetch('/api/gps/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                addLogEntry(`GPS test successful: ${data.lat}, ${data.lon}`, 'INFO');
            } else {
                addLogEntry(`GPS test failed: ${data.error}`, 'ERROR');
            }
        })
        .catch(e => addLogEntry(`GPS test error: ${e}`, 'ERROR'));
}

// ==================== USER MANAGEMENT ====================

function loadUsers() {
    fetch('/api/users')
        .then(r => r.json())
        .then(data => {
            const userList = document.getElementById('userList');
            userList.innerHTML = '';

            data.forEach(user => {
                const item = document.createElement('div');
                item.className = 'user-item';
                // Only admins can delete, and can't delete bluek9 or themselves
                const canDelete = typeof IS_ADMIN !== 'undefined' && IS_ADMIN &&
                                  user.username !== 'bluek9' &&
                                  user.username !== (typeof CURRENT_USER !== 'undefined' ? CURRENT_USER : '');
                item.innerHTML = `
                    <div>
                        <span class="user-name">${user.username}</span>
                        <span class="user-role">${user.is_admin ? '[ADMIN]' : '[USER]'}</span>
                    </div>
                    <div class="user-actions">
                        ${canDelete ? `<button onclick="deleteUser('${user.username}')">Delete</button>` : ''}
                    </div>
                `;
                userList.appendChild(item);
            });
        })
        .catch(e => addLogEntry(`Failed to load users: ${e}`, 'ERROR'));
}

function createUser() {
    const username = document.getElementById('newUsername').value;
    const password = document.getElementById('newPassword').value;
    const isAdmin = document.getElementById('newUserAdmin').checked;

    if (!username || !password) {
        addLogEntry('Username and password are required', 'ERROR');
        return;
    }

    fetch('/api/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, is_admin: isAdmin })
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'created') {
                addLogEntry(`User ${username} created`, 'INFO');
                document.getElementById('newUsername').value = '';
                document.getElementById('newPassword').value = '';
                document.getElementById('newUserAdmin').checked = false;
                loadUsers();
            } else {
                addLogEntry(data.error || 'Failed to create user', 'ERROR');
            }
        })
        .catch(e => addLogEntry(`Create user error: ${e}`, 'ERROR'));
}

function deleteUser(username) {
    if (!confirm(`Delete user ${username}?`)) return;

    fetch(`/api/users/${username}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'deleted') {
                addLogEntry(`User ${username} deleted`, 'INFO');
                loadUsers();
            } else {
                addLogEntry(data.error || 'Failed to delete user', 'ERROR');
            }
        })
        .catch(e => addLogEntry(`Delete user error: ${e}`, 'ERROR'));
}

function changePassword() {
    const current = document.getElementById('currentPassword').value;
    const newPass = document.getElementById('newPasswordChange').value;
    const confirm = document.getElementById('confirmPassword').value;

    if (newPass !== confirm) {
        addLogEntry('New passwords do not match', 'ERROR');
        return;
    }

    fetch('/api/users/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: current, new_password: newPass })
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'changed') {
                addLogEntry('Password changed successfully', 'INFO');
                document.getElementById('currentPassword').value = '';
                document.getElementById('newPasswordChange').value = '';
                document.getElementById('confirmPassword').value = '';
            } else {
                addLogEntry(data.error || 'Failed to change password', 'ERROR');
            }
        })
        .catch(e => addLogEntry(`Change password error: ${e}`, 'ERROR'));
}

// ==================== CONFIG EXPORT/IMPORT ====================

function exportConfig() {
    fetch('/api/config/export')
        .then(r => r.json())
        .then(data => {
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `bluek9-config-${new Date().toISOString().slice(0,10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
            addLogEntry('Configuration exported', 'INFO');
        })
        .catch(e => addLogEntry(`Export error: ${e}`, 'ERROR'));
}

function importConfig(event) {
    const file = event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
        try {
            const config = JSON.parse(e.target.result);
            fetch('/api/config/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'imported') {
                        addLogEntry('Configuration imported successfully', 'INFO');
                        loadSettings();
                    } else {
                        addLogEntry(data.error || 'Import failed', 'ERROR');
                    }
                });
        } catch (err) {
            addLogEntry('Invalid configuration file', 'ERROR');
        }
    };
    reader.readAsText(file);
    event.target.value = '';
}

function resetLayout() {
    if (!confirm('Reset UI layout to default?')) return;

    const leftPanel = document.querySelector('.panel-left');
    const rightPanel = document.querySelector('.panel-right');
    const surveySection = document.getElementById('surveySection');
    const statsSection = document.getElementById('statsSection');
    const logsSection = document.getElementById('logsSection');

    leftPanel.style.width = '280px';
    rightPanel.style.width = '420px';

    // Reset vertical section heights to auto (flex)
    if (surveySection) {
        surveySection.style.height = '';
        surveySection.style.flex = '1';
    }
    if (statsSection) {
        statsSection.style.height = '';
        statsSection.style.flex = '0 0 auto';
    }
    if (logsSection) {
        logsSection.style.height = '';
        logsSection.style.flex = '1';
    }

    document.getElementById('settingLeftPanelWidth').value = 280;
    document.getElementById('settingRightPanelWidth').value = 420;

    // Clear localStorage layout
    localStorage.removeItem('bluek9_layout');

    addLogEntry('Layout reset to default', 'INFO');
}

// ==================== DRAGGABLE PANEL RESIZERS ====================

function saveLayoutToLocalStorage() {
    const leftPanel = document.querySelector('.panel-left');
    const rightPanel = document.querySelector('.panel-right');
    const surveySection = document.getElementById('surveySection');
    const statsSection = document.getElementById('statsSection');
    const logsSection = document.getElementById('logsSection');

    const layout = {
        leftPanelWidth: leftPanel ? leftPanel.offsetWidth : 280,
        rightPanelWidth: rightPanel ? rightPanel.offsetWidth : 420,
        surveySectionHeight: surveySection ? surveySection.offsetHeight : null,
        statsSectionHeight: statsSection ? statsSection.offsetHeight : null,
        logsSectionHeight: logsSection ? logsSection.offsetHeight : null,
        savedAt: new Date().toISOString()
    };

    localStorage.setItem('bluek9_layout', JSON.stringify(layout));

    // Also update settings inputs if they exist
    const leftInput = document.getElementById('settingLeftPanelWidth');
    const rightInput = document.getElementById('settingRightPanelWidth');
    if (leftInput) leftInput.value = layout.leftPanelWidth;
    if (rightInput) rightInput.value = layout.rightPanelWidth;
}

function loadLayoutFromLocalStorage() {
    const saved = localStorage.getItem('bluek9_layout');
    if (!saved) return;

    try {
        const layout = JSON.parse(saved);
        const leftPanel = document.querySelector('.panel-left');
        const rightPanel = document.querySelector('.panel-right');
        const surveySection = document.getElementById('surveySection');
        const statsSection = document.getElementById('statsSection');
        const logsSection = document.getElementById('logsSection');

        if (leftPanel && layout.leftPanelWidth) {
            leftPanel.style.width = layout.leftPanelWidth + 'px';
        }
        if (rightPanel && layout.rightPanelWidth) {
            rightPanel.style.width = layout.rightPanelWidth + 'px';
        }

        // Restore vertical section heights
        if (surveySection && layout.surveySectionHeight) {
            surveySection.style.height = layout.surveySectionHeight + 'px';
            surveySection.style.flex = 'none';
        }
        if (statsSection && layout.statsSectionHeight) {
            statsSection.style.height = layout.statsSectionHeight + 'px';
            statsSection.style.flex = 'none';
        }
        if (logsSection && layout.logsSectionHeight) {
            logsSection.style.height = layout.logsSectionHeight + 'px';
            logsSection.style.flex = 'none';
        }

        addLogEntry('Layout loaded from local storage', 'INFO');
    } catch (e) {
        console.error('Failed to load layout:', e);
    }
}

function initPanelResizers() {
    const leftPanel = document.querySelector('.panel-left');
    const rightPanel = document.querySelector('.panel-right');
    const leftHandle = document.getElementById('resizeHandleLeft');
    const rightHandle = document.getElementById('resizeHandleRight');

    if (!leftHandle || !rightHandle) return;

    let isResizing = false;
    let currentHandle = null;
    let startX = 0;
    let startWidth = 0;

    // Left handle - resizes left panel
    leftHandle.addEventListener('mousedown', (e) => {
        isResizing = true;
        currentHandle = 'left';
        startX = e.clientX;
        startWidth = leftPanel.offsetWidth;
        leftHandle.classList.add('dragging');
        document.body.classList.add('resizing');
        e.preventDefault();
    });

    // Right handle - resizes right panel
    rightHandle.addEventListener('mousedown', (e) => {
        isResizing = true;
        currentHandle = 'right';
        startX = e.clientX;
        startWidth = rightPanel.offsetWidth;
        rightHandle.classList.add('dragging');
        document.body.classList.add('resizing');
        e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
        if (!isResizing) return;

        const diff = e.clientX - startX;

        if (currentHandle === 'left') {
            const newWidth = Math.max(200, Math.min(500, startWidth + diff));
            leftPanel.style.width = newWidth + 'px';
        } else if (currentHandle === 'right') {
            // Right panel: dragging left = bigger, dragging right = smaller
            const newWidth = Math.max(300, Math.min(700, startWidth - diff));
            rightPanel.style.width = newWidth + 'px';
        }

        // Trigger map resize
        if (window.map) {
            window.map.resize();
        }
    });

    document.addEventListener('mouseup', () => {
        if (isResizing) {
            isResizing = false;
            leftHandle.classList.remove('dragging');
            rightHandle.classList.remove('dragging');
            document.body.classList.remove('resizing');

            // Save layout
            saveLayoutToLocalStorage();

            // Also save to server for cross-device sync
            const config = {
                left_panel_width: leftPanel.offsetWidth,
                right_panel_width: rightPanel.offsetWidth
            };
            fetch('/api/config/layout', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            }).catch(() => {});
        }
    });

    // Load saved layout
    loadLayoutFromLocalStorage();
}

// Initialize vertical resizers for right panel sections
function initVerticalResizers() {
    const surveySection = document.getElementById('surveySection');
    const statsSection = document.getElementById('statsSection');
    const logsSection = document.getElementById('logsSection');
    const handle1 = document.getElementById('resizeHandleSurveyStats');
    const handle2 = document.getElementById('resizeHandleStatsLogs');

    if (!handle1 || !handle2) return;

    let isResizing = false;
    let currentHandle = null;
    let startY = 0;
    let startHeight1 = 0;
    let startHeight2 = 0;

    // Handle 1: Between Survey and Stats
    handle1.addEventListener('mousedown', (e) => {
        isResizing = true;
        currentHandle = 'survey-stats';
        startY = e.clientY;
        startHeight1 = surveySection.offsetHeight;
        startHeight2 = statsSection.offsetHeight;
        handle1.classList.add('dragging');
        document.body.classList.add('resizing-vertical');
        e.preventDefault();
    });

    // Handle 2: Between Stats and Logs
    handle2.addEventListener('mousedown', (e) => {
        isResizing = true;
        currentHandle = 'stats-logs';
        startY = e.clientY;
        startHeight1 = statsSection.offsetHeight;
        startHeight2 = logsSection.offsetHeight;
        handle2.classList.add('dragging');
        document.body.classList.add('resizing-vertical');
        e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
        if (!isResizing) return;

        const diff = e.clientY - startY;
        const minHeight = 80;

        if (currentHandle === 'survey-stats') {
            const newHeight1 = Math.max(minHeight, startHeight1 + diff);
            const newHeight2 = Math.max(minHeight, startHeight2 - diff);

            // Only apply if both heights are valid
            if (newHeight1 >= minHeight && newHeight2 >= minHeight) {
                surveySection.style.height = newHeight1 + 'px';
                surveySection.style.flex = 'none';
                statsSection.style.height = newHeight2 + 'px';
                statsSection.style.flex = 'none';
            }
        } else if (currentHandle === 'stats-logs') {
            const newHeight1 = Math.max(minHeight, startHeight1 + diff);
            const newHeight2 = Math.max(minHeight, startHeight2 - diff);

            if (newHeight1 >= minHeight && newHeight2 >= minHeight) {
                statsSection.style.height = newHeight1 + 'px';
                statsSection.style.flex = 'none';
                logsSection.style.height = newHeight2 + 'px';
                logsSection.style.flex = 'none';
            }
        }
    });

    document.addEventListener('mouseup', () => {
        if (isResizing) {
            isResizing = false;
            handle1.classList.remove('dragging');
            handle2.classList.remove('dragging');
            document.body.classList.remove('resizing-vertical');

            // Save layout
            saveLayoutToLocalStorage();
        }
    });
}

// Call after DOM ready
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
        initPanelResizers();
        initVerticalResizers();
    }, 500);
});
