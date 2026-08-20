const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const micJsPath = path.resolve(__dirname, '..', '..', 'app', 'static', 'js', 'mic.js');
const micJsSource = fs.readFileSync(micJsPath, 'utf8');

function createClassList() {
    const values = new Set();
    return {
        add(...tokens) {
            tokens.forEach(token => values.add(String(token)));
        },
        remove(...tokens) {
            tokens.forEach(token => values.delete(String(token)));
        },
        contains(token) {
            return values.has(String(token));
        },
        toggle(token, force) {
            const normalized = String(token);
            if (typeof force === 'boolean') {
                if (force) values.add(normalized);
                else values.delete(normalized);
                return force;
            }
            if (values.has(normalized)) {
                values.delete(normalized);
                return false;
            }
            values.add(normalized);
            return true;
        },
    };
}

function createElement(initial = {}) {
    const listeners = new Map();
    return {
        id: initial.id || '',
        dataset: { ...(initial.dataset || {}) },
        value: initial.value || '',
        textContent: initial.textContent || '',
        innerHTML: initial.innerHTML || '',
        style: { ...(initial.style || {}) },
        classList: createClassList(),
        addEventListener(type, handler) {
            listeners.set(String(type), handler);
        },
        dispatch(type, event = {}) {
            const handler = listeners.get(String(type));
            if (handler) handler({ preventDefault() {}, ...event });
        },
    };
}

function createContext() {
    const elements = {
        pttButton: createElement({ id: 'pttButton' }),
        micStatusText: createElement({ id: 'micStatusText' }),
        latencyValue: createElement({ id: 'latencyValue' }),
        micDeviceSelect: createElement({ id: 'micDeviceSelect' }),
        vuMeterFill: createElement({ id: 'vuMeterFill' }),
        micGainSlider: createElement({ id: 'micGainSlider', value: '1' }),
        musicGainSlider: createElement({ id: 'musicGainSlider', value: '1' }),
        duckLevelSlider: createElement({ id: 'duckLevelSlider', value: '0.15' }),
        micGainValue: createElement({ id: 'micGainValue' }),
        musicGainValue: createElement({ id: 'musicGainValue' }),
        duckLevelValue: createElement({ id: 'duckLevelValue' }),
    };
    const modeButtons = [
        createElement({ dataset: { mode: 'push-to-talk' } }),
        createElement({ dataset: { mode: 'always-on' } }),
    ];
    const sentRequests = [];
    const context = {
        console,
        window: null,
        globalThis: null,
        self: null,
        setInterval() {
            return 1;
        },
        clearInterval() {},
        setTimeout(fn) {
            if (typeof fn === 'function') fn();
            return 1;
        },
        clearTimeout() {},
        currentState: { currentStationId: 4, programMusicMode: 'normal' },
        Auth: {
            getUser() {
                return { id: 7, username: 'dj-a', role: 'dj' };
            },
        },
        WS: { send() {} },
        RTCSessionDescription: function RTCSessionDescription(init) { return init; },
        apiFetch: async (url, options = {}) => {
            sentRequests.push({ url, options });
            if (String(url).includes('/api/audio/live/status')) {
                return {
                    station_id: 4,
                    program_music_mode: 'duck',
                    mic_gain: 1.25,
                    music_gain: 0.8,
                    duck_level: 0.2,
                };
            }
            return { ok: true };
        },
        navigator: {
            mediaDevices: {
                enumerateDevices: async () => [
                    { kind: 'audioinput', deviceId: 'mic-1', label: 'Studio Mic' },
                ],
            },
        },
        document: {
            getElementById(id) {
                return elements[id] || null;
            },
            querySelectorAll(selector) {
                if (selector === '.mic-mode-btn') return modeButtons;
                return [];
            },
        },
    };
    context.window = context;
    context.globalThis = context;
    context.self = context;
    vm.runInNewContext(micJsSource, context, { filename: micJsPath });
    return { context, elements, modeButtons, sentRequests };
}

test('MicManager init loads live settings and mode toggle updates CTA state', async () => {
    const { context, elements, modeButtons } = createContext();

    await context.MicManager.init();

    assert.equal(elements.micGainSlider.value, '1.25');
    assert.equal(elements.musicGainSlider.value, '0.8');
    assert.equal(elements.duckLevelSlider.value, '0.2');
    assert.equal(elements.micGainValue.textContent, '125%');
    assert.equal(elements.musicGainValue.textContent, '80%');
    assert.equal(elements.duckLevelValue.textContent, '20%');

    modeButtons[1].dispatch('click');

    assert.equal(context.MicManager.mode, 'always-on');
    assert.equal(modeButtons[1].classList.contains('active'), true);
    assert.equal(modeButtons[0].classList.contains('active'), false);
    assert.equal(elements.pttButton.dataset.micMode, 'always-on');
    assert.equal(elements.pttButton.textContent, 'CLICK TO GO LIVE');
});

test('MicManager applyServerStatus updates live label and latency from buffer size', () => {
    const { context, elements } = createContext();

    context.MicManager.applyServerStatus({
        transmitting: true,
        live_input_enabled: true,
        buffer_bytes: 9600,
    });

    assert.equal(elements.micStatusText.textContent, 'Microphone Live');
    assert.equal(elements.latencyValue.textContent, '100 ms');
});

test('MicManager does not stay live after forbidden mic.start response', () => {
    const { context, elements } = createContext();
    let stopped = 0;
    context.MicManager.transmitting = true;
    context.MicManager.recorder = {
        state: 'recording',
        stop() {
            stopped += 1;
            this.state = 'inactive';
        },
    };

    context.MicManager.handleWsEvent({
        type: 'mic.error',
        payload: { detail: 'forbidden' },
    });

    assert.equal(context.MicManager.transmitting, false);
    assert.equal(stopped > 0, true);
    assert.equal(elements.micStatusText.textContent, 'Microphone Off');
});

test('MicManager drops live state when ownership is lost', () => {
    const { context } = createContext();
    let stopped = 0;
    context.MicManager.transmitting = true;
    context.MicManager.recorder = {
        state: 'recording',
        stop() {
            stopped += 1;
            this.state = 'inactive';
        },
    };

    context.MicManager.applyServerStatus({
        transmitting: true,
        live_input_enabled: true,
        active_user: { id: 99, username: 'other-dj', role: 'dj' },
    });

    assert.equal(context.MicManager.transmitting, false);
    assert.equal(stopped > 0, true);
});

test('MicManager has webrtc transport fields', async () => {
    const { context } = createContext();
    await context.MicManager.init();
    assert.equal(context.MicManager.rtcPeerConnection, null);
    assert.equal(context.MicManager.transport, 'none');
});

test('MicManager startWsTransmit is callable for fallback', async () => {
    const { context } = createContext();
    await context.MicManager.init();
    assert.equal(typeof context.MicManager.startWsTransmit, 'function');
});

test('MicManager stopTransmit cleans up rtcPeerConnection when set', () => {
    const { context } = createContext();
    let closed = false;
    context.MicManager.rtcPeerConnection = {
        close() { closed = true; },
        connectionState: 'connected',
    };
    context.MicManager.transport = 'webrtc';
    context.MicManager.transmitting = true;
    context.MicManager.stopTransmit();
    assert.equal(closed, true);
    assert.equal(context.MicManager.rtcPeerConnection, null);
    assert.equal(context.MicManager.transport, 'none');
});

test('MicManager handleWsEvent handles webrtc.answer', () => {
    const { context } = createContext();
    let descSet = false;
    context.MicManager._pendingRtcResolve = () => { descSet = true; };
    context.MicManager.rtcPeerConnection = {
        setRemoteDescription() { return Promise.resolve(); },
    };
    context.MicManager.handleWsEvent({
        type: 'webrtc.answer',
        payload: { sdp: 'v=0\r\n' },
    });
    // setRemoteDescription is async; give the microtask queue a tick to flush
    return new Promise((resolve) => setTimeout(resolve, 0)).then(() => {
        assert.equal(descSet, true);
    });
});

test('MicManager handleWsEvent handles webrtc.error fallback', () => {
    const { context } = createContext();
    let fallbackCalled = false;
    context.MicManager._pendingRtcReject = () => { fallbackCalled = true; };
    context.MicManager.handleWsEvent({
        type: 'webrtc.error',
        payload: { detail: 'webrtc_disabled' },
    });
    assert.equal(fallbackCalled, true);
});
