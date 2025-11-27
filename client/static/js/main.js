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
let currentTimezone = 'UTC';

// Active operations tracking
const activeOperations = new Map(); // id -> { type, label, bdAddress?, startTime, cancellable }

// Operations management functions
function addOperation(id, type, label, options = {}) {
    activeOperations.set(id, {
        type,
        label,
        bdAddress: options.bdAddress || null,
        startTime: Date.now(),
        cancellable: options.cancellable !== false,
        cancelFn: options.cancelFn || null
    });
    updateOperationsBar();
}

function removeOperation(id) {
    activeOperations.delete(id);
    updateOperationsBar();
}

function updateOperationsBar() {
    const opsList = document.getElementById('opsList');
    if (!opsList) return;

    if (activeOperations.size === 0) {
        opsList.innerHTML = '<span class="ops-none">None</span>';
        return;
    }

    opsList.innerHTML = '';
    activeOperations.forEach((op, id) => {
        const item = document.createElement('div');
        item.className = 'ops-item';
        item.dataset.opId = id;

        const elapsed = Math.floor((Date.now() - op.startTime) / 1000);
        const timeStr = elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed/60)}m${elapsed%60}s`;

        let labelHtml = `<span class="ops-type">${op.type}:</span> ${op.label}`;
        if (op.bdAddress) {
            labelHtml += ` <span class="ops-bd">${op.bdAddress.substring(0,8)}...</span>`;
        }
        labelHtml += ` <span class="ops-time">(${timeStr})</span>`;

        item.innerHTML = labelHtml;

        if (op.cancellable && op.cancelFn) {
            const cancelBtn = document.createElement('button');
            cancelBtn.className = 'ops-cancel';
            cancelBtn.innerHTML = '✕';
            cancelBtn.title = 'Cancel operation';
            cancelBtn.onclick = (e) => {
                e.stopPropagation();
                op.cancelFn();
            };
            item.appendChild(cancelBtn);
        }

        opsList.appendChild(item);
    });
}

// Update operation timers every second
setInterval(() => {
    if (activeOperations.size > 0) {
        updateOperationsBar();
    }
}, 1000);

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

    // Fetch hardware stats every 5 seconds
    setInterval(fetchSystemStats, 5000);
    fetchSystemStats();

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
        // Sync state on reconnect
        syncSystemState();
    });

    socket.on('disconnect', () => {
        addLogEntry('Disconnected from server', 'WARNING');
        // Don't reset UI state on disconnect - server operations may still be running
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

    socket.on('geo_ping', (data) => {
        handleGeoPing(data);
    });
}

/**
 * Sync system state on connect/reconnect
 */
function syncSystemState() {
    fetch('/api/state')
        .then(r => r.json())
        .then(state => {
            // Clear existing operations on reconnect
            activeOperations.clear();

            // Sync scanning state
            updateScanStatus(state.scanning);

            // Sync active geo sessions
            activeGeoSessions.clear();
            state.active_geo_sessions.forEach(session => {
                activeGeoSessions.add(session.bd_address);
                updateGeoButtonState(session.bd_address, true);

                // Add to operations bar
                addOperation(`geo-${session.bd_address}`, 'GEO', 'Tracking', {
                    bdAddress: session.bd_address,
                    cancellable: true,
                    cancelFn: () => stopActiveGeo(session.bd_address)
                });

                // If this is the manual tracking target, update panel
                const select = document.getElementById('trackTargetSelect');
                if (select && select.value === session.bd_address) {
                    manualTrackingBd = session.bd_address;
                    document.getElementById('btnStartTrack').disabled = true;
                    document.getElementById('btnStopTrack').disabled = false;
                    document.getElementById('trackTargetSelect').disabled = true;
                    document.getElementById('trackingStatus').textContent = 'ACTIVE';
                    document.getElementById('trackingStatus').className = 'tracking-status active';
                }
            });

            // Check if any tracking session matches our dropdown
            if (state.active_geo_sessions.length > 0) {
                const select = document.getElementById('trackTargetSelect');
                const activeSession = state.active_geo_sessions.find(s => s.bd_address === select?.value);
                if (!activeSession && manualTrackingBd) {
                    // Our tracked target stopped, reset UI
                    if (!state.active_geo_sessions.find(s => s.bd_address === manualTrackingBd)) {
                        manualTrackingBd = null;
                        document.getElementById('btnStartTrack').disabled = false;
                        document.getElementById('btnStopTrack').disabled = true;
                        document.getElementById('trackTargetSelect').disabled = false;
                        document.getElementById('trackingStatus').textContent = '--';
                        document.getElementById('trackingStatus').className = 'tracking-status';
                    }
                }
            }

            addLogEntry(`State synced: scanning=${state.scanning}, geo_sessions=${state.active_geo_sessions.length}`, 'INFO');
        })
        .catch(e => {
            // May fail if not logged in yet
            console.log('State sync failed:', e);
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

// Survey table sorting state
let surveySort = { column: 'last_seen', direction: 'desc' };
let surveyFilter = '';

/**
 * Update the survey table
 */
function updateSurveyTable() {
    const tbody = document.getElementById('surveyTableBody');
    tbody.innerHTML = '';

    // Get filter value
    surveyFilter = (document.getElementById('surveySearch')?.value || '').toLowerCase();

    // Sort devices based on current sort state
    let sortedDevices = Object.values(devices).sort((a, b) => {
        // Targets always first
        if (a.is_target && !b.is_target) return -1;
        if (!a.is_target && b.is_target) return 1;

        // Then by selected column
        let aVal = a[surveySort.column];
        let bVal = b[surveySort.column];

        // Handle special cases
        if (surveySort.column === 'last_seen') {
            aVal = aVal ? new Date(aVal).getTime() : 0;
            bVal = bVal ? new Date(bVal).getTime() : 0;
        } else if (surveySort.column === 'rssi' || surveySort.column === 'emitter_accuracy') {
            aVal = aVal || -999;
            bVal = bVal || -999;
        } else {
            aVal = (aVal || '').toString().toLowerCase();
            bVal = (bVal || '').toString().toLowerCase();
        }

        if (aVal < bVal) return surveySort.direction === 'asc' ? -1 : 1;
        if (aVal > bVal) return surveySort.direction === 'asc' ? 1 : -1;
        return 0;
    });

    sortedDevices.forEach(device => {
        const row = document.createElement('tr');
        row.dataset.bdAddress = device.bd_address;

        if (device.is_target) {
            row.classList.add('target-row');
        }

        // Check if row matches filter (includes bt_company from btmon)
        const searchFields = [
            device.bd_address,
            device.device_name,
            device.manufacturer,
            device.bt_company,
            device.device_type
        ].map(f => (f || '').toLowerCase()).join(' ');

        if (surveyFilter && !searchFields.includes(surveyFilter)) {
            row.classList.add('filtered-out');
        }

        const cep = device.emitter_accuracy
            ? `${device.emitter_accuracy.toFixed(0)}m`
            : '--';

        const lastSeen = formatTimeInTimezone(device.last_seen);

        // Device type badge
        const deviceType = device.device_type || 'unknown';
        const typeBadge = `<span class="device-type-badge ${deviceType}">${deviceType === 'ble' ? 'LE' : deviceType === 'classic' ? 'CL' : '?'}</span>`;

        row.innerHTML = `
            <td class="bd-address">${device.bd_address}</td>
            <td title="${device.device_name || 'Unknown'}">${truncate(device.device_name || 'Unknown', 10)}</td>
            <td>${typeBadge}</td>
            <td>${device.rssi || '--'}</td>
            <td class="cep-cell">${cep}</td>
            <td>${lastSeen}</td>
            <td>
                <button class="action-btn" onclick="getDeviceInfo('${device.bd_address}')" title="Get Info">i</button>
                <button class="action-btn geo-btn" onclick="toggleActiveGeo('${device.bd_address}')" title="Track Location" data-bd="${device.bd_address}">&#128205;</button>
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

    // Update visible count
    const visibleCount = sortedDevices.filter(d => {
        const searchFields = [d.bd_address, d.device_name, d.manufacturer, d.bt_company, d.device_type]
            .map(f => (f || '').toLowerCase()).join(' ');
        return !surveyFilter || searchFields.includes(surveyFilter);
    }).length;
    document.getElementById('surveyCount').textContent = surveyFilter
        ? `${visibleCount}/${Object.keys(devices).length}`
        : Object.keys(devices).length;
}

/**
 * Sort survey table by column
 */
function sortSurveyTable(column) {
    // Toggle direction if same column
    if (surveySort.column === column) {
        surveySort.direction = surveySort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        surveySort.column = column;
        surveySort.direction = 'desc';
    }

    // Update header classes
    document.querySelectorAll('.survey-table th.sortable').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.sort === column) {
            th.classList.add(surveySort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    });

    updateSurveyTable();
}

/**
 * Filter survey table
 */
function filterSurveyTable() {
    updateSurveyTable();
}

/**
 * Clear survey filter
 */
function clearSurveyFilter() {
    document.getElementById('surveySearch').value = '';
    surveyFilter = '';
    updateSurveyTable();
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

    // Only add marker if we have valid location (not 0,0 which is null island)
    const lat = parseFloat(device.emitter_lat);
    const lon = parseFloat(device.emitter_lon);

    if (!lat || !lon || lat === 0 || lon === 0 || isNaN(lat) || isNaN(lon) ||
        lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        return;
    }

    // Create marker element
    const el = document.createElement('div');
    el.className = device.is_target ? 'target-marker' : 'device-marker';

    // For target markers, add inner element for animation (prevents CSS transform conflict)
    if (device.is_target) {
        const inner = document.createElement('div');
        inner.className = 'target-marker-inner';
        el.appendChild(inner);
    }

    // Build popup with enhanced device info
    const typeLabel = device.device_type === 'ble' ? 'BLE' : device.device_type === 'classic' ? 'Classic' : 'Unknown';
    const mfr = device.bt_company || device.manufacturer || 'Unknown';
    const cepStr = device.emitter_accuracy ? `${device.emitter_accuracy.toFixed(1)}m` : 'N/A';

    // Create marker
    const marker = new mapboxgl.Marker(el)
        .setLngLat([lon, lat])
        .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML(`
            <strong>${device.bd_address}</strong><br>
            Name: ${device.device_name || 'Unknown'}<br>
            Type: ${typeLabel}<br>
            Manufacturer: ${mfr}<br>
            RSSI: ${device.rssi || '--'} dBm<br>
            CEP: ${cepStr}<br>
            Last Seen: ${formatTimeInTimezone(device.last_seen)}
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

    // Validate emitter location with robust parsing
    const lat = parseFloat(device.emitter_lat);
    const lon = parseFloat(device.emitter_lon);

    if (!lat || !lon || lat === 0 || lon === 0 || isNaN(lat) || isNaN(lon) ||
        lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        return; // Don't add CEP if no valid location
    }

    // Create circle GeoJSON
    const circle = createGeoJSONCircle(
        [lon, lat],
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
    // Validate location (not null, not 0,0)
    if (!location.lat || !location.lon ||
        (location.lat === 0 && location.lon === 0) ||
        isNaN(location.lat) || isNaN(location.lon)) {
        return;
    }

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
    let located = 0;
    let totalRssi = 0;
    let rssiCount = 0;
    const manufacturerCounts = {};

    Object.values(devices).forEach(device => {
        if (device.is_target) targetCount++;
        if (device.device_type === 'classic') classic++;
        else if (device.device_type === 'ble') ble++;

        // Count devices with location data
        if (device.emitter_lat && device.emitter_lon) located++;

        // Calculate average RSSI
        if (device.rssi && device.rssi !== '--') {
            totalRssi += parseInt(device.rssi);
            rssiCount++;
        }

        // Count manufacturers
        const mfr = device.bt_company || device.manufacturer || 'Unknown';
        if (mfr && mfr !== 'Unknown') {
            manufacturerCounts[mfr] = (manufacturerCounts[mfr] || 0) + 1;
        }
    });

    // Update chart
    deviceTypeChart.data.datasets[0].data = [classic, ble, targetCount];
    deviceTypeChart.update();

    // Update stat boxes
    const totalDevices = Object.keys(devices).length;
    document.getElementById('statTotalDevices').textContent = totalDevices;
    document.getElementById('statClassic').textContent = classic;
    document.getElementById('statBLE').textContent = ble;
    document.getElementById('statTargets').textContent = targetCount;
    document.getElementById('statLocated').textContent = located;

    // Average RSSI
    const avgRssi = rssiCount > 0 ? Math.round(totalRssi / rssiCount) : '--';
    document.getElementById('statAvgRSSI').textContent = avgRssi === '--' ? '--' : `${avgRssi}`;

    // Top manufacturer
    let topMfr = '--';
    let topMfrCount = 0;
    Object.entries(manufacturerCounts).forEach(([mfr, count]) => {
        if (count > topMfrCount) {
            topMfrCount = count;
            topMfr = mfr.length > 15 ? mfr.substring(0, 12) + '...' : mfr;
        }
    });
    document.getElementById('statTopManufacturer').textContent = topMfr;
}

// Session timer
let sessionStartTime = Date.now();

function updateSessionTime() {
    const elapsed = Math.floor((Date.now() - sessionStartTime) / 1000);
    const hours = Math.floor(elapsed / 3600).toString().padStart(2, '0');
    const minutes = Math.floor((elapsed % 3600) / 60).toString().padStart(2, '0');
    const seconds = (elapsed % 60).toString().padStart(2, '0');
    const el = document.getElementById('statSessionTime');
    if (el) el.textContent = `${hours}:${minutes}:${seconds}`;
}

// Update session time every second
setInterval(updateSessionTime, 1000);

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
    const opId = `stim-${type}-${Date.now()}`;
    const label = type === 'ble' ? 'BLE Stimulation' : 'Classic Stimulation';
    addOperation(opId, 'STIM', label, { cancellable: false });
    addLogEntry(`Starting ${type} stimulation scan...`, 'INFO');
    fetch('/api/scan/stimulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: type })
    })
        .then(response => response.json())
        .then(data => {
            removeOperation(opId);
            addLogEntry(`Stimulation complete: ${data.count} devices found`, 'INFO');
        })
        .catch(error => {
            removeOperation(opId);
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
    const activityIndicator = document.getElementById('activityIndicator');

    if (isScanning) {
        indicator.classList.add('active');
        statusText.textContent = 'SCANNING';
        startBtn.disabled = true;
        stopBtn.disabled = false;
        // Activate radar animation
        if (activityIndicator) {
            activityIndicator.classList.add('active');
        }
        // Track in operations bar
        addOperation('scan', 'SCAN', 'BT Scan Active', {
            cancellable: true,
            cancelFn: () => stopScan()
        });
    } else {
        indicator.classList.remove('active');
        statusText.textContent = 'STANDBY';
        startBtn.disabled = false;
        stopBtn.disabled = true;
        // Deactivate radar animation
        if (activityIndicator) {
            activityIndicator.classList.remove('active');
        }
        // Remove from operations bar
        removeOperation('scan');
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

        // Re-apply 3D terrain if enabled
        if (show3dTerrain) {
            if (!map.getSource('mapbox-dem')) {
                map.addSource('mapbox-dem', {
                    type: 'raster-dem',
                    url: 'mapbox://mapbox.mapbox-terrain-dem-v1',
                    tileSize: 512,
                    maxzoom: 14
                });
            }
            map.setTerrain({ source: 'mapbox-dem', exaggeration: 1.5 });
        }

        // Re-apply 3D buildings if enabled
        if (show3dBuildings && !map.getLayer('3d-buildings')) {
            const layers = map.getStyle().layers;
            let labelLayerId;
            for (let i = 0; i < layers.length; i++) {
                if (layers[i].type === 'symbol' && layers[i].layout['text-field']) {
                    labelLayerId = layers[i].id;
                    break;
                }
            }

            map.addLayer({
                id: '3d-buildings',
                source: 'composite',
                'source-layer': 'building',
                filter: ['==', 'extrude', 'true'],
                type: 'fill-extrusion',
                minzoom: 15,
                paint: {
                    'fill-extrusion-color': '#1a2633',
                    'fill-extrusion-height': ['get', 'height'],
                    'fill-extrusion-base': ['get', 'min_height'],
                    'fill-extrusion-opacity': 0.7
                }
            }, labelLayerId);
        }
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

// ==================== 3D TERRAIN & BUILDINGS ====================

let show3dTerrain = false;
let show3dBuildings = false;

function toggle3dTerrain() {
    show3dTerrain = document.getElementById('toggle3dTerrain').checked;

    if (show3dTerrain) {
        // Add terrain source if not present
        if (!map.getSource('mapbox-dem')) {
            map.addSource('mapbox-dem', {
                type: 'raster-dem',
                url: 'mapbox://mapbox.mapbox-terrain-dem-v1',
                tileSize: 512,
                maxzoom: 14
            });
        }
        map.setTerrain({ source: 'mapbox-dem', exaggeration: 1.5 });
        addLogEntry('3D terrain enabled', 'INFO');
    } else {
        map.setTerrain(null);
        addLogEntry('3D terrain disabled', 'INFO');
    }
}

function toggle3dBuildings() {
    show3dBuildings = document.getElementById('toggle3dBuildings').checked;

    if (show3dBuildings) {
        // Add 3D buildings layer if not present
        if (!map.getLayer('3d-buildings')) {
            // Find the first symbol layer for proper layering
            const layers = map.getStyle().layers;
            let labelLayerId;
            for (let i = 0; i < layers.length; i++) {
                if (layers[i].type === 'symbol' && layers[i].layout['text-field']) {
                    labelLayerId = layers[i].id;
                    break;
                }
            }

            map.addLayer({
                id: '3d-buildings',
                source: 'composite',
                'source-layer': 'building',
                filter: ['==', 'extrude', 'true'],
                type: 'fill-extrusion',
                minzoom: 15,
                paint: {
                    'fill-extrusion-color': '#1a2633',
                    'fill-extrusion-height': ['get', 'height'],
                    'fill-extrusion-base': ['get', 'min_height'],
                    'fill-extrusion-opacity': 0.7
                }
            }, labelLayerId);
        } else {
            map.setLayoutProperty('3d-buildings', 'visibility', 'visible');
        }
        addLogEntry('3D buildings enabled', 'INFO');
    } else {
        if (map.getLayer('3d-buildings')) {
            map.setLayoutProperty('3d-buildings', 'visibility', 'none');
        }
        addLogEntry('3D buildings disabled', 'INFO');
    }
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
    event.preventDefault();
    contextMenuTarget = bdAddress;

    let menu = document.getElementById('targetContextMenu');
    if (!menu) {
        menu = createTargetContextMenu();
    }

    // Update menu items based on device type
    const device = devices[bdAddress];
    const isBLE = device && device.device_type === 'ble';

    // Update stimulate option for BLE devices
    const stimItem = menu.querySelector('[onclick*="stimulate"]');
    if (stimItem) {
        if (isBLE) {
            stimItem.innerHTML = '<i>📡</i> Stimulate (not for BLE)';
            stimItem.classList.add('disabled');
            stimItem.title = 'Classic BT stimulation not available for BLE devices';
        } else {
            stimItem.innerHTML = '<i>📡</i> Stimulate (BT Classic)';
            stimItem.classList.remove('disabled');
            stimItem.title = '';
        }
    }

    // Update locate option
    const locateItem = menu.querySelector('[onclick*="locate"]');
    if (locateItem) {
        if (isBLE) {
            locateItem.innerHTML = '<i>📍</i> Geolocate (RSSI only)';
            locateItem.title = 'BLE devices use RSSI-based tracking only';
        } else {
            locateItem.innerHTML = '<i>📍</i> Geolocate Device';
            locateItem.title = '';
        }
    }

    // Position the menu with smart repositioning
    positionContextMenu(menu, event.clientX, event.clientY);
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
    const isBLE = device && device.device_type === 'ble';

    switch (action) {
        case 'info':
            getDeviceInfo(bdAddress);
            break;
        case 'name':
            requestDeviceName(bdAddress);
            break;
        case 'stimulate':
            // Block stimulate for BLE devices
            if (isBLE) {
                addLogEntry('Classic BT stimulation is not available for BLE devices', 'WARNING');
            } else {
                stimulateForDevice(bdAddress);
            }
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
            updateSmsListSettings(data);
        });
}

function updateSmsList(numbers) {
    const list = document.getElementById('smsList');
    if (!list) return;
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

function updateSmsListSettings(numbers) {
    const list = document.getElementById('smsListSettings');
    if (!list) return;
    list.innerHTML = '';

    if (numbers.length === 0) {
        list.innerHTML = '<div class="settings-hint">No SMS numbers configured</div>';
        return;
    }

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
    const input = document.getElementById('smsNumber');
    if (!input) return;
    const phone = input.value.trim();
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
                input.value = '';
                addLogEntry(`SMS number added: ${phone}`, 'INFO');
            }
        })
        .catch(error => {
            addLogEntry('Failed to add SMS number: ' + error, 'ERROR');
        });
}

function addSmsNumberFromSettings() {
    const input = document.getElementById('settingSmsNumber');
    if (!input) return;
    const phone = input.value.trim();
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
                input.value = '';
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
            updateRadioListSettings(data);
        });
}

function updateRadioList(radios) {
    const list = document.getElementById('radioList');
    if (!list) return;
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

function updateRadioListSettings(radios) {
    const list = document.getElementById('radioListSettings');
    if (!list) return;
    list.innerHTML = '';

    if (radios.bluetooth.length === 0 && radios.wifi.length === 0) {
        list.innerHTML = '<div class="settings-hint">No radios detected</div>';
        return;
    }

    // Bluetooth radios
    radios.bluetooth.forEach(radio => {
        const item = document.createElement('div');
        item.className = `radio-item ${radio.status === 'up' ? '' : 'inactive'}`;
        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary">${radio.interface} (Bluetooth)</span>
                <span class="item-secondary">${radio.bd_address}</span>
                <span class="item-status ${radio.status === 'up' ? 'status-up' : 'status-down'}">${radio.status.toUpperCase()}</span>
            </div>
            <button class="btn btn-sm ${radio.status === 'up' ? 'btn-danger' : 'btn-success'}" onclick="toggleRadio('${radio.interface}', 'bluetooth', '${radio.status}')">
                ${radio.status === 'up' ? 'Disable' : 'Enable'}
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
                <span class="item-status ${radio.status === 'up' ? 'status-up' : 'status-down'}">${radio.status.toUpperCase()}</span>
            </div>
            <button class="btn btn-sm ${radio.status === 'up' ? 'btn-danger' : 'btn-success'}" onclick="toggleRadio('${radio.interface}', 'wifi', '${radio.status}')">
                ${radio.status === 'up' ? 'Disable' : 'Enable'}
            </button>
        `;
        list.appendChild(item);
    });
}

function refreshRadiosInSettings() {
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

    if (location && location.status === 'ok' && location.lat && location.lat !== 0) {
        statusEl.textContent = 'OK';
        statusEl.className = 'gps-status connected';
    } else if (location && location.status === 'error') {
        statusEl.textContent = 'ERROR';
        statusEl.className = 'gps-status error';
    } else if (location && location.status === 'no_fix') {
        statusEl.textContent = 'NO FIX';
        statusEl.className = 'gps-status warning';
    } else {
        statusEl.textContent = 'NO FIX';
        statusEl.className = 'gps-status warning';
    }
}

// ==================== DEVICE INFO ====================

function getDeviceInfo(bdAddress) {
    addLogEntry(`Querying hcitool info for ${bdAddress}...`, 'INFO');

    // Track in operations bar
    addOperation(`info-${bdAddress}`, 'INFO', 'Device Query', {
        bdAddress,
        cancellable: false
    });

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
    // Remove from operations bar
    removeOperation(`info-${info.bd_address}`);

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
    const deviceTypeLabel = device.device_type === 'ble' ? 'BLE (Low Energy)' :
                           device.device_type === 'classic' ? 'Classic' : 'Unknown';
    const mfrDisplay = device.bt_company || device.manufacturer || 'Unknown';

    const fields = [
        ['BD Address', info.bd_address],
        ['Device Name', info.device_name || device.device_name || 'Unknown'],
        ['Device Type', deviceTypeLabel],
        ['OUI Manufacturer', device.manufacturer || 'Unknown'],
        ['BT Company', device.bt_company || 'N/A'],
        ['Device Class', info.device_class || 'N/A'],
        ['Address Type', device.addr_type || 'N/A'],
        ['RSSI', device.rssi ? `${device.rssi} dBm` : 'N/A'],
        ['TX Power', device.tx_power ? `${device.tx_power} dBm` : 'N/A'],
        ['First Seen', formatDateTimeInTimezone(device.first_seen)],
        ['Last Seen', formatDateTimeInTimezone(device.last_seen)],
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
    details += `Time: ${formatDateTimeInTimezone(new Date())}\n`;
    if (device.first_seen) details += `First Seen: ${formatDateTimeInTimezone(device.first_seen)}\n`;
    if (device.last_seen) details += `Last Seen: ${formatDateTimeInTimezone(device.last_seen)}\n`;
    if (device.rssi) details += `RSSI: ${device.rssi} dBm\n`;
    if (device.tx_power) details += `TX Power: ${device.tx_power} dBm\n`;
    if (device.device_type) details += `Type: ${device.device_type === 'ble' ? 'BLE' : device.device_type === 'classic' ? 'Classic' : device.device_type}\n`;
    if (device.addr_type) details += `Address Type: ${device.addr_type}\n`;
    if (device.manufacturer) details += `OUI Manufacturer: ${device.manufacturer}\n`;
    if (device.bt_company) details += `BT Company: ${device.bt_company}\n`;
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
    if (d.tx_power) text += `TX Power: ${d.tx_power} dBm\n`;
    if (d.device_type) text += `Type: ${d.device_type === 'ble' ? 'BLE' : d.device_type === 'classic' ? 'Classic' : d.device_type}\n`;
    if (d.addr_type) text += `Address Type: ${d.addr_type}\n`;
    if (d.manufacturer) text += `OUI Manufacturer: ${d.manufacturer}\n`;
    if (d.bt_company) text += `BT Company: ${d.bt_company}\n`;
    if (d.first_seen) text += `First Seen: ${formatDateTimeInTimezone(d.first_seen)}\n`;
    if (d.last_seen) text += `Last Seen: ${formatDateTimeInTimezone(d.last_seen)}\n`;

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

    const time = formatTimeInTimezone(new Date());
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
    const options = {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        timeZone: currentTimezone
    };
    try {
        document.getElementById('sysTime').textContent = now.toLocaleTimeString('en-US', options);
    } catch (e) {
        // Fallback if timezone is invalid
        document.getElementById('sysTime').textContent = now.toLocaleTimeString();
    }
}

function formatTimeInTimezone(date) {
    if (!date) return '--';
    const options = {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        timeZone: currentTimezone
    };
    try {
        return new Date(date).toLocaleTimeString('en-US', options);
    } catch (e) {
        return new Date(date).toLocaleTimeString();
    }
}

function formatDateTimeInTimezone(date) {
    if (!date) return '--';
    const options = {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        timeZone: currentTimezone
    };
    try {
        return new Date(date).toLocaleString('en-US', options);
    } catch (e) {
        return new Date(date).toLocaleString();
    }
}

// ==================== HARDWARE MONITORING ====================

function fetchSystemStats() {
    fetch('/api/system/stats')
        .then(r => r.json())
        .then(stats => {
            updateHardwareDisplay(stats);
        })
        .catch(e => {
            // Silent fail - hardware stats are non-critical
        });
}

function updateHardwareDisplay(stats) {
    // CPU Usage
    const cpuEl = document.getElementById('cpuUsage');
    if (cpuEl && stats.cpu_percent !== null) {
        cpuEl.textContent = `${stats.cpu_percent}%`;
        cpuEl.classList.remove('warning', 'danger');
        if (stats.cpu_percent > 80) {
            cpuEl.classList.add('danger');
        } else if (stats.cpu_percent > 60) {
            cpuEl.classList.add('warning');
        }
    }

    // CPU Temperature
    const tempEl = document.getElementById('cpuTemp');
    if (tempEl && stats.cpu_temp !== null) {
        tempEl.textContent = `${stats.cpu_temp}°C`;
        tempEl.classList.remove('warning', 'danger');
        if (stats.cpu_temp > 70) {
            tempEl.classList.add('danger');
        } else if (stats.cpu_temp > 55) {
            tempEl.classList.add('warning');
        }
    } else if (tempEl) {
        tempEl.textContent = '--°C';
    }

    // Memory Usage
    const memEl = document.getElementById('memUsage');
    if (memEl && stats.memory_percent !== null) {
        memEl.textContent = `${stats.memory_percent}%`;
        memEl.classList.remove('warning', 'danger');
        if (stats.memory_percent > 85) {
            memEl.classList.add('danger');
        } else if (stats.memory_percent > 70) {
            memEl.classList.add('warning');
        }
    }
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

    // Update locate option based on device type
    const locateItem = menu.querySelector('[onclick*="locate"]');
    if (locateItem) {
        if (device && device.device_type === 'ble') {
            locateItem.innerHTML = '<i>📍</i> Geolocate (RSSI only)';
            locateItem.title = 'BLE devices use RSSI-based tracking only';
        } else {
            locateItem.innerHTML = '<i>📍</i> Geolocate Device';
            locateItem.title = '';
        }
    }

    // Update info option based on device type
    const infoItem = menu.querySelector('[onclick*="info"]');
    if (infoItem) {
        if (device && device.device_type === 'ble') {
            infoItem.innerHTML = '<i>ℹ</i> Get Device Info (limited)';
            infoItem.title = 'hcitool info has limited results for BLE devices';
        } else {
            infoItem.innerHTML = '<i>ℹ</i> Get Device Info';
            infoItem.title = '';
        }
    }

    // Position the menu with smart repositioning
    positionContextMenu(menu, e.clientX, e.clientY);
}

/**
 * Position context menu to stay within viewport bounds
 */
function positionContextMenu(menu, x, y) {
    // Reset position to calculate natural size
    menu.style.left = '0';
    menu.style.top = '0';
    menu.classList.remove('hidden');

    const rect = menu.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const padding = 10; // Padding from edges

    // Calculate best position
    let finalX = x;
    let finalY = y;

    // Horizontal positioning
    if (x + rect.width + padding > viewportWidth) {
        // Menu would overflow right - position to left of cursor
        finalX = Math.max(padding, x - rect.width);
    }

    // Vertical positioning
    if (y + rect.height + padding > viewportHeight) {
        // Menu would overflow bottom
        if (rect.height > viewportHeight - 2 * padding) {
            // Menu is taller than viewport - position at top with scroll
            finalY = padding;
        } else {
            // Position above cursor
            finalY = Math.max(padding, y - rect.height);
        }
    }

    menu.style.left = finalX + 'px';
    menu.style.top = finalY + 'px';
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
                // Track in operations bar
                addOperation(`name-${bdAddress}`, 'NAME', 'Name Query', {
                    bdAddress,
                    cancellable: true,
                    cancelFn: () => stopNameRetrieval(bdAddress)
                });
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
            // Remove from operations bar
            removeOperation(`name-${bdAddress}`);
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
    loadSmsNumbers();
    loadRadios();
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

            // Update version footer with system ID
            const footerIdEl = document.getElementById('systemIdFooter');
            if (footerIdEl) {
                footerIdEl.textContent = data.system_id || 'BK9-001';
            }
            document.getElementById('settingScanInterval').value = data.scan_interval || 2;
            document.getElementById('settingSmsInterval').value = data.sms_alert_interval || 60;

            // Display settings
            document.getElementById('settingTimezone').value = data.timezone || 'UTC';
            currentTimezone = data.timezone || 'UTC';

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
        timezone: document.getElementById('settingTimezone').value,
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

    // Update global timezone
    currentTimezone = settings.timezone;

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
                // Update version footer with new system ID
                const footerIdEl = document.getElementById('systemIdFooter');
                if (footerIdEl && settings.system_id) {
                    footerIdEl.textContent = settings.system_id;
                }
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
            // Dragging down (positive diff) expands logs (below), shrinks stats (above)
            const newStatsHeight = Math.max(minHeight, startHeight1 - diff);

            // Only set explicit height on stats section
            // Logs section uses flex: 1 to fill remaining space (bottom-locked)
            if (newStatsHeight >= minHeight) {
                statsSection.style.height = newStatsHeight + 'px';
                statsSection.style.flex = 'none';
                // Let logs fill remaining space - removes bottom float issue
                logsSection.style.height = 'auto';
                logsSection.style.flex = '1';
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
        loadPanelLayout();
        refreshTargetList();
    }, 500);
});

// ==================== DOCKABLE PANELS ====================

let panelDragState = null;
let panelLayout = {
    leftPanel: 'left',
    rightPanel: 'right',
    leftCollapsed: false,
    rightCollapsed: false,
    rightDock: 'right' // 'right' or 'bottom'
};

/**
 * Start dragging a panel
 */
function startPanelDrag(event, panelId) {
    // Only start drag on left mouse button
    if (event.button !== 0) return;

    const panel = document.getElementById(panelId);
    if (!panel) return;

    // Prevent text selection during drag
    event.preventDefault();

    panelDragState = {
        panelId: panelId,
        panel: panel,
        startX: event.clientX,
        startY: event.clientY,
        isDragging: false
    };

    document.addEventListener('mousemove', handlePanelDrag);
    document.addEventListener('mouseup', endPanelDrag);
}

/**
 * Handle panel drag movement
 */
function handlePanelDrag(event) {
    if (!panelDragState) return;

    const dx = Math.abs(event.clientX - panelDragState.startX);
    const dy = Math.abs(event.clientY - panelDragState.startY);

    // Start actual drag after moving 10px
    if (!panelDragState.isDragging && (dx > 10 || dy > 10)) {
        panelDragState.isDragging = true;
        panelDragState.panel.classList.add('dragging');
        showDockZones();
    }

    if (panelDragState.isDragging) {
        highlightDockZone(event.clientX, event.clientY);
    }
}

/**
 * End panel drag
 */
function endPanelDrag(event) {
    if (!panelDragState) return;

    document.removeEventListener('mousemove', handlePanelDrag);
    document.removeEventListener('mouseup', endPanelDrag);

    if (panelDragState.isDragging) {
        panelDragState.panel.classList.remove('dragging');
        hideDockZones();

        // Check if we should dock to a new position
        const dockZone = getDockZoneAtPosition(event.clientX, event.clientY);
        if (dockZone) {
            dockPanelTo(panelDragState.panelId, dockZone);
        }
    }

    panelDragState = null;
}

/**
 * Show dock zone indicators
 */
function showDockZones() {
    let indicator = document.getElementById('dockIndicator');
    if (!indicator) {
        indicator = document.createElement('div');
        indicator.id = 'dockIndicator';
        indicator.className = 'dock-indicator';
        indicator.innerHTML = `
            <div class="dock-highlight dock-left" data-zone="left"></div>
            <div class="dock-highlight dock-right" data-zone="right"></div>
        `;
        document.body.appendChild(indicator);
    }
    indicator.classList.add('active');

    const mainContent = document.querySelector('.main-content');
    if (mainContent) {
        const rect = mainContent.getBoundingClientRect();
        const leftHighlight = indicator.querySelector('.dock-left');
        const rightHighlight = indicator.querySelector('.dock-right');

        leftHighlight.style.cssText = `
            left: ${rect.left}px;
            top: ${rect.top}px;
            width: 100px;
            height: ${rect.height}px;
            opacity: 0;
        `;
        rightHighlight.style.cssText = `
            left: ${rect.right - 100}px;
            top: ${rect.top}px;
            width: 100px;
            height: ${rect.height}px;
            opacity: 0;
        `;
    }
}

/**
 * Hide dock zone indicators
 */
function hideDockZones() {
    const indicator = document.getElementById('dockIndicator');
    if (indicator) {
        indicator.classList.remove('active');
    }
}

/**
 * Highlight the dock zone at cursor position
 */
function highlightDockZone(x, y) {
    const indicator = document.getElementById('dockIndicator');
    if (!indicator) return;

    const leftHighlight = indicator.querySelector('.dock-left');
    const rightHighlight = indicator.querySelector('.dock-right');

    const mainContent = document.querySelector('.main-content');
    if (!mainContent) return;

    const rect = mainContent.getBoundingClientRect();

    if (x < rect.left + 150) {
        leftHighlight.style.opacity = '1';
        rightHighlight.style.opacity = '0';
    } else if (x > rect.right - 150) {
        leftHighlight.style.opacity = '0';
        rightHighlight.style.opacity = '1';
    } else {
        leftHighlight.style.opacity = '0';
        rightHighlight.style.opacity = '0';
    }
}

/**
 * Get dock zone at cursor position
 */
function getDockZoneAtPosition(x, y) {
    const mainContent = document.querySelector('.main-content');
    if (!mainContent) return null;

    const rect = mainContent.getBoundingClientRect();

    if (x < rect.left + 150) return 'left';
    if (x > rect.right - 150) return 'right';
    return null;
}

/**
 * Dock a panel to a new position
 */
function dockPanelTo(panelId, position) {
    const panel = document.getElementById(panelId);
    if (!panel) return;

    const leftPanel = document.getElementById('panelLeft');
    const rightPanel = document.getElementById('panelRight');

    const panelIsLeft = panel === leftPanel;
    const targetIsLeft = position === 'left';

    if ((panelIsLeft && !targetIsLeft) || (!panelIsLeft && targetIsLeft)) {
        swapPanels();
    }

    savePanelLayout();
}

/**
 * Swap left and right panels
 */
function swapPanels() {
    const leftPanel = document.getElementById('panelLeft');
    const rightPanel = document.getElementById('panelRight');
    const mainContent = document.querySelector('.main-content');
    const leftHandle = document.getElementById('resizeHandleLeft');
    const rightHandle = document.getElementById('resizeHandleRight');

    if (!leftPanel || !rightPanel || !mainContent) return;

    // Toggle classes
    leftPanel.classList.toggle('panel-left');
    leftPanel.classList.toggle('panel-right');
    rightPanel.classList.toggle('panel-left');
    rightPanel.classList.toggle('panel-right');

    // Swap widths
    const leftWidth = leftPanel.style.width;
    const rightWidth = rightPanel.style.width;
    leftPanel.style.width = rightWidth || '420px';
    rightPanel.style.width = leftWidth || '280px';

    // Reorder elements in DOM
    if (leftPanel.classList.contains('panel-right')) {
        mainContent.insertBefore(rightPanel, leftHandle);
        mainContent.appendChild(leftPanel);
    } else {
        mainContent.insertBefore(leftPanel, leftHandle);
        mainContent.appendChild(rightPanel);
    }

    // Update layout state
    panelLayout.leftPanel = leftPanel.classList.contains('panel-left') ? 'left' : 'right';
    panelLayout.rightPanel = rightPanel.classList.contains('panel-left') ? 'left' : 'right';

    savePanelLayout();
    addLogEntry('Panel layout swapped', 'INFO');
}

/**
 * Toggle panel collapse state
 */
function togglePanelCollapse(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;

    panel.classList.toggle('collapsed');

    const btn = panel.querySelector('.collapse-btn');
    if (btn) {
        btn.innerHTML = panel.classList.contains('collapsed') ? '+' : '&#8722;';
    }

    if (panelId === 'panelLeft') {
        panelLayout.leftCollapsed = panel.classList.contains('collapsed');
    } else if (panelId === 'panelRight') {
        panelLayout.rightCollapsed = panel.classList.contains('collapsed');
    }

    savePanelLayout();
}

/**
 * Save panel layout to local storage
 */
function savePanelLayout() {
    localStorage.setItem('bluek9_panel_layout', JSON.stringify(panelLayout));
}

/**
 * Toggle right panel between right side and bottom dock
 */
function toggleBottomDock() {
    const rightPanel = document.getElementById('panelRight');
    const mainContent = document.querySelector('.main-content');
    const resizeHandle = document.getElementById('resizeHandleRight');

    if (!rightPanel || !mainContent) return;

    if (panelLayout.rightDock === 'right') {
        // Move to bottom
        panelLayout.rightDock = 'bottom';
        rightPanel.classList.add('docked-bottom');
        rightPanel.classList.remove('panel-right');
        if (resizeHandle) resizeHandle.classList.add('hidden');

        // Move panel to after main-content
        const appContainer = document.querySelector('.app-container');
        if (appContainer) {
            appContainer.appendChild(rightPanel);
        }

        // Reset width and set height
        rightPanel.style.width = '100%';
        rightPanel.style.height = '300px';

        addLogEntry('Survey panel docked to bottom', 'INFO');
    } else {
        // Move to right
        panelLayout.rightDock = 'right';
        rightPanel.classList.remove('docked-bottom');
        rightPanel.classList.add('panel-right');
        if (resizeHandle) resizeHandle.classList.remove('hidden');

        // Move panel back into main-content
        mainContent.appendChild(rightPanel);

        // Reset height and set width
        rightPanel.style.width = '420px';
        rightPanel.style.height = '';

        addLogEntry('Survey panel docked to right', 'INFO');
    }

    // Update button icon
    const dockBtn = rightPanel.querySelector('.dock-bottom-btn');
    if (dockBtn) {
        dockBtn.innerHTML = panelLayout.rightDock === 'right' ? '&#8615;' : '&#8614;';
        dockBtn.title = panelLayout.rightDock === 'right' ? 'Dock to Bottom' : 'Dock to Right';
    }

    // Resize map
    if (window.map) {
        setTimeout(() => window.map.resize(), 100);
    }

    savePanelLayout();
}

/**
 * Load panel layout from local storage
 */
function loadPanelLayout() {
    const saved = localStorage.getItem('bluek9_panel_layout');
    if (saved) {
        try {
            panelLayout = { ...panelLayout, ...JSON.parse(saved) };

            const leftPanel = document.getElementById('panelLeft');
            const rightPanel = document.getElementById('panelRight');

            if (panelLayout.leftCollapsed && leftPanel) {
                leftPanel.classList.add('collapsed');
                const btn = leftPanel.querySelector('.collapse-btn');
                if (btn) btn.innerHTML = '+';
            }

            if (panelLayout.rightCollapsed && rightPanel) {
                rightPanel.classList.add('collapsed');
                const btn = rightPanel.querySelector('.collapse-btn');
                if (btn) btn.innerHTML = '+';
            }

            if (panelLayout.leftPanel === 'right') {
                swapPanels();
            }

            // Apply bottom dock if saved
            if (panelLayout.rightDock === 'bottom') {
                // Need to toggle from default 'right' to 'bottom'
                panelLayout.rightDock = 'right'; // Reset so toggle works
                toggleBottomDock();
            }
        } catch (e) {
            console.error('Failed to load panel layout:', e);
        }
    }
}

// ==================== EXPORT FUNCTIONS ====================

/**
 * Export collection data for offline analysis
 */
function exportCollection(format = 'json') {
    addLogEntry(`Exporting collection data as ${format.toUpperCase()}...`, 'INFO');

    // Track in operations bar
    const opId = `export-${Date.now()}`;
    addOperation(opId, 'EXPORT', `Collection (${format.toUpperCase()})`, { cancellable: false });

    fetch(`/api/logs/export?format=${format}`, {
        credentials: 'same-origin'
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`Export failed: ${response.status} ${response.statusText}`);
        }
        return response.blob();
    })
    .then(blob => {
        removeOperation(opId);
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
        const filename = `bluek9_collection_${timestamp}.${format}`;

        // Create download link
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);

        addLogEntry(`Collection exported: ${filename}`, 'INFO');
    })
    .catch(error => {
        removeOperation(opId);
        addLogEntry(`Export failed: ${error.message}`, 'ERROR');
    });
}

// ==================== ACTIVE GEO TRACKING ====================

// Track which devices have active geo sessions
const activeGeoSessions = new Set();

/**
 * Toggle active geo tracking for a device
 */
function toggleActiveGeo(bdAddress) {
    if (activeGeoSessions.has(bdAddress)) {
        stopActiveGeo(bdAddress);
    } else {
        startActiveGeo(bdAddress);
    }
}

/**
 * Start active geo tracking with continuous l2ping
 */
function startActiveGeo(bdAddress) {
    fetch(`/api/device/${bdAddress}/geo/track`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started' || data.status === 'already_running') {
            activeGeoSessions.add(bdAddress);
            updateGeoButtonState(bdAddress, true);
            addLogEntry(`Active geo tracking started for ${bdAddress}`, 'INFO');
            // Track in operations bar
            addOperation(`geo-${bdAddress}`, 'GEO', 'Tracking', {
                bdAddress,
                cancellable: true,
                cancelFn: () => stopActiveGeo(bdAddress)
            });
        }
    })
    .catch(e => addLogEntry(`Failed to start geo tracking: ${e}`, 'ERROR'));
}

/**
 * Stop active geo tracking
 */
function stopActiveGeo(bdAddress) {
    fetch(`/api/device/${bdAddress}/geo/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    })
    .then(r => r.json())
    .then(data => {
        activeGeoSessions.delete(bdAddress);
        updateGeoButtonState(bdAddress, false);
        addLogEntry(`Active geo tracking stopped for ${bdAddress}`, 'INFO');
        // Remove from operations bar
        removeOperation(`geo-${bdAddress}`);
    })
    .catch(e => addLogEntry(`Failed to stop geo tracking: ${e}`, 'ERROR'));
}

/**
 * Update geo button visual state
 */
function updateGeoButtonState(bdAddress, active) {
    const btn = document.querySelector(`.geo-btn[data-bd="${bdAddress}"]`);
    if (btn) {
        btn.classList.toggle('active', active);
        btn.title = active ? 'Stop Tracking' : 'Track Location';
    }
}

/**
 * Handle geo ping events from server
 */
let manualTrackingBd = null;

function handleGeoPing(data) {
    // Update device RSSI in real-time
    if (data.rssi && devices[data.bd_address]) {
        devices[data.bd_address].rssi = data.rssi;
    }

    // Update tracking stats panel if this is the manual tracking target
    if (data.bd_address === manualTrackingBd) {
        updateTrackingStats(data);
    }

    // Show ping status in log (throttled)
    if (data.ping % 5 === 0 || data.status === 'timeout' || data.status === 'error') {
        const methods = data.methods ? `[${data.methods.join('+')}]` : '';
        const status = data.status === 'timeout' ? 'TIMEOUT' :
            data.status === 'error' ? `ERROR: ${data.error}` :
            `RSSI:${data.rssi || '--'}dBm RTT:${data.rtt || '--'}ms`;
        addLogEntry(`GEO ${methods} ${data.bd_address}: ${status}`, 'DEBUG');
    }
}

/**
 * Get color for RSSI value (red=strong, blue=weak)
 * RSSI typically ranges from -30 (very strong) to -100 (very weak)
 */
function getRssiColor(rssi) {
    if (rssi === null || rssi === undefined || rssi === '--') {
        return '#8b949e'; // Default gray
    }

    // Clamp RSSI between -100 and -30
    const clampedRssi = Math.max(-100, Math.min(-30, rssi));

    // Normalize to 0-1 range (0 = weak/-100, 1 = strong/-30)
    const normalized = (clampedRssi + 100) / 70;

    // Interpolate from blue (weak) to red (strong)
    // Blue: rgb(59, 130, 246)  -> Red: rgb(239, 68, 68)
    const r = Math.round(59 + (239 - 59) * normalized);
    const g = Math.round(130 + (68 - 130) * normalized);
    const b = Math.round(246 + (68 - 246) * normalized);

    return `rgb(${r}, ${g}, ${b})`;
}

/**
 * Update tracking stats display
 */
function updateTrackingStats(data) {
    const statsEl = document.getElementById('trackingStats');
    if (!statsEl) return;

    const rssiColor = getRssiColor(data.rssi);
    const rssiValue = data.rssi !== null && data.rssi !== undefined ? data.rssi : '--';

    statsEl.innerHTML = `
        <div class="stats-grid">
            <div class="stat-item rssi-highlight">
                <span class="stat-label">RSSI</span>
                <span class="stat-value rssi-value" style="color: ${rssiColor}; font-size: 18px; font-weight: bold;">${rssiValue} dBm</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">RTT</span>
                <span class="stat-value">${data.rtt ? data.rtt.toFixed(1) : '--'} ms</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">AVG RTT</span>
                <span class="stat-value">${data.avg_rtt || '--'} ms</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">PINGS</span>
                <span class="stat-value">${data.ping || 0}</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">READINGS</span>
                <span class="stat-value">${data.rssi_readings || 0}</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">SUCCESS</span>
                <span class="stat-value">${data.success_rate || 0}%</span>
            </div>
        </div>
    `;
}

/**
 * Refresh target list in dropdown
 */
function refreshTargetList() {
    fetch('/api/targets')
        .then(r => r.json())
        .then(targets => {
            const select = document.getElementById('trackTargetSelect');
            const currentValue = select.value;

            // Clear existing options except the placeholder
            select.innerHTML = '<option value="">-- Select Target --</option>';

            // Add targets
            targets.forEach(target => {
                const option = document.createElement('option');
                option.value = target.bd_address;
                const device = devices[target.bd_address];
                const name = device?.device_name || target.notes || 'Unknown';
                option.textContent = `${target.bd_address} (${name})`;
                select.appendChild(option);
            });

            // Restore selection if still valid
            if (currentValue && targets.some(t => t.bd_address === currentValue)) {
                select.value = currentValue;
            }

            if (targets.length === 0) {
                addLogEntry('No targets defined. Add targets first.', 'WARNING');
            }
        })
        .catch(e => addLogEntry(`Failed to load targets: ${e}`, 'ERROR'));
}

/**
 * Start target tracking from the panel
 */
function startTargetTracking() {
    const select = document.getElementById('trackTargetSelect');
    const bdAddress = select.value;

    if (!bdAddress) {
        addLogEntry('Select a target to track', 'ERROR');
        return;
    }

    // Get selected methods
    const methods = [];
    if (document.getElementById('methodL2ping').checked) methods.push('l2ping');
    if (document.getElementById('methodRssi').checked) methods.push('rssi');

    if (methods.length === 0) {
        addLogEntry('Select at least one tracking method', 'ERROR');
        return;
    }

    manualTrackingBd = bdAddress;

    fetch(`/api/device/${bdAddress}/geo/track`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ methods: methods })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started' || data.status === 'already_running') {
            activeGeoSessions.add(bdAddress);
            document.getElementById('btnStartTrack').disabled = true;
            document.getElementById('btnStopTrack').disabled = false;
            document.getElementById('trackTargetSelect').disabled = true;
            document.getElementById('trackingStatus').textContent = 'ACTIVE';
            document.getElementById('trackingStatus').className = 'tracking-status active';
            addLogEntry(`Target tracking started for ${bdAddress} (${methods.join('+')})`, 'INFO');
        }
    })
    .catch(e => addLogEntry(`Failed to start tracking: ${e}`, 'ERROR'));
}

/**
 * Stop target tracking
 */
function stopTargetTracking() {
    if (!manualTrackingBd) return;

    fetch(`/api/device/${manualTrackingBd}/geo/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
    })
    .then(r => r.json())
    .then(data => {
        activeGeoSessions.delete(manualTrackingBd);
        document.getElementById('btnStartTrack').disabled = false;
        document.getElementById('btnStopTrack').disabled = true;
        document.getElementById('trackTargetSelect').disabled = false;
        document.getElementById('trackingStatus').textContent = 'STOPPED';
        document.getElementById('trackingStatus').className = 'tracking-status';
        addLogEntry(`Target tracking stopped for ${manualTrackingBd}`, 'INFO');
        manualTrackingBd = null;
    })
    .catch(e => addLogEntry(`Failed to stop tracking: ${e}`, 'ERROR'));
}
