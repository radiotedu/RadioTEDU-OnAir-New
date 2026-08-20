(function () {
    function escapeHtml(value) {
        const text = String(value ?? '');
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function clipText(value, maxLen = 220) {
        const text = String(value ?? '').trim();
        if (!text) return '';
        return text.length > maxLen ? `${text.slice(0, maxLen - 1)}…` : text;
    }

    function normalizeStudio(row) {
        return {
            id: Number(row?.id || 0),
            station_id: Number(row?.station_id || 0),
            name: String(row?.name || `Studio ${row?.id || ''}`).trim() || `Studio ${row?.id || ''}`,
            description: String(row?.description || ''),
            sort_order: Number(row?.sort_order || 1),
            is_active: Boolean(row?.is_active),
            is_on_air: Boolean(row?.is_on_air),
            current_user_id: row?.current_user_id === null || row?.current_user_id === undefined
                ? null
                : Number(row.current_user_id),
            joined: Boolean(row?.joined),
            live_presence_count: Number(row?.live_presence_count || 0),
            active_dj: row?.active_dj && typeof row.active_dj === 'object' ? { ...row.active_dj } : null,
        };
    }

    function normalizeMessage(row) {
        const userName = String(row?.user_name || row?.username || row?.display_name || '').trim();
        return {
            id: Number(row?.id || 0),
            studio_id: Number(row?.studio_id || 0),
            user_id: Number(row?.user_id || 0),
            user_name: userName || `User ${row?.user_id || ''}`.trim(),
            message: String(row?.message || ''),
            created_at: String(row?.created_at || ''),
        };
    }

    function getCurrentUser() {
        if (globalThis.Auth && typeof globalThis.Auth.getUser === 'function') {
            return globalThis.Auth.getUser() || null;
        }
        return null;
    }

    function getApiFetch() {
        return typeof globalThis.apiFetch === 'function' ? globalThis.apiFetch : null;
    }

    const StudioManager = {
        stationId: 0,
        initialized: false,
        async init() {
            this.bindUi();
            this.initialized = true;
            this.render();
        },
        bindUi() {
            const stripEl = document.getElementById('studioStrip');
            if (stripEl && stripEl.dataset.boundStudio === '1') {
                return;
            }
            if (stripEl) {
                stripEl.dataset.boundStudio = '1';
                stripEl.addEventListener('click', async (event) => {
                    const actionEl = event.target?.closest?.('[data-studio-action][data-studio-id]');
                    if (!actionEl) return;
                    const studioId = Number(actionEl.dataset.studioId || 0);
                    if (!Number.isInteger(studioId) || studioId <= 0) return;
                    const action = String(actionEl.dataset.studioAction || '').trim();
                    if (action === 'select') {
                        await this.selectStudio(studioId);
                        return;
                    }
                    if (action === 'join') {
                        await this.joinStudio(studioId);
                        return;
                    }
                    if (action === 'leave') {
                        await this.leaveStudio(studioId);
                    }
                });
            }

            const formEl = document.getElementById('studioChatForm');
            if (formEl && formEl.dataset.boundStudio !== '1') {
                formEl.dataset.boundStudio = '1';
                formEl.addEventListener('submit', async (event) => {
                    event.preventDefault();
                    await this.submitChat();
                });
            }
        },
        _getSelectedStudio() {
            const selectedId = Number(globalThis.currentState?.selectedStudioId || 0);
            return (Array.isArray(globalThis.currentState?.studios) ? globalThis.currentState.studios : [])
                .find(row => Number(row.id) === selectedId) || null;
        },
        _hasJoinedStudio(studioId) {
            return Number(globalThis.currentState?.joinedStudioId || 0) === Number(studioId || 0);
        },
        _canChat() {
            const selected = Number(globalThis.currentState?.selectedStudioId || 0);
            return selected > 0 && this._hasJoinedStudio(selected);
        },
        render() {
            this.renderStrip();
            this.renderChat();
            this.renderSelectionSummary();
        },
        renderStrip() {
            const stripEl = document.getElementById('studioStrip');
            const studios = Array.isArray(globalThis.currentState?.studios)
                ? globalThis.currentState.studios.map(normalizeStudio)
                : [];
            const selectedId = Number(globalThis.currentState?.selectedStudioId || 0);
            const user = getCurrentUser();
            const presenceCount = Number(globalThis.currentState?.studioPresence?.count || 0);

            if (!stripEl) return;
            if (!studios.length) {
                stripEl.innerHTML = `
                    <div class="studio-strip-empty">
                        <span class="material-icons-round">forum</span>
                        <span>No studios available yet.</span>
                    </div>
                `;
                return;
            }

            stripEl.innerHTML = studios.map((studio) => {
                const selected = Number(studio.id) === selectedId;
                const joined = this._hasJoinedStudio(studio.id) || studio.joined;
                const ownerName = studio.active_dj?.username
                    || (studio.current_user_id && user && Number(user.id) === Number(studio.current_user_id) ? user.username : '')
                    || (studio.current_user_id ? `User ${studio.current_user_id}` : 'Open');
                const actionLabel = joined ? 'Leave' : 'Join';
                const statusLabel = studio.is_on_air ? 'ON AIR' : 'STANDBY';
                return `
                    <div class="studio-strip-item ${selected ? 'selected' : ''} ${joined ? 'joined' : ''} ${studio.is_on_air ? 'on-air' : ''}"
                         data-studio-id="${studio.id}">
                        <button class="studio-strip-select" type="button" data-studio-action="select" data-studio-id="${studio.id}">
                            <span class="studio-strip-name">${escapeHtml(studio.name)}</span>
                            <span class="studio-strip-status">${escapeHtml(statusLabel)}</span>
                        </button>
                        <div class="studio-strip-meta">
                            <span>${escapeHtml(joined ? 'Joined' : 'Read only')}</span>
                            <span>${escapeHtml(studio.live_presence_count > 0 ? `${studio.live_presence_count} in room` : ownerName)}</span>
                        </div>
                        <div class="studio-strip-actions">
                            <button class="btn-sm" type="button" data-studio-action="${joined ? 'leave' : 'join'}" data-studio-id="${studio.id}">
                                ${escapeHtml(actionLabel)}
                            </button>
                        </div>
                    </div>
                `;
            }).join('');

            const presenceEl = document.getElementById('studioPresenceBadge');
            if (presenceEl) {
                presenceEl.textContent = presenceCount > 0
                    ? `${presenceCount} connected`
                    : 'Station presence hidden';
            }
        },
        renderSelectionSummary() {
            const selected = this._getSelectedStudio();
            const titleEl = document.getElementById('studioSelectedTitle');
            const metaEl = document.getElementById('studioSelectedMeta');
            const hintEl = document.getElementById('studioJoinHint');
            if (titleEl) {
                titleEl.textContent = selected ? selected.name : 'Select a studio';
            }
            if (metaEl) {
                if (!selected) {
                    metaEl.textContent = 'No studio selected.';
                } else {
                    const stateBits = [];
                    stateBits.push(selected.is_on_air ? 'On air' : 'Off air');
                    stateBits.push(this._hasJoinedStudio(selected.id) ? 'You are joined here' : 'Read-only');
                    metaEl.textContent = stateBits.join(' · ');
                }
            }
            if (hintEl) {
                if (!selected) {
                    hintEl.hidden = false;
                    hintEl.textContent = 'Select a studio to view its chat.';
                } else if (!this._canChat()) {
                    hintEl.hidden = false;
                    hintEl.textContent = 'Join this studio to chat';
                } else {
                    hintEl.hidden = true;
                    hintEl.textContent = '';
                }
            }
            this.syncComposerState();
        },
        renderChat() {
            const historyEl = document.getElementById('studioChatHistory');
            const chatStateEl = document.getElementById('studioChatState');
            const messages = Array.isArray(globalThis.currentState?.chatHistory)
                ? globalThis.currentState.chatHistory.map(normalizeMessage)
                : [];
            const canChat = this._canChat();

            if (historyEl) {
                if (!messages.length) {
                    historyEl.innerHTML = '<div class="studio-chat-empty">No messages yet.</div>';
                } else {
                    historyEl.innerHTML = messages.map((message) => `
                        <div class="studio-chat-message">
                            <div class="studio-chat-message-head">
                                <span class="studio-chat-message-user">${escapeHtml(message.user_name)}</span>
                                <span class="studio-chat-message-time">${escapeHtml(message.created_at || '')}</span>
                            </div>
                            <div class="studio-chat-message-body">${escapeHtml(message.message)}</div>
                        </div>
                    `).join('');
                }
            }

            if (chatStateEl) {
                if (canChat) {
                    chatStateEl.hidden = true;
                    chatStateEl.textContent = '';
                } else {
                    chatStateEl.hidden = false;
                    chatStateEl.textContent = 'Join this studio to chat';
                }
            }
            this.syncComposerState();
        },
        syncComposerState() {
            const canChat = this._canChat();
            const inputEl = document.getElementById('studioChatInput');
            const buttonEl = document.getElementById('studioChatSendBtn');
            if (inputEl) {
                inputEl.disabled = !canChat;
                if (!canChat) {
                    inputEl.placeholder = 'Join this studio to chat';
                }
            }
            if (buttonEl) {
                buttonEl.disabled = !canChat;
            }
        },
        async refresh(options = {}) {
            const fetchApi = getApiFetch();
            if (!fetchApi) return null;
            const stationId = Number(options.stationId || globalThis.currentState?.currentStationId || 1);
            this.stationId = stationId;
            const snapshot = await fetchApi(`/api/studios?station_id=${stationId}`);
            await this.applySnapshot(snapshot || {});
            const selectedStudioId = Number(globalThis.currentState?.selectedStudioId || 0);
            const snapshotChatMessages = Array.isArray(snapshot?.chat_messages) ? snapshot.chat_messages : null;
            if (
                selectedStudioId > 0
                && (!Array.isArray(snapshotChatMessages) || snapshotChatMessages.length === 0)
            ) {
                await this.loadChatHistory(selectedStudioId);
            }
            return snapshot;
        },
        async applySnapshot(snapshot = {}) {
            const studios = Array.isArray(snapshot.studios) ? snapshot.studios.map(normalizeStudio) : [];
            const currentSelected = Number(globalThis.currentState?.selectedStudioId || 0);
            const selectedExists = currentSelected > 0 && studios.some(studio => Number(studio.id) === currentSelected);
            const incomingSelected = Number(snapshot.selected_studio_id || 0);
            const incomingExists = incomingSelected > 0 && studios.some(studio => Number(studio.id) === incomingSelected);
            const joinedStudio = studios.find(studio => studio.joined) || null;
            const selectedStudioId = selectedExists
                ? currentSelected
                : incomingExists
                    ? incomingSelected
                    : (joinedStudio?.id || studios[0]?.id || 0);
            const selectedStudio = studios.find(studio => Number(studio.id) === Number(selectedStudioId)) || null;
            const joinedStudioId = selectedStudio && selectedStudio.joined
                ? Number(selectedStudio.id)
                : Number(joinedStudio?.id || 0);

            if (globalThis.currentState) {
                globalThis.currentState.studios = studios;
                globalThis.currentState.selectedStudioId = Number(selectedStudioId || 0);
                globalThis.currentState.joinedStudioId = Number(joinedStudioId || 0);
                globalThis.currentState.chatHistory = Array.isArray(snapshot.chat_messages)
                    ? snapshot.chat_messages.map(normalizeMessage)
                    : Array.isArray(globalThis.currentState.chatHistory)
                        ? globalThis.currentState.chatHistory
                        : [];
                globalThis.currentState.studioPresence = {
                    ...(globalThis.currentState.studioPresence || {}),
                    count: Number(globalThis.currentState.studioPresence?.count || 0),
                };
            }
            this.stationId = Number(snapshot.station_id || this.stationId || globalThis.currentState?.currentStationId || 1);
            this.render();
            return {
                station_id: this.stationId,
                selected_studio_id: Number(selectedStudioId || 0),
                joined_studio_id: Number(joinedStudioId || 0),
                studios,
                chat_history: Array.isArray(globalThis.currentState?.chatHistory)
                    ? globalThis.currentState.chatHistory
                    : [],
            };
        },
        async loadChatHistory(studioId) {
            const selected = Number(studioId || 0);
            const fetchApi = getApiFetch();
            if (selected <= 0 || !fetchApi) {
                if (globalThis.currentState) {
                    globalThis.currentState.chatHistory = [];
                }
                this.renderChat();
                return [];
            }
            const data = await fetchApi(`/api/studios/${selected}/chat?limit=50`);
            const messages = Array.isArray(data?.messages)
                ? data.messages.map(normalizeMessage)
                : Array.isArray(data?.chat_history)
                    ? data.chat_history.map(normalizeMessage)
                    : [];
            if (globalThis.currentState) {
                globalThis.currentState.chatHistory = messages;
            }
            this.renderChat();
            return messages;
        },
        async selectStudio(studioId) {
            const selected = Number(studioId || 0);
            if (!Number.isInteger(selected) || selected <= 0) return;
            if (globalThis.currentState) {
                globalThis.currentState.selectedStudioId = selected;
            }
            this.render();
            await this.loadChatHistory(selected);
        },
        async joinStudio(studioId) {
            const selected = Number(studioId || 0);
            const fetchApi = getApiFetch();
            if (!Number.isInteger(selected) || selected <= 0 || !fetchApi) return;
            await fetchApi(`/api/studios/${selected}/join`, { method: 'POST' });
            await this.refresh({ stationId: this.stationId || globalThis.currentState?.currentStationId || 1, force: true });
        },
        async leaveStudio(studioId) {
            const selected = Number(studioId || 0);
            const fetchApi = getApiFetch();
            if (!Number.isInteger(selected) || selected <= 0 || !fetchApi) return;
            await fetchApi(`/api/studios/${selected}/leave`, { method: 'POST' });
            await this.refresh({ stationId: this.stationId || globalThis.currentState?.currentStationId || 1, force: true });
        },
        async submitChat() {
            const inputEl = document.getElementById('studioChatInput');
            if (!inputEl) return null;
            const message = clipText(inputEl.value || '', 1000).trim();
            if (!message) return null;
            const selected = Number(globalThis.currentState?.selectedStudioId || 0);
            const fetchApi = getApiFetch();
            if (selected <= 0 || !this._canChat() || !fetchApi) return null;
            const response = await fetchApi(`/api/studios/${selected}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message }),
            });
            if (globalThis.currentState) {
                const nextMessage = normalizeMessage(response?.message || response || {});
                globalThis.currentState.chatHistory = [
                    ...(Array.isArray(globalThis.currentState.chatHistory) ? globalThis.currentState.chatHistory : []),
                    nextMessage,
                ];
            }
            inputEl.value = '';
            this.renderChat();
            return response;
        },
        handleWsEvent(event) {
            const eventType = String(event?.type || '').trim().toLowerCase();
            if (eventType === 'studio.status') {
                return this.applySnapshot(event?.payload || {});
            }
            if (eventType === 'dj.presence') {
                if (globalThis.currentState) {
                    globalThis.currentState.studioPresence = {
                        ...(event?.payload || {}),
                        count: Number(event?.payload?.count || 0),
                    };
                }
                this.renderStrip();
                return globalThis.currentState?.studioPresence || null;
            }
            if (eventType === 'chat.message') {
                const payload = normalizeMessage(event?.payload || {});
                if (!Array.isArray(globalThis.currentState?.chatHistory)) {
                    globalThis.currentState.chatHistory = [];
                }
                const selectedStudioId = Number(globalThis.currentState?.selectedStudioId || 0);
                if (selectedStudioId > 0 && Number(payload.studio_id || 0) === selectedStudioId) {
                    globalThis.currentState.chatHistory = [
                        ...globalThis.currentState.chatHistory,
                        payload,
                    ];
                    this.renderChat();
                }
                return payload;
            }
            return null;
        },
    };

    if (typeof globalThis !== 'undefined') {
        globalThis.StudioManager = StudioManager;
    }
})();
