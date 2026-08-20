(function () {
    const MicManager = {
        stream: null,
        recorder: null,
        analyser: null,
        audioContext: null,
        levelTimer: 0,
        saveTimer: 0,
        mode: 'push-to-talk',
        transmitting: false,
        serverStatus: null,
        rtcPeerConnection: null,
        transport: 'none',
        _pendingRtcResolve: null,
        _pendingRtcReject: null,
        settings: {
            program_music_mode: 'normal',
            mic_gain: 1.0,
            music_gain: 1.0,
            duck_level: 0.15,
        },
        async init() {
            this.bindUi();
            this.setMode(this.mode);
            await this.loadServerSettings();
            this.applyServerStatus(null);
        },
        bindUi() {
            const pttButton = document.getElementById('pttButton');
            if (pttButton && !pttButton.dataset.boundMic) {
                pttButton.dataset.boundMic = '1';
                pttButton.addEventListener('mousedown', () => {
                    if (this.mode === 'push-to-talk') this.startTransmit();
                });
                pttButton.addEventListener('mouseup', () => {
                    if (this.mode === 'push-to-talk') this.stopTransmit();
                });
                pttButton.addEventListener('mouseleave', () => {
                    if (this.mode === 'push-to-talk') this.stopTransmit();
                });
                pttButton.addEventListener('touchstart', (event) => {
                    event.preventDefault();
                    if (this.mode === 'push-to-talk') this.startTransmit();
                }, { passive: false });
                pttButton.addEventListener('touchend', (event) => {
                    event.preventDefault();
                    if (this.mode === 'push-to-talk') this.stopTransmit();
                }, { passive: false });
                pttButton.addEventListener('click', () => {
                    if (this.mode !== 'always-on') return;
                    if (this.transmitting) this.stopTransmit();
                    else this.startTransmit();
                });
            }
            document.querySelectorAll('.mic-mode-btn').forEach((button) => {
                if (button.dataset.boundMic === '1') return;
                button.dataset.boundMic = '1';
                button.addEventListener('click', () => {
                    this.setMode(button.dataset.mode || 'push-to-talk');
                });
            });
            this.bindSettingsSlider('micGainSlider', 'micGainValue', 'mic_gain', (value) => `${Math.round(value * 100)}%`);
            this.bindSettingsSlider('musicGainSlider', 'musicGainValue', 'music_gain', (value) => `${Math.round(value * 100)}%`);
            this.bindSettingsSlider('duckLevelSlider', 'duckLevelValue', 'duck_level', (value) => `${Math.round(value * 100)}%`);
        },
        bindSettingsSlider(sliderId, valueId, settingKey, formatter) {
            const slider = document.getElementById(sliderId);
            if (!slider || slider.dataset.boundMic === '1') return;
            slider.dataset.boundMic = '1';
            const applyValue = () => {
                const numeric = this.normalizeSettingValue(slider.value, settingKey);
                this.settings[settingKey] = numeric;
                const valueEl = document.getElementById(valueId);
                if (valueEl) valueEl.textContent = formatter(numeric);
                this.queueSaveSettings();
            };
            slider.addEventListener('input', applyValue);
            slider.addEventListener('change', applyValue);
        },
        normalizeSettingValue(raw, settingKey) {
            const numeric = Number(raw);
            if (!Number.isFinite(numeric)) {
                return this.settings[settingKey] ?? 0;
            }
            if (settingKey === 'duck_level') {
                return Math.max(0, Math.min(1, numeric));
            }
            return Math.max(0, Math.min(2, numeric));
        },
        setMode(mode) {
            const nextMode = String(mode || '').trim().toLowerCase() === 'always-on'
                ? 'always-on'
                : 'push-to-talk';
            if (this.mode === 'always-on' && nextMode === 'push-to-talk' && this.transmitting) {
                this.stopTransmit();
            }
            this.mode = nextMode;
            document.querySelectorAll('.mic-mode-btn').forEach((button) => {
                const targetMode = String(button.dataset.mode || '').trim().toLowerCase();
                button.classList.toggle('active', targetMode === this.mode);
            });
            this.updatePttUi();
        },
        updatePttUi() {
            const pttButton = document.getElementById('pttButton');
            const label = document.getElementById('pttButtonLabel');
            let text = 'HOLD TO TALK';
            if (this.mode === 'always-on') {
                text = this.transmitting ? 'CLICK TO STOP' : 'CLICK TO GO LIVE';
            } else if (this.transmitting) {
                text = 'RELEASE TO STOP';
            }
            if (pttButton) {
                pttButton.dataset.micMode = this.mode;
                pttButton.classList.toggle('transmitting', !!this.transmitting);
            }
            if (label) label.textContent = text;
            else if (pttButton) pttButton.textContent = text;
        },
        applySettingsToUi() {
            const mapping = [
                ['micGainSlider', 'micGainValue', this.settings.mic_gain, (value) => `${Math.round(value * 100)}%`],
                ['musicGainSlider', 'musicGainValue', this.settings.music_gain, (value) => `${Math.round(value * 100)}%`],
                ['duckLevelSlider', 'duckLevelValue', this.settings.duck_level, (value) => `${Math.round(value * 100)}%`],
            ];
            mapping.forEach(([sliderId, valueId, rawValue, formatter]) => {
                const slider = document.getElementById(sliderId);
                if (slider) slider.value = String(rawValue);
                const valueEl = document.getElementById(valueId);
                if (valueEl) valueEl.textContent = formatter(rawValue);
            });
        },
        async loadServerSettings() {
            if (typeof apiFetch !== 'function') {
                this.applySettingsToUi();
                return null;
            }
            try {
                const payload = await apiFetch(`/api/audio/live/status?station_id=${Number(currentState.currentStationId || 1)}`);
                this.settings = {
                    ...this.settings,
                    program_music_mode: String(payload?.program_music_mode || this.settings.program_music_mode),
                    mic_gain: this.normalizeSettingValue(payload?.mic_gain, 'mic_gain'),
                    music_gain: this.normalizeSettingValue(payload?.music_gain, 'music_gain'),
                    duck_level: this.normalizeSettingValue(payload?.duck_level, 'duck_level'),
                };
                if (typeof currentState === 'object' && currentState) {
                    currentState.programMusicMode = this.settings.program_music_mode;
                }
                if (typeof syncProgramMusicModeUi === 'function') {
                    syncProgramMusicModeUi();
                }
            } catch (_) {
                // Keep defaults on bootstrap failure.
            }
            this.applySettingsToUi();
            return { ...this.settings };
        },
        queueSaveSettings() {
            if (this.saveTimer) clearTimeout(this.saveTimer);
            this.saveTimer = window.setTimeout(() => {
                this.saveTimer = 0;
                this.saveSettings().catch(() => { });
            }, 250);
        },
        async saveSettings() {
            if (typeof apiFetch !== 'function') return null;
            const payload = {
                station_id: Number(currentState.currentStationId || 1),
                program_music_mode: String(currentState.programMusicMode || this.settings.program_music_mode || 'normal'),
                mic_gain: this.settings.mic_gain,
                music_gain: this.settings.music_gain,
                duck_level: this.settings.duck_level,
            };
            const response = await apiFetch('/api/audio/live/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            this.settings = {
                ...this.settings,
                ...response,
            };
            this.applySettingsToUi();
            return response;
        },
        async ensureMedia() {
            if (this.stream) return this.stream;
            if (!navigator.mediaDevices?.getUserMedia) {
                throw new Error('Microphone capture is not supported in this browser.');
            }
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    sampleRate: 48000,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const source = this.audioContext.createMediaStreamSource(this.stream);
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 2048;
            source.connect(this.analyser);
            this.startVuLoop();
            await this.populateDevices();
            return this.stream;
        },
        startVuLoop() {
            if (!this.analyser || this.levelTimer) return;
            const buffer = new Uint8Array(this.analyser.frequencyBinCount);
            this.levelTimer = window.setInterval(() => {
                if (!this.analyser) return;
                this.analyser.getByteTimeDomainData(buffer);
                let peak = 0;
                for (const sample of buffer) {
                    const normalized = Math.abs((sample - 128) / 128);
                    if (normalized > peak) peak = normalized;
                }
                const percent = Math.max(0, Math.min(100, Math.round(peak * 100)));
                const vuFill = document.getElementById('vuMeterFill');
                if (vuFill) vuFill.style.width = `${percent}%`;
            }, 50);
        },
        async populateDevices() {
            const select = document.getElementById('micDeviceSelect');
            if (!select || !navigator.mediaDevices?.enumerateDevices) return;
            const devices = await navigator.mediaDevices.enumerateDevices();
            const options = devices.filter(device => device.kind === 'audioinput');
            select.innerHTML = options.map(device => {
                const label = device.label || 'Microphone';
                return `<option value="${String(device.deviceId || '')}">${label}</option>`;
            }).join('');
        },
        async startWsTransmit() {
            if (this.recorder && this.recorder.state === 'recording') return;
            if (typeof MediaRecorder === 'undefined') {
                throw new Error('MediaRecorder is not available.');
            }
            this.recorder = new MediaRecorder(this.stream, { mimeType: 'audio/webm;codecs=opus' });
            this.recorder.addEventListener('dataavailable', async (event) => {
                if (!event.data || event.data.size <= 0) return;
                const chunk = new Uint8Array(await event.data.arrayBuffer());
                WS.send(chunk);
            });
            this.recorder.start(100);
            WS.send({ type: 'mic.start', station_id: Number(currentState.currentStationId || 1) });
            this.transport = 'websocket';
        },
        async startWebRtcTransmit() {
            if (typeof apiFetch !== 'function') throw new Error('apiFetch unavailable');
            const config = await apiFetch('/api/webrtc/ice-config');
            if (!config || !config.enabled) throw new Error('webrtc_disabled');

            const pc = new RTCPeerConnection({ iceServers: config.ice_servers });
            this.rtcPeerConnection = pc;

            this.stream.getAudioTracks().forEach(track => pc.addTrack(track, this.stream));

            pc.onicecandidate = (event) => {
                if (event.candidate) {
                    WS.send({ type: 'webrtc.ice', candidate: event.candidate.toJSON() });
                }
            };

            pc.onconnectionstatechange = () => {
                if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
                    this.stopTransmit({ sendCommand: false });
                }
            };

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            WS.send({ type: 'webrtc.offer', sdp: offer.sdp });

            let timeoutId;
            await new Promise((resolve, reject) => {
                this._pendingRtcResolve = resolve;
                this._pendingRtcReject = reject;
                timeoutId = setTimeout(() => reject(new Error('webrtc_timeout')), 10000);
            }).finally(() => clearTimeout(timeoutId));

            this.transport = 'webrtc';
        },
        async startTransmit() {
            if (this.transmitting) return;
            await this.ensureMedia();
            if (typeof RTCPeerConnection !== 'undefined') {
                try {
                    await this.startWebRtcTransmit();
                    this.transmitting = true;
                    this.updatePttUi();
                    return;
                } catch (e) {
                    if (typeof console !== 'undefined') console.warn('WebRTC failed, falling back to WS:', e);
                    this.rtcPeerConnection = null;
                }
            }
            await this.startWsTransmit();
            this.transmitting = true;
            this.updatePttUi();
        },
        stopTransmit(options = {}) {
            const sendCommand = options.sendCommand !== false;
            if (this.rtcPeerConnection) {
                try { this.rtcPeerConnection.close(); } catch (_) {}
                this.rtcPeerConnection = null;
                if (sendCommand) {
                    WS.send({ type: 'webrtc.close', station_id: Number(currentState.currentStationId || 1) });
                }
            }
            if (this.recorder && this.recorder.state !== 'inactive') {
                this.recorder.stop();
            }
            if (sendCommand && this.transport === 'websocket') {
                WS.send({ type: 'mic.stop', station_id: Number(currentState.currentStationId || 1) });
            }
            this.transport = 'none';
            if (this._pendingRtcReject) {
                this._pendingRtcReject(new Error('aborted'));
            }
            this._pendingRtcResolve = null;
            this._pendingRtcReject = null;
            this.transmitting = false;
            this.updatePttUi();
        },
        applyServerStatus(payload) {
            const currentUser = globalThis.Auth && typeof globalThis.Auth.getUser === 'function'
                ? globalThis.Auth.getUser()
                : null;
            const currentUserId = Number(currentUser?.id || 0);
            const activeUserId = Number(payload?.active_user?.id || 0);
            const ownershipLost = activeUserId > 0 && currentUserId > 0 && activeUserId !== currentUserId;
            if (ownershipLost || (!payload?.transmitting && this.transmitting)) {
                this.stopTransmit({ sendCommand: false });
            }
            this.serverStatus = payload || null;
            this.transmitting = Boolean(payload?.transmitting) && !ownershipLost;
            const statusText = document.getElementById('micStatusText');
            if (statusText) {
                if (this.transmitting) statusText.textContent = 'Microphone Live';
                else if (payload?.live_input_enabled) statusText.textContent = 'Microphone Ready';
                else statusText.textContent = 'Microphone Off';
            }
            const latencyValue = document.getElementById('latencyValue');
            if (latencyValue) {
                const bufferBytes = Number(payload?.buffer_bytes ?? 0);
                const latencyMs = bufferBytes > 0 ? Math.round(bufferBytes / 96) : null;
                latencyValue.textContent = latencyMs !== null ? `${latencyMs} ms` : '-- ms';
            }
            this.updatePttUi();
        },
        handleWsEvent(event) {
            const eventType = String(event?.type || '').trim().toLowerCase();
            if (eventType === 'mic.status') {
                this.applyServerStatus(event?.payload || null);
            }
            if (eventType === 'mic.error') {
                if (this.transmitting || (this.recorder && this.recorder.state !== 'inactive')) {
                    this.stopTransmit({ sendCommand: false });
                }
                const statusText = document.getElementById('micStatusText');
                if (statusText) {
                    statusText.textContent = 'Microphone Off';
                }
                const errorDetail = String(event?.payload?.detail || 'mic_error');
                this.serverStatus = {
                    ...(this.serverStatus || {}),
                    last_error: errorDetail,
                };
                if (!this._lastMicErrorShown || Date.now() - this._lastMicErrorShown > 5000) {
                    this._lastMicErrorShown = Date.now();
                    const messages = {
                        'no_on_air_studio': 'No studio is on-air for this station',
                        'not_studio_owner': 'Join the on-air studio first',
                        'no_dj_session': 'Join the on-air studio as DJ first',
                        'forbidden': 'Join the on-air studio first',
                    };
                    const message = messages[errorDetail] || `Mic error: ${errorDetail}`;
                    if (typeof showToast === 'function') showToast(message, 'error');
                }
            }
            if (eventType === 'mic.level') {
                const vuFill = document.getElementById('vuMeterFill');
                const peak = Number(event?.payload?.peak_db ?? -60);
                const percent = Math.max(0, Math.min(100, Math.round(((peak + 60) / 60) * 100)));
                if (vuFill) vuFill.style.width = `${percent}%`;
            }
            if (eventType === 'webrtc.answer') {
                const sdp = event?.payload?.sdp;
                if (sdp && this.rtcPeerConnection) {
                    this.rtcPeerConnection.setRemoteDescription(
                        new RTCSessionDescription({ type: 'answer', sdp: sdp })
                    ).then(() => {
                        if (this._pendingRtcResolve) this._pendingRtcResolve();
                        this._pendingRtcResolve = null;
                        this._pendingRtcReject = null;
                    }).catch(() => {
                        if (this._pendingRtcReject) this._pendingRtcReject(new Error('answer_failed'));
                        this._pendingRtcReject = null;
                        this._pendingRtcResolve = null;
                    });
                }
            }
            if (eventType === 'webrtc.ice') {
                const candidate = event?.payload?.candidate;
                if (candidate && this.rtcPeerConnection) {
                    this.rtcPeerConnection.addIceCandidate(candidate).catch(() => {});
                }
            }
            if (eventType === 'webrtc.error') {
                if (this._pendingRtcReject) {
                    this._pendingRtcReject(new Error(event?.payload?.detail || 'webrtc_error'));
                }
                this._pendingRtcReject = null;
                this._pendingRtcResolve = null;
            }
        },
        destroy() {
            if (this.levelTimer) {
                clearInterval(this.levelTimer);
                this.levelTimer = 0;
            }
            if (this.saveTimer) {
                clearTimeout(this.saveTimer);
                this.saveTimer = 0;
            }
            if (this.rtcPeerConnection) {
                try { this.rtcPeerConnection.close(); } catch (_) {}
                this.rtcPeerConnection = null;
            }
            this.transport = 'none';
            if (this.recorder && this.recorder.state !== 'inactive') {
                this.recorder.stop();
            }
            if (this.stream) {
                this.stream.getTracks().forEach(track => track.stop());
            }
            this.recorder = null;
            this.stream = null;
            this.analyser = null;
            if (this.audioContext) {
                this.audioContext.close().catch(() => {});
            }
            this.audioContext = null;
            this.transmitting = false;
        },
    };

    if (typeof globalThis !== 'undefined') {
        globalThis.MicManager = MicManager;
    }
})();
