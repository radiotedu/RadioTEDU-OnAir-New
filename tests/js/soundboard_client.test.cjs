const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sbJsPath = path.resolve(__dirname, '..', '..', 'app', 'static', 'js', 'soundboard.js');
const sbJsSource = fs.readFileSync(sbJsPath, 'utf8');

function createContext(options = {}) {
    const sentRequests = [];
    const elements = {};
    const permissionSet = new Set(options.permissions || []);
    const context = {
        console,
        window: null,
        globalThis: null,
        self: null,
        setInterval() { return 1; },
        clearInterval() {},
        setTimeout(fn) { if (typeof fn === 'function') fn(); return 1; },
        clearTimeout() {},
        FormData: class FormData {
            constructor() {
                this.entries = [];
            }
            append(key, value) {
                this.entries.push([key, value]);
            }
        },
        currentState: {
            currentStationId: 1,
            selectedShowId: null,
            activeShowSession: null,
            programWorkspaceClaimedShowId: null,
            ...(options.currentState || {}),
        },
        hasPermission(permissionKey) {
            return permissionSet.has(String(permissionKey || ''));
        },
        apiFetch: async (url, options = {}) => {
            sentRequests.push({ url, options });
            if (String(url).includes('/api/soundboard') && !String(url).includes('/play') && !String(url).includes('/stop')) {
                return [
                    { id: 1, name: 'Jingle', file_path: '/j.mp3', color: '#ff0000', hotkey: '1', duration_s: 3.0, sort_order: 0 },
                    { id: 2, name: 'Bip', file_path: '/b.mp3', color: '#00ff00', hotkey: null, duration_s: 0.5, sort_order: 1 },
                ];
            }
            return { ok: true };
        },
        document: {
            getElementById(id) {
                if (!elements[id]) {
                    elements[id] = {
                        id,
                        innerHTML: '',
                        textContent: '',
                        style: {},
                        dataset: {},
                        classList: {
                            _set: new Set(),
                            add(c) { this._set.add(c); },
                            remove(c) { this._set.delete(c); },
                            contains(c) { return this._set.has(c); },
                            toggle(c, f) { if (f) this._set.add(c); else this._set.delete(c); },
                        },
                        addEventListener() {},
                        querySelectorAll() { return []; },
                    };
                }
                return elements[id];
            },
            querySelectorAll() { return []; },
            addEventListener() {},
            activeElement: { tagName: 'DIV' },
        },
    };
    context.window = context;
    context.globalThis = context;
    context.self = context;
    vm.runInNewContext(sbJsSource, context, { filename: sbJsPath });
    return { context, elements, sentRequests };
}

test('SoundBoardManager exists with expected methods', () => {
    const { context } = createContext();
    assert.ok(context.SoundBoardManager);
    assert.equal(typeof context.SoundBoardManager.init, 'function');
    assert.equal(typeof context.SoundBoardManager.loadItems, 'function');
    assert.equal(typeof context.SoundBoardManager.uploadAudio, 'function');
    assert.equal(typeof context.SoundBoardManager.saveItem, 'function');
    assert.equal(typeof context.SoundBoardManager.playItem, 'function');
    assert.equal(typeof context.SoundBoardManager.stopItem, 'function');
    assert.equal(typeof context.SoundBoardManager.handleWsEvent, 'function');
    assert.equal(typeof context.SoundBoardManager.render, 'function');
    assert.equal(typeof context.SoundBoardManager.destroy, 'function');
});

test('SoundBoardManager loadItems fetches and stores items', async () => {
    const { context } = createContext();
    await context.SoundBoardManager.loadItems(1);
    assert.equal(context.SoundBoardManager.items.length, 2);
    assert.equal(context.SoundBoardManager.items[0].name, 'Jingle');
});

test('SoundBoardManager playItem sends POST', async () => {
    const { context, sentRequests } = createContext();
    await context.SoundBoardManager.loadItems(1);
    await context.SoundBoardManager.playItem(1);
    const playReq = sentRequests.find(r => String(r.url).includes('/play'));
    assert.ok(playReq);
});

test('SoundBoardManager uploadAudio posts multipart form data to the upload endpoint', async () => {
    const { context, sentRequests } = createContext();

    await context.SoundBoardManager.uploadAudio({
        stationId: 4,
        file: { name: 'sting.mp3' },
    });

    const uploadReq = sentRequests.find(r => String(r.url).includes('/api/soundboard/upload'));
    assert.ok(uploadReq);
    assert.equal(uploadReq.options.method, 'POST');
    assert.ok(uploadReq.options.body);
});

test('SoundBoardManager saveItem updates an existing item via PUT', async () => {
    const { context, sentRequests } = createContext();

    await context.SoundBoardManager.saveItem({
        id: 9,
        name: 'Updated Sting',
        color: '#112233',
    });

    const updateReq = sentRequests.find(r => String(r.url).includes('/api/soundboard/9'));
    assert.ok(updateReq);
    assert.equal(updateReq.options.method, 'PUT');
});

test('SoundBoardManager handleWsEvent tracks playing items', async () => {
    const { context } = createContext();
    await context.SoundBoardManager.loadItems(1);
    context.SoundBoardManager.handleWsEvent({
        type: 'soundboard.played',
        payload: { item_id: 1, name: 'Jingle', duration_s: 3.0 },
    });
    assert.ok(context.SoundBoardManager.playingItems.has(1));
});

test('SoundBoardManager handleWsEvent removes stopped items', async () => {
    const { context } = createContext();
    context.SoundBoardManager.playingItems.add(1);
    context.SoundBoardManager.handleWsEvent({
        type: 'soundboard.stopped',
        payload: { item_id: 1, stopped_all: false },
    });
    assert.ok(!context.SoundBoardManager.playingItems.has(1));
});

test('SoundBoardManager handleWsEvent clears all on stopped_all', async () => {
    const { context } = createContext();
    context.SoundBoardManager.playingItems.add(1);
    context.SoundBoardManager.playingItems.add(2);
    context.SoundBoardManager.handleWsEvent({
        type: 'soundboard.stopped',
        payload: { item_id: null, stopped_all: true },
    });
    assert.equal(context.SoundBoardManager.playingItems.size, 0);
});

test('SoundBoardManager renders a blocked live pad when the workspace is not claimed', async () => {
    const { context, elements } = createContext({
        permissions: ['soundboard.play'],
        currentState: {
            selectedShowId: 9,
            programWorkspaceClaimedShowId: null,
        },
    });

    await context.SoundBoardManager.loadItems(1);

    assert.match(elements.liveSoundboardStatus.textContent, /claim the live workspace/i);
    assert.match(elements.liveSoundboardGrid.innerHTML, /disabled/);
});

test('SoundBoardManager renders an active live pad when the selected show is claimed', async () => {
    const { context, elements } = createContext({
        permissions: ['soundboard.play'],
        currentState: {
            selectedShowId: 9,
            programWorkspaceClaimedShowId: 9,
        },
    });

    await context.SoundBoardManager.loadItems(1);

    assert.equal(elements.liveSoundboardStatus.textContent, '');
    assert.match(elements.liveSoundboardGrid.innerHTML, /live-soundboard-btn/);
    assert.doesNotMatch(elements.liveSoundboardGrid.innerHTML, /disabled/);
});

test('SoundBoardManager destroy clears state', () => {
    const { context } = createContext();
    context.SoundBoardManager.items = [{ id: 1 }];
    context.SoundBoardManager.playingItems.add(1);
    context.SoundBoardManager.destroy();
    assert.equal(context.SoundBoardManager.items.length, 0);
    assert.equal(context.SoundBoardManager.playingItems.size, 0);
});
