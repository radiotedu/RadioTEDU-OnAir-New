/**
 * RadioTEDU guided first-run provisioning.
 * The backend owns setup state; this overlay collects operator choices,
 * runs tests, and blocks completion until restart-safe readiness is achieved.
 */
(function () {
    const API_BASE = '/api/setup';
    const CHECK_ORDER = [
        'backend',
        'media_runtime',
        'webview2',
        'station',
        'local_output',
        'stream_output',
        'ollama',
        'local_tts_runtime',
        'ai_tts',
        'startup_ai_buffer',
    ];
    const VERIFIABLE = new Set(['local_output', 'stream_output', 'ai_tts']);
    let currentSetupState = null;
    let deviceOptions = [];
    let booted = false;

    function stationId() {
        try {
            const sid = Number(currentState?.currentStationId || 0);
            if (Number.isInteger(sid) && sid > 0) return sid;
        } catch (_) {
            // currentState belongs to app.js.
        }
        const params = new URLSearchParams(window.location.search || '');
        const fromUrl = Number(params.get('station_id') || 0);
        return Number.isInteger(fromUrl) && fromUrl > 0 ? fromUrl : 1;
    }

    function setupFetch(path, options = {}) {
        return apiFetch(path, {
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...(options.headers || {}),
            },
        });
    }

    function byName(checks) {
        const out = {};
        (checks || []).forEach(check => {
            out[String(check?.name || '')] = check;
        });
        return out;
    }

    function checkIcon(check) {
        return check?.ready ? 'check_circle' : 'radio_button_unchecked';
    }

    function checkClass(check) {
        return check?.ready ? 'setup-check-ready' : 'setup-check-needed';
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function fieldValue(id) {
        return document.getElementById(id)?.value ?? '';
    }

    function fieldChecked(id) {
        return !!document.getElementById(id)?.checked;
    }

    function ensureRoot() {
        let root = document.getElementById('setupWizardOverlay');
        if (root) return root;
        root = document.createElement('div');
        root.id = 'setupWizardOverlay';
        root.className = 'setup-wizard-overlay';
        root.innerHTML = `
            <section class="setup-wizard-card" role="dialog" aria-modal="true" aria-labelledby="setupWizardTitle">
                <div class="setup-wizard-hero">
                    <span class="material-icons-round">radio</span>
                    <div>
                        <p>Guided provisioning</p>
                        <h2 id="setupWizardTitle">RadioTEDU OnAir setup</h2>
                    </div>
                </div>
                <p class="setup-wizard-copy">
                    Completion stays blocked until the local monitor, stream output, AI voice path,
                    desktop runtime, and startup intro buffer can survive a restart without dead air.
                </p>
                <div class="setup-wizard-progress" id="setupWizardProgress"></div>
                <div class="setup-wizard-form">
                    <div class="form-row">
                        <div class="form-group">
                            <label for="setupStationName">Station Name</label>
                            <input id="setupStationName" type="text" />
                        </div>
                        <div class="form-group">
                            <label for="setupOutputDevice">Local Monitor Device</label>
                            <select id="setupOutputDevice"></select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label><input id="setupLocalOutputEnabled" type="checkbox" checked> Local monitor enabled</label>
                        </div>
                        <div class="form-group">
                            <label><input id="setupAiEnabled" type="checkbox" checked> AI host enabled</label>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="setupIcecastHost">Icecast Host</label>
                            <input id="setupIcecastHost" type="text" />
                        </div>
                        <div class="form-group">
                            <label for="setupIcecastPort">Port</label>
                            <input id="setupIcecastPort" type="number" min="1" max="65535" />
                        </div>
                        <div class="form-group">
                            <label for="setupIcecastMount">Mount</label>
                            <input id="setupIcecastMount" type="text" />
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="setupIcecastUser">Source User</label>
                            <input id="setupIcecastUser" type="text" />
                        </div>
                        <div class="form-group">
                            <label for="setupIcecastPassword">Source Password</label>
                            <input id="setupIcecastPassword" type="password" />
                        </div>
                        <div class="form-group">
                            <label for="setupStreamCodec">Stream Codec Profile</label>
                            <select id="setupStreamCodec"></select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="setupAiWarmth">AI Host Warmth</label>
                            <select id="setupAiWarmth"></select>
                        </div>
                        <div class="form-group">
                            <label>AI Stack</label>
                            <div class="setup-wizard-static">Hybrid: Ollama + Local Qwen TTS</div>
                        </div>
                        <div class="form-group">
                            <label>Startup Buffer</label>
                            <div class="setup-wizard-static" id="setupStartupState">Waiting for readiness snapshot</div>
                        </div>
                    </div>
                </div>
                <div class="setup-wizard-checks" id="setupWizardChecks"></div>
                <div class="setup-wizard-errors" id="setupWizardErrors" hidden></div>
                <div class="setup-wizard-actions">
                    <button class="btn-secondary" id="setupSaveBtn" type="button">Save Provisioning</button>
                    <button class="btn-secondary" id="setupPreflightBtn" type="button">Run Preflight</button>
                    <button class="btn-secondary" id="setupRepairDepsBtn" type="button">Repair Dependencies</button>
                    <button class="btn-secondary" id="setupTestAiBtn" type="button">Test AI Voice</button>
                    <button class="btn-secondary" id="setupVerifyLocalBtn" type="button">Verify Local Audio</button>
                    <button class="btn-secondary" id="setupVerifyStreamBtn" type="button">Verify Test Stream</button>
                    <button class="btn-secondary" id="setupVerifyAiBtn" type="button">Verify AI/TTS</button>
                    <button class="btn-primary" id="setupCompleteBtn" type="button">Finish Setup</button>
                </div>
            </section>
        `;
        document.body.appendChild(root);
        bindActions(root);
        return root;
    }

    function setBusy(root, busy) {
        root.querySelectorAll('button, input, select').forEach(control => {
            control.disabled = !!busy;
        });
        root.classList.toggle('setup-wizard-busy', !!busy);
    }

    function renderErrors(root, state) {
        const errors = root.querySelector('#setupWizardErrors');
        const reasons = state?.blocking_reasons || [];
        if (!errors || reasons.length === 0) {
            if (errors) {
                errors.hidden = true;
                errors.innerHTML = '';
            }
            return;
        }
        errors.hidden = false;
        errors.innerHTML = reasons.map(reason => `<div>${escapeHtml(reason)}</div>`).join('');
    }

    function populateSelect(select, options, selectedValue, emptyLabel) {
        if (!select) return;
        const rows = [];
        if (emptyLabel) {
            rows.push(`<option value="">${escapeHtml(emptyLabel)}</option>`);
        }
        rows.push(...(options || []).map(option => {
            const value = String(option.id ?? option.value ?? '');
            const label = String(option.label ?? option.display_name ?? option.name ?? value);
            const selected = value === String(selectedValue ?? '') ? ' selected' : '';
            return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(label)}</option>`;
        }));
        select.innerHTML = rows.join('');
        if (selectedValue !== undefined && selectedValue !== null) {
            select.value = String(selectedValue);
        }
    }

    function renderConfig(state) {
        const config = state?.config || {};
        const outputDevice = document.getElementById('setupOutputDevice');
        populateSelect(
            outputDevice,
            deviceOptions.map(device => ({ id: device.id, label: device.label })),
            config.output_device_id || '',
            'System default output'
        );
        populateSelect(document.getElementById('setupStreamCodec'), state?.codec_presets || [], config.stream_codec_profile || 'he_aac_192');
        populateSelect(document.getElementById('setupAiWarmth'), state?.warmth_presets || [], config.ai_warmth || 'warm');

        const assignments = {
            setupStationName: config.station_name || 'RadioTEDU OnAir',
            setupIcecastHost: config.icecast_host || '127.0.0.1',
            setupIcecastPort: config.icecast_port || 8000,
            setupIcecastMount: config.icecast_mount || '/live',
            setupIcecastUser: config.icecast_user || 'source',
            setupIcecastPassword: config.icecast_password || '',
        };
        Object.entries(assignments).forEach(([id, value]) => {
            const el = document.getElementById(id);
            if (el && document.activeElement !== el) {
                el.value = value;
            }
        });
        const localOutputToggle = document.getElementById('setupLocalOutputEnabled');
        if (localOutputToggle) localOutputToggle.checked = !!config.local_output_enabled;
        const aiToggle = document.getElementById('setupAiEnabled');
        if (aiToggle) aiToggle.checked = !!config.ai_enabled;
        const startup = document.getElementById('setupStartupState');
        if (startup) {
            const readiness = config.startup_ai_readiness || {};
            const readyTrackIntros = Number(readiness.ready_track_intros || 0);
            const requiredTrackIntros = Number(readiness.required_ready_track_intros || 0);
            startup.textContent = readiness.message
                || `Ready intros: ${readyTrackIntros}/${requiredTrackIntros}`;
        }
    }

    function renderState(state) {
        currentSetupState = state;
        if (!state || state.completed || shouldSuppressSetupWizard(state)) {
            document.getElementById('setupWizardOverlay')?.remove();
            return;
        }

        const root = ensureRoot();
        renderConfig(state);
        const checksByName = byName(state.checks || []);
        const checks = CHECK_ORDER.map(name => checksByName[name]).filter(Boolean);
        const readyCount = checks.filter(check => check.ready).length;
        const progress = root.querySelector('#setupWizardProgress');
        if (progress) {
            progress.textContent = `${readyCount}/${checks.length} readiness checks passed for ${state.station?.name || 'station'}`;
        }

        const list = root.querySelector('#setupWizardChecks');
        if (list) {
            list.innerHTML = checks.map(check => `
                <article class="setup-check ${checkClass(check)}">
                    <span class="material-icons-round">${checkIcon(check)}</span>
                    <div>
                        <h3>${escapeHtml(check.label || check.name)}</h3>
                        <p>${escapeHtml(check.message || '')}</p>
                    </div>
                </article>
            `).join('');
        }

        const completeBtn = root.querySelector('#setupCompleteBtn');
        if (completeBtn) completeBtn.disabled = !state.can_complete;
        for (const checkName of VERIFIABLE) {
            const check = checksByName[checkName] || {};
            const btn = root.querySelector(
                checkName === 'local_output'
                    ? '#setupVerifyLocalBtn'
                    : checkName === 'stream_output'
                        ? '#setupVerifyStreamBtn'
                        : '#setupVerifyAiBtn'
            );
            if (btn) btn.disabled = !!check.ready;
        }
        renderErrors(root, state);
    }

    function shouldSuppressSetupWizard(state) {
        const config = state?.config || {};
        const checks = byName(state?.checks || []);
        const stationConfigured = Boolean(
            state?.station?.id
            && String(config.station_name || state?.station?.name || '').trim()
        );
        const streamConfigured = Boolean(
            config.icecast_enabled
            && String(config.icecast_host || '').trim()
            && Number(config.icecast_port || 0) > 0
            && String(config.icecast_mount || '').trim()
            && String(config.icecast_user || '').trim()
        );
        const startupBufferReady = Boolean(
            checks.startup_ai_buffer?.ready
            || config.startup_ai_readiness?.ready
        );
        const libraryReady = Number(globalThis.currentState?.health?.tracks_in_library || 0) > 0;
        const engineLive = Boolean(
            globalThis.currentState?.health?.engine_running
            || globalThis.currentState?.health?.runtime?.running
            || globalThis.currentState?.health?.runtime?.output_feed_active
        );

        return stationConfigured && streamConfigured && startupBufferReady && (libraryReady || engineLive);
    }

    async function loadDevices() {
        try {
            const payload = await apiFetch('/api/audio/devices');
            deviceOptions = Array.isArray(payload?.devices) ? payload.devices : [];
        } catch (_) {
            deviceOptions = [];
        }
    }

    async function refreshState() {
        await loadDevices();
        const state = await setupFetch(`${API_BASE}/state?station_id=${stationId()}`);
        renderState(state);
        return state;
    }

    function readConfigPayload() {
        return {
            station_id: stationId(),
            station_name: fieldValue('setupStationName') || 'RadioTEDU OnAir',
            local_output_enabled: fieldChecked('setupLocalOutputEnabled'),
            output_device_id: fieldValue('setupOutputDevice'),
            icecast_enabled: true,
            icecast_host: fieldValue('setupIcecastHost') || '127.0.0.1',
            icecast_port: Number(fieldValue('setupIcecastPort') || 8000),
            icecast_mount: fieldValue('setupIcecastMount') || '/live',
            icecast_user: fieldValue('setupIcecastUser') || 'source',
            icecast_password: fieldValue('setupIcecastPassword') || '',
            stream_codec_profile: fieldValue('setupStreamCodec') || 'he_aac_192',
            ai_enabled: fieldChecked('setupAiEnabled'),
            ai_warmth: fieldValue('setupAiWarmth') || 'warm',
        };
    }

    async function saveConfig() {
        const state = await setupFetch(`${API_BASE}/configure`, {
            method: 'POST',
            body: JSON.stringify(readConfigPayload()),
        });
        renderState(state);
    }

    async function runPreflight() {
        const state = await setupFetch(`${API_BASE}/preflight`, {
            method: 'POST',
            body: JSON.stringify({ station_id: stationId() }),
        });
        renderState(state);
    }

    async function repairDependencies() {
        const state = await setupFetch(`${API_BASE}/repair-dependencies`, {
            method: 'POST',
            body: JSON.stringify({ station_id: stationId() }),
        });
        renderState(state);
    }

    async function testAiVoice() {
        const state = await setupFetch(`${API_BASE}/test-ai`, {
            method: 'POST',
            body: JSON.stringify({ station_id: stationId() }),
        });
        renderState(state);
    }

    async function verify(check) {
        const state = await setupFetch(`${API_BASE}/verify`, {
            method: 'POST',
            body: JSON.stringify({ station_id: stationId(), check }),
        });
        renderState(state);
    }

    async function completeSetup() {
        const state = await setupFetch(`${API_BASE}/complete`, {
            method: 'POST',
            body: JSON.stringify({ station_id: stationId() }),
        });
        renderState(state);
        if (state?.completed && typeof showToast === 'function') {
            showToast('Setup complete. RadioTEDU OnAir is restart-safe.');
        }
    }

    function bindActions(root) {
        root.querySelector('#setupSaveBtn')?.addEventListener('click', () => runAction(saveConfig));
        root.querySelector('#setupPreflightBtn')?.addEventListener('click', () => runAction(runPreflight));
        root.querySelector('#setupRepairDepsBtn')?.addEventListener('click', () => runAction(repairDependencies));
        root.querySelector('#setupTestAiBtn')?.addEventListener('click', () => runAction(testAiVoice));
        root.querySelector('#setupVerifyLocalBtn')?.addEventListener('click', () => runAction(() => verify('local_output')));
        root.querySelector('#setupVerifyStreamBtn')?.addEventListener('click', () => runAction(() => verify('stream_output')));
        root.querySelector('#setupVerifyAiBtn')?.addEventListener('click', () => runAction(() => verify('ai_tts')));
        root.querySelector('#setupCompleteBtn')?.addEventListener('click', () => runAction(completeSetup));
    }

    async function runAction(action) {
        const root = ensureRoot();
        setBusy(root, true);
        try {
            await action();
        } catch (_) {
            if (currentSetupState) renderState(currentSetupState);
        } finally {
            setBusy(root, false);
        }
    }

    async function boot() {
        const path = String(window.location?.pathname || '').replace(/\/+$/, '') || '/';
        if (booted || path !== '/app') return;
        booted = true;
        try {
            await refreshState();
        } catch (_) {
            // apiFetch already reports user-facing errors.
        }
    }

    document.addEventListener('radiotedu:app-ready', boot);
    if (document.readyState === 'complete') {
        setTimeout(boot, 0);
    }

    globalThis.RadioTEDUSetupWizard = {
        refreshState,
        runPreflight,
        repairDependencies,
        saveConfig,
        testAiVoice,
        verify,
        completeSetup,
    };
})();
