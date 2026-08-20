(function () {
    const SoundBoardManager = {
        items: [],
        playingItems: new Set(),
        _hotkeyListener: null,
        draftItemId: null,

        async init() {
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            await this.loadItems(stationId);
            this.bindUi();
            this.render();
        },

        async loadItems(stationId) {
            if (typeof apiFetch !== 'function') return;
            try {
                const items = await apiFetch(`/api/soundboard/?station_id=${Number(stationId)}`);
                this.items = Array.isArray(items) ? items : [];
                this.render();
            } catch (_) {
                this.items = [];
            }
        },

        async uploadAudio({ stationId, file }) {
            if (typeof apiFetch !== 'function' || !file) return null;
            const formData = new FormData();
            formData.append('station_id', String(Number(stationId) || 1));
            formData.append('file', file);
            return apiFetch('/api/soundboard/upload', {
                method: 'POST',
                body: formData,
            });
        },

        async saveItem(payload = {}) {
            if (typeof apiFetch !== 'function') return null;
            const itemId = Number(payload.id || 0);
            if (itemId > 0) {
                return apiFetch(`/api/soundboard/${itemId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }
            return apiFetch('/api/soundboard/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        },

        bindUi() {
            const grid = typeof document !== 'undefined' ? document.getElementById('soundboardGrid') : null;
            const liveGrid = typeof document !== 'undefined' ? document.getElementById('liveSoundboardGrid') : null;
            const uploadBtn = typeof document !== 'undefined' ? document.getElementById('soundboardUploadBtn') : null;
            const saveBtn = typeof document !== 'undefined' ? document.getElementById('soundboardSaveBtn') : null;
            const resetBtn = typeof document !== 'undefined' ? document.getElementById('soundboardResetBtn') : null;
            if (grid) {
                grid.addEventListener('click', (e) => {
                    const editBtn = e.target.closest && e.target.closest('.soundboard-item-action[data-action="edit"]');
                    if (editBtn) {
                        const itemId = Number(editBtn.dataset.itemId);
                        this.populateForm(itemId);
                        return;
                    }
                    const deleteBtn = e.target.closest && e.target.closest('.soundboard-item-action[data-action="delete"]');
                    if (deleteBtn) {
                        const itemId = Number(deleteBtn.dataset.itemId);
                        void this.deleteItem(itemId);
                        return;
                    }
                    const btn = e.target.closest && e.target.closest('.soundboard-btn');
                    if (!btn) return;
                    const itemId = Number(btn.dataset.itemId);
                    if (this.playingItems.has(itemId)) {
                        this.stopItem(itemId);
                    } else {
                        this.playItem(itemId);
                    }
                });
            }
            if (liveGrid) {
                liveGrid.addEventListener('click', (e) => {
                    const btn = e.target.closest && e.target.closest('.live-soundboard-btn');
                    if (!btn || this.isLivePadBlocked()) return;
                    const itemId = Number(btn.dataset.itemId);
                    if (this.playingItems.has(itemId)) {
                        this.stopItem(itemId);
                    } else {
                        this.playItem(itemId);
                    }
                });
            }
            if (uploadBtn) {
                uploadBtn.addEventListener('click', () => {
                    void this.handleUploadClick();
                });
            }
            if (saveBtn) {
                saveBtn.addEventListener('click', () => {
                    void this.handleSaveClick();
                });
            }
            if (resetBtn) {
                resetBtn.addEventListener('click', () => {
                    this.resetForm();
                });
            }
        },

        getManagePermission() {
            if (typeof hasPermission === 'function') {
                return !!hasPermission('soundboard.manage');
            }
            return false;
        },

        getPlayPermission() {
            if (typeof hasPermission === 'function') {
                return !!(hasPermission('soundboard.play') || hasPermission('soundboard.manage'));
            }
            return false;
        },

        getManageElements() {
            if (typeof document === 'undefined') return {};
            return {
                panel: document.getElementById('soundboardManagePanel'),
                name: document.getElementById('soundboardNameInput'),
                color: document.getElementById('soundboardColorInput'),
                hotkey: document.getElementById('soundboardHotkeyInput'),
                category: document.getElementById('soundboardCategoryInput'),
                file: document.getElementById('soundboardFileInput'),
                status: document.getElementById('soundboardManageStatus'),
            };
        },

        getLiveElements() {
            if (typeof document === 'undefined') return {};
            return {
                panel: document.getElementById('liveSoundboardCard'),
                grid: document.getElementById('liveSoundboardGrid'),
                status: document.getElementById('liveSoundboardStatus'),
            };
        },

        getLivePadState() {
            const canPlay = this.getPlayPermission();
            const selectedShowId = Number(
                (typeof currentState !== 'undefined' && currentState.selectedShowId)
                || (typeof currentState !== 'undefined' && currentState.activeShowSession?.show_id)
                || 0
            );
            const activeShowId = Number(
                (typeof currentState !== 'undefined' && currentState.activeShowSession?.show_id) || 0
            );
            const claimedShowId = Number(
                (typeof currentState !== 'undefined' && currentState.programWorkspaceClaimedShowId) || 0
            );
            const hasClaim = Boolean(
                selectedShowId > 0 && (claimedShowId === selectedShowId || activeShowId === selectedShowId)
            );
            const blockedReason = !selectedShowId
                ? 'Select and claim a show to use the live sound pad.'
                : 'Claim the live workspace to use the sound pad.';
            return {
                canPlay,
                selectedShowId,
                hasClaim,
                blockedReason,
            };
        },

        isLivePadBlocked() {
            const state = this.getLivePadState();
            return !state.canPlay || !state.hasClaim;
        },

        setStatus(message) {
            const { status } = this.getManageElements();
            if (status) status.textContent = String(message || 'Ready');
        },

        resetForm() {
            this.draftItemId = null;
            const { name, color, hotkey, category, file } = this.getManageElements();
            if (name) name.value = '';
            if (color) color.value = '#4a90d9';
            if (hotkey) hotkey.value = '';
            if (category) category.value = '';
            if (file) file.value = '';
            this.setStatus('Ready');
        },

        populateForm(itemId) {
            const item = this.items.find(entry => Number(entry.id) === Number(itemId));
            if (!item) return;
            this.draftItemId = Number(item.id);
            const { name, color, hotkey, category } = this.getManageElements();
            if (name) name.value = String(item.name || '');
            if (color) color.value = String(item.color || '#4a90d9');
            if (hotkey) hotkey.value = String(item.hotkey || '');
            if (category) category.value = String(item.category || '');
            this.setStatus(`Editing: ${item.name || 'Soundboard item'}`);
        },

        collectPayload(overrides = {}) {
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            const { name, color, hotkey, category } = this.getManageElements();
            const payload = {
                station_id: stationId,
                name: String(name?.value || '').trim(),
                color: String(color?.value || '#4a90d9').trim() || '#4a90d9',
                hotkey: String(hotkey?.value || '').trim() || null,
                category: String(category?.value || '').trim() || null,
                ...overrides,
            };
            if (this.draftItemId) {
                payload.id = this.draftItemId;
            }
            return payload;
        },

        async handleUploadClick() {
            if (!this.getManagePermission()) return;
            const { file } = this.getManageElements();
            const selected = file?.files?.[0];
            if (!selected) {
                this.setStatus('Select an audio file first.');
                return;
            }
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            const result = await this.uploadAudio({ stationId, file: selected });
            if (result?.file_path) {
                const payload = this.collectPayload({
                    file_path: result.file_path,
                    duration_s: result.duration_s,
                    uploaded: 1,
                    name: this.collectPayload().name || String(result.original_name || selected.name || 'Sound Effect').replace(/\.[^.]+$/, ''),
                });
                const { name } = this.getManageElements();
                if (name && !String(name.value || '').trim()) {
                    name.value = String(payload.name || '');
                }
                this.setStatus(`Uploaded: ${result.original_name || selected.name}`);
                await this.saveItem(payload);
                await this.loadItems(stationId);
                this.resetForm();
            }
        },

        async handleSaveClick() {
            if (!this.getManagePermission()) return;
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            const payload = this.collectPayload();
            if (!payload.name) {
                this.setStatus('Name is required.');
                return;
            }
            const existing = this.draftItemId
                ? this.items.find(entry => Number(entry.id) === Number(this.draftItemId))
                : null;
            if (!payload.file_path && existing?.file_path) {
                payload.file_path = existing.file_path;
            }
            if (!payload.file_path) {
                this.setStatus('Upload an audio file before saving.');
                return;
            }
            await this.saveItem(payload);
            await this.loadItems(stationId);
            this.resetForm();
        },

        async deleteItem(itemId) {
            if (!this.getManagePermission() || typeof apiFetch !== 'function') return;
            await apiFetch(`/api/soundboard/${Number(itemId)}`, {
                method: 'DELETE',
            });
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            await this.loadItems(stationId);
            if (Number(this.draftItemId) === Number(itemId)) {
                this.resetForm();
            }
        },

        async playItem(itemId) {
            if (typeof apiFetch !== 'function') return;
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            try {
                await apiFetch('/api/soundboard/play', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ item_id: Number(itemId), station_id: stationId }),
                });
            } catch (_) {}
        },

        async stopItem(itemId) {
            if (typeof apiFetch !== 'function') return;
            const stationId = Number(
                (typeof currentState !== 'undefined' && currentState.currentStationId) || 1
            );
            try {
                await apiFetch('/api/soundboard/stop', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ item_id: Number(itemId), station_id: stationId }),
                });
            } catch (_) {}
        },

        handleWsEvent(event) {
            const eventType = String(event?.type || '').trim();
            if (eventType === 'soundboard.played') {
                const itemId = Number(event?.payload?.item_id);
                if (itemId > 0) this.playingItems.add(itemId);
                this._updateButtonState(itemId, true);
            }
            if (eventType === 'soundboard.stopped') {
                const stoppedAll = Boolean(event?.payload?.stopped_all);
                if (stoppedAll) {
                    this.playingItems.clear();
                    this._updateAllButtonStates();
                } else {
                    const itemId = Number(event?.payload?.item_id);
                    this.playingItems.delete(itemId);
                    this._updateButtonState(itemId, false);
                }
            }
        },

        _updateButtonState(itemId, playing) {
            if (typeof document === 'undefined') return;
            const btn = document.getElementById(`sb-btn-${itemId}`);
            if (btn && btn.classList) {
                btn.classList.toggle('playing', playing);
            }
        },

        _updateAllButtonStates() {
            this.items.forEach((item) => {
                this._updateButtonState(item.id, this.playingItems.has(item.id));
            });
        },

        renderLivePad() {
            const { panel, grid, status } = this.getLiveElements();
            if (!grid) return;

            const liveState = this.getLivePadState();
            if (panel) {
                panel.style.display = liveState.canPlay ? '' : 'none';
            }
            if (!liveState.canPlay) {
                grid.innerHTML = '';
                if (status) {
                    status.textContent = '';
                    status.classList?.remove?.('is-blocked');
                }
                return;
            }

            if (status) {
                status.textContent = liveState.hasClaim ? '' : liveState.blockedReason;
                status.classList?.toggle?.('is-blocked', !liveState.hasClaim);
            }

            if (!this.items.length) {
                grid.innerHTML = '<div class="live-program-empty">No soundboard items available for this station.</div>';
                return;
            }

            grid.innerHTML = this.items.map((item) => {
                const playing = this.playingItems.has(item.id) ? ' playing' : '';
                const hotkey = item.hotkey ? `<span class="hotkey-badge">${item.hotkey}</span>` : '';
                const dur = item.duration_s ? `${item.duration_s.toFixed(1)}s` : '';
                const disabled = liveState.hasClaim ? '' : ' disabled';
                return `<button class="soundboard-btn live-soundboard-btn${playing}" id="live-sb-btn-${item.id}" `
                    + `data-item-id="${item.id}" style="background-color:${item.color || '#4a90d9'}"${disabled}>`
                    + `${hotkey}<span class="sb-name">${item.name}</span>`
                    + `<span class="sb-duration">${dur}</span></button>`;
            }).join('');
        },

        render() {
            const grid = typeof document !== 'undefined' ? document.getElementById('soundboardGrid') : null;
            if (!grid) return;
            const { panel } = this.getManageElements();
            if (panel) {
                panel.style.display = this.getManagePermission() ? '' : 'none';
            }
            const canManage = this.getManagePermission();
            grid.innerHTML = this.items.map((item) => {
                const playing = this.playingItems.has(item.id) ? ' playing' : '';
                const hotkey = item.hotkey ? `<span class="hotkey-badge">${item.hotkey}</span>` : '';
                const dur = item.duration_s ? `${item.duration_s.toFixed(1)}s` : '';
                const actions = canManage
                    ? `<span class="soundboard-item-actions">`
                        + `<button class="soundboard-item-action" type="button" data-action="edit" data-item-id="${item.id}">Edit</button>`
                        + `<button class="soundboard-item-action" type="button" data-action="delete" data-item-id="${item.id}">Delete</button>`
                      + `</span>`
                    : '';
                return `<button class="soundboard-btn${playing}" id="sb-btn-${item.id}" `
                    + `data-item-id="${item.id}" style="background-color:${item.color || '#4a90d9'}">`
                    + `${hotkey}<span class="sb-name">${item.name}</span>`
                    + `<span class="sb-duration">${dur}</span>${actions}</button>`;
            }).join('');
            this.renderLivePad();
        },

        enableHotkeys() {
            if (this._hotkeyListener || typeof document === 'undefined') return;
            this._hotkeyListener = (e) => {
                const tag = String(document.activeElement?.tagName || '').toLowerCase();
                if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
                const key = String(e.key || '').toLowerCase();
                const match = this.items.find(
                    (item) => item.hotkey && String(item.hotkey).toLowerCase() === key
                );
                if (match) {
                    e.preventDefault();
                    if (this.playingItems.has(match.id)) {
                        this.stopItem(match.id);
                    } else {
                        this.playItem(match.id);
                    }
                }
            };
            document.addEventListener('keydown', this._hotkeyListener);
        },

        disableHotkeys() {
            if (this._hotkeyListener && typeof document !== 'undefined') {
                document.removeEventListener('keydown', this._hotkeyListener);
                this._hotkeyListener = null;
            }
        },

        destroy() {
            this.disableHotkeys();
            this.items = [];
            this.playingItems.clear();
        },
    };

    if (typeof globalThis !== 'undefined') {
        globalThis.SoundBoardManager = SoundBoardManager;
    }
})();
