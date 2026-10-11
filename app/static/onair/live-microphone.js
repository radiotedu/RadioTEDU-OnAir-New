(function (scope) {
  'use strict';
  class LiveMicrophone {
    constructor(options) {
      this.options = options;
      this.phase = 'off';
      this.generation = 0;
      this.stationId = null;
      this.stream = null;
      this.context = null;
      this.node = null;
      this.queue = [];
      this.draining = false;
      this.inflight = null;
      this.renderStarted = false;
      this.original = null;
      this.stopPromise = null;
      this.startPromise = null;
      this.preparePromise = null;
      this.deviceId = '';
      this.captureEnabled = false;
      this.confirmChunk = null;
      this.settings = { program_music_mode: 'normal', mic_gain: 1, music_gain: 1, duck_level: 0.15 };
    }
    async devices() {
      const list = await navigator.mediaDevices.enumerateDevices();
      const inputs = list.filter((d) => d.kind === 'audioinput');
      this.options.devices?.(inputs, this.deviceId);
      if (this.stream && !inputs.some((d) => d.deviceId === this.deviceId)) {
        // The selected physical input disappearing must never silently select
        // a different microphone and put it on air.
        await this.stop('Selected microphone was disconnected');
        this.release();
      }
      return inputs;
    }
    changed(message = '') { this.options.status?.(this.phase, message, this); }
    prepare() {
      if (this.preparePromise) return this.preparePromise;
      this.preparePromise = this.prepareTransaction().finally(() => { this.preparePromise = null; });
      return this.preparePromise;
    }
    async prepareTransaction() {
      if (this.stream && this.stream.getAudioTracks().every((t) => t.readyState !== 'ended')) return;
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Open this panel on localhost or HTTPS to allow microphone access');
      const generation = this.generation;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: {
        deviceId: this.deviceId ? { exact: this.deviceId } : undefined,
        channelCount: 1, sampleRate: 48000,
        echoCancellation: false, noiseSuppression: false, autoGainControl: false,
      }});
      if (generation !== this.generation) {
        stream.getTracks().forEach((t) => t.stop());
        throw new Error('Microphone preparation cancelled');
      }
      this.stream = stream;
      this.deviceId = stream.getAudioTracks()[0]?.getSettings?.().deviceId || this.deviceId;
      try {
        const Audio = scope.AudioContext || scope.webkitAudioContext;
        this.context = new Audio({ sampleRate: 48000, latencyHint: 'interactive' });
        if (this.context.sampleRate !== 48000 || !this.context.audioWorklet) throw new Error('This browser requires a 48 kHz AudioWorklet. Use a current Chrome, Edge, Firefox or Safari');
        await this.context.audioWorklet.addModule('/static/onair/microphone-worklet.js?v=1');
        if (generation !== this.generation) throw new Error('Microphone preparation cancelled');
        this.source = this.context.createMediaStreamSource(stream);
        this.node = new scope.AudioWorkletNode(this.context, 'radiotedu-microphone', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
        this.silent = this.context.createGain();
        this.silent.gain.value = 0;
        this.source.connect(this.node);
        this.node.connect(this.silent).connect(this.context.destination);
        this.node.port.onmessage = (event) => this.enqueue(event.data);
        stream.getTracks().forEach((track) => track.addEventListener('ended', () => {
          this.stop('Microphone was disconnected').finally(() => this.release());
        }));
        await this.devices();
        this.changed('Microphone ready · 48 kHz PCM · headphones recommended');
      } catch (error) { this.release(); throw error; }
    }
    async selectDevice(id) {
      if (this.phase !== 'off') throw new Error('Turn the microphone off before selecting another input');
      this.generation += 1;
      this.release();
      this.deviceId = String(id || '');
      this.changed();
    }
    async loadSettings() {
      const sid = Number(this.options.station());
      const saved = await this.options.api(`/api/audio/live/status?station_id=${sid}`);
      if (this.phase === 'off' && sid === Number(this.options.station())) {
        this.settings = { program_music_mode: saved.program_music_mode || 'normal', mic_gain: Number(saved.mic_gain ?? 1), music_gain: Number(saved.music_gain ?? 1), duck_level: Number(saved.duck_level ?? 0.15) };
        this.options.settings?.(this.settings);
      }
      return saved;
    }
    async saveSettings(settings = this.settings) {
      const sid = this.stationId || Number(this.options.station());
      await this.options.api('/api/audio/live/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...settings, station_id: sid }) });
      const saved = await this.options.api(`/api/audio/live/status?station_id=${sid}`);
      for (const key of ['program_music_mode', 'mic_gain', 'music_gain', 'duck_level']) {
        if (String(saved[key]) !== String(settings[key])) throw new Error('Live mix settings did not persist');
      }
      return saved;
    }
    start() {
      if (this.phase !== 'off' || this.startPromise || this.stopPromise) return Promise.resolve();
      this.startPromise = this.startTransaction().finally(() => { this.startPromise = null; });
      return this.startPromise;
    }
    async startTransaction() {
      if (this.phase !== 'off' || this.stopPromise) return;
      this.options.canStart?.();
      const generation = ++this.generation;
      const sid = Number(this.options.station());
      if (!sid) throw new Error('Select a station first');
      this.stationId = sid;
      this.phase = 'starting';
      this.changed('Preparing microphone');
      const assertCurrent = () => {
        if (generation !== this.generation || sid !== Number(this.options.station())) throw new Error('Microphone start cancelled');
      };
      try {
        await this.prepare(); assertCurrent();
        const runtime = await this.options.api(`/api/runtime/${sid}/status`); assertCurrent();
        if (!runtime.output_feed_active || !runtime.program_running) throw new Error('Start the station broadcast before going live');
        if (runtime.icecast_mount_health?.source_response_confirmed === false) throw new Error('Station source has not been accepted by the broadcast server');
        if (runtime.live_mic_active) throw new Error('Another live source already owns this station');
        const saved = await this.options.api(`/api/audio/live/status?station_id=${sid}`); assertCurrent();
        this.original = { station_id: sid, program_music_mode: saved.program_music_mode || 'normal', mic_gain: Number(saved.mic_gain ?? 1), music_gain: Number(saved.music_gain ?? 1), duck_level: Number(saved.duck_level ?? 0.15) };
        await this.options.studio(sid); assertCurrent();
        await this.saveSettings(); assertCurrent();
        await this.options.api('/api/audio/live/render/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: sid, source_name: 'Studio microphone · 48 kHz PCM', input_format: 's16le', sample_rate: 48000, channels: 1, max_buffer_bytes: 38400 }) });
        this.renderStarted = true;
        assertCurrent();
        await this.context.resume(); assertCurrent();
        let timer;
        const received = new Promise((resolve, reject) => {
          this.confirmChunk = { resolve, reject };
          timer = setTimeout(() => reject(new Error('Microphone PCM was not received; normal music restored')), 2500);
        });
        this.captureEnabled = true;
        this.node.port.postMessage({ enabled: true });
        this.changed('Checking microphone PCM delivery');
        try { await received; } finally { clearTimeout(timer); this.confirmChunk = null; }
        assertCurrent();
        const confirmed = await this.options.api(`/api/runtime/${sid}/status`); assertCurrent();
        if (!confirmed.output_feed_active || !confirmed.program_running) throw new Error('Station output stopped while starting the microphone');
        if (confirmed.icecast_mount_health?.source_response_confirmed === false) throw new Error('Station source acceptance was lost while starting the microphone');
        this.phase = 'live';
        this.changed('Microphone live · music continues in the background');
      } catch (error) {
        // Only the start transaction owns these compensating operations. A
        // cancelled permission/HTTP response cannot resurrect a stopped mic.
        try { await this.finishStop(); this.phase = 'off'; }
        catch (stopError) { this.phase = 'error'; this.changed(stopError.message); throw stopError; }
        this.stationId = null;
        this.changed(error.message);
        throw error;
      }
    }
    enqueue(chunk) {
      if (!this.captureEnabled || !['starting', 'live'].includes(this.phase)) return;
      if (this.queue.length >= 4) {
        this.stop('Microphone network is too slow; normal music restored').catch((e) => this.changed(e.message));
        return;
      }
      this.queue.push(chunk);
      this.drain();
    }
    async drain() {
      if (this.draining) return;
      this.draining = true;
      try {
        while (this.captureEnabled && ['starting', 'live'].includes(this.phase) && this.queue.length) {
          const chunk = this.queue.shift();
          this.inflight = this.options.api(`/api/audio/live/render/chunk?station_id=${this.stationId}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: chunk, timeoutMs: 1500 });
          const accepted = await this.inflight;
          this.inflight = null;
          this.options.levels?.(accepted);
          if (accepted?.receiving) this.confirmChunk?.resolve();
        }
      } catch (error) {
        this.inflight = null;
        this.confirmChunk?.reject(error);
        this.stop('Microphone transport failed: '+error.message).catch((e) => this.changed(e.message));
      } finally { this.draining = false; }
    }
    async finishStop() {
      this.captureEnabled = false;
      this.confirmChunk?.reject(new Error('Microphone start cancelled'));
      this.node?.port.postMessage({ enabled: false });
      this.queue = [];
      if (this.inflight) await this.inflight.catch(() => {});
      if (this.renderStarted) {
        await this.options.api('/api/audio/live/render/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: this.stationId }), keepalive: true });
        this.renderStarted = false;
      }
      if (this.original) {
        await this.options.api('/api/audio/live/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.original), keepalive: true });
        const restored = await this.options.api(`/api/audio/live/status?station_id=${this.stationId}`);
        for (const key of ['program_music_mode', 'mic_gain', 'music_gain', 'duck_level']) {
          if (String(restored[key]) !== String(this.original[key])) throw new Error('Original live mix restoration was not verified');
        }
        this.original = null;
      }
    }
    async stop(reason = 'Microphone off · normal music restored') {
      this.generation += 1;
      if (this.stopPromise) return this.stopPromise;
      const wasStarting = this.phase === 'starting';
      this.captureEnabled = false;
      this.confirmChunk?.reject(new Error('Microphone start cancelled'));
      this.phase = 'stopping';
      this.changed('Returning to normal music');
      // An in-progress start will observe the generation and undo any accepted
      // server transaction itself. Stop must not race its settings restore.
      const cleanup = wasStarting ? this.startPromise.catch(() => {}) : this.finishStop();
      this.stopPromise = cleanup.then(() => {
        if (this.renderStarted || this.original) throw new Error('Microphone restoration did not complete');
        this.phase = 'off'; this.stationId = null; this.changed(reason);
      }).catch((error) => {
        this.phase = 'error'; this.changed('Stop needs attention: '+error.message); throw error;
      }).finally(() => { this.stopPromise = null; });
      return this.stopPromise;
    }
    release() {
      if (this.node) this.node.port.onmessage = null;
      for (const node of [this.source, this.node, this.silent]) { try { node?.disconnect(); } catch (_) {} }
      this.stream?.getTracks().forEach((t) => t.stop());
      this.context?.close().catch(() => {});
      this.stream = this.context = this.source = this.node = this.silent = null;
    }
  }
  scope.RadioLiveMicrophone = LiveMicrophone;
  if (typeof module !== 'undefined' && module.exports) module.exports = LiveMicrophone;
})(typeof window !== 'undefined' ? window : globalThis);
