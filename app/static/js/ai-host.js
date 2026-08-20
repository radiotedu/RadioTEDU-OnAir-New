// === AI HOST PANEL FUNCTIONS ===

window.loadAISettings = async function() {
    try {
        const sid = document.getElementById('stationSelector')?.value || 1;
        const d = await apiFetch('/api/ai/settings?station_id=' + sid);
        const t = document.getElementById('aiHostToggle');
        if (t) t.checked = d.ai_host_enabled;
        const l = document.getElementById('aiLlmModel');
        if (l) l.value = d.llm_model || '';
        const p = document.getElementById('aiTtsModelPath');
        if (p) p.value = d.tts_model_path || '';
        const v = document.getElementById('aiVoicePersona');
        if (v) v.value = d.voice_persona || 'auto';
        const a = document.getElementById('aiAnnouncementLength');
        if (a) a.value = d.announcement_max_seconds || 15;
        const h = document.getElementById('aiMusicHistory');
        if (h) h.checked = d.include_music_history;
        const e = document.getElementById('aiEducationalSegments');
        if (e) e.checked = d.educational_segments_enabled;
        const i = document.getElementById('aiStationIdInterval');
        if (i) i.value = d.station_id_announcement_interval || 1800;
        const prompt = document.getElementById('aiPromptTemplate');
        if (prompt) prompt.value = d.prompt_template || '';
    } catch (err) { console.warn('AI settings load failed:', err); }
};

window.saveAISettings = async function() {
    try {
        const sid = document.getElementById('stationSelector')?.value || 1;
        const btn = document.getElementById('aiSaveSettingsBtn');
        if (btn) { btn.disabled = true; btn.innerHTML = 'Saving...'; }
        await apiFetch('/api/ai/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                station_id: parseInt(sid),
                ai_host_enabled: document.getElementById('aiHostToggle')?.checked || false,
                llm_model: document.getElementById('aiLlmModel')?.value || '',
                tts_model_path: document.getElementById('aiTtsModelPath')?.value || '',
                voice_persona: document.getElementById('aiVoicePersona')?.value || 'auto',
                announcement_max_seconds: parseInt(document.getElementById('aiAnnouncementLength')?.value || 15),
                include_music_history: document.getElementById('aiMusicHistory')?.checked || false,
                educational_segments_enabled: document.getElementById('aiEducationalSegments')?.checked || false,
                station_id_announcement_interval: parseInt(document.getElementById('aiStationIdInterval')?.value || 1800),
                prompt_template: document.getElementById('aiPromptTemplate')?.value || '',
            })
        });
        showToast('AI settings saved!');
        if (window.loadAIStatus) await window.loadAIStatus();
    } catch (err) {
        showToast(
            { message: 'Failed to save AI settings', detail: String(err?.message || '').trim() },
            'error',
            { title: 'AI Host', duration: 9000 }
        );
    } finally {
        const btn = document.getElementById('aiSaveSettingsBtn');
        if (btn) { btn.disabled = false; btn.innerHTML = '<span class="material-icons-round">save</span> Save AI Settings'; }
    }
};

window.loadAIStatus = async function() {
    try {
        const sid = document.getElementById('stationSelector')?.value || 1;
        const d = await apiFetch('/api/ai/status?station_id=' + sid);
        const ll = document.getElementById('aiLlmStatus');
        if (ll) {
            const llmLabel = d.llm_loaded
                ? `Loaded (${d.llm_provider || 'local-qwen'})`
                : (d.llm_provider === 'template-fallback' ? 'Template fallback' : 'Local model available');
            ll.textContent = llmLabel;
            ll.style.color = d.llm_loaded || d.llm_provider === 'template-fallback' ? 'var(--green)' : 'var(--text-muted)';
        }
        const tt = document.getElementById('aiTtsStatus');
        if (tt) {
            tt.textContent = d.tts_provider || (d.tts_model_exists ? 'local-qwen-tts-available' : 'unavailable');
            tt.style.color = d.operational ? 'var(--green)' : (d.tts_loaded ? 'var(--green)' : 'var(--yellow)');
        }
        const pp = document.getElementById('aiPersonaStatus');
        if (pp) pp.textContent = d.current_persona || '-';
        const cc = document.getElementById('aiCacheStatus');
        if (cc) cc.textContent = d.cache_size || 0;
    } catch (err) { console.warn('AI status load failed:', err); }
};

window.loadAIAnnouncements = async function() {
    try {
        const sid = document.getElementById('stationSelector')?.value || 1;
        const d = await apiFetch('/api/ai/announcements?station_id=' + sid);
        const tb = document.getElementById('aiAnnouncementsTable');
        if (!tb) return;
        if (d.total === 0) { tb.innerHTML = '<tr><td colspan="3">No cached announcements</td></tr>'; return; }
        tb.innerHTML = d.announcements.map(a => {
            const title = String(a.title || 'AI Announcement');
            const type = String(a.announcement_type || 'announcement');
            const provider = String(a.tts_provider || 'unknown');
            const duration = Number(a.duration_seconds || 0);
            return '<tr>'
                + '<td>' + title + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">' + type + '</div></td>'
                + '<td>' + (a.size_bytes / 1024).toFixed(1) + ' KB'
                + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">' + (duration > 0 ? duration.toFixed(1) + 's' : 'duration pending') + '</div></td>'
                + '<td style="color:var(--green)">' + provider + '</td>'
                + '</tr>';
        }).join('');
    } catch (err) { console.warn('AI announcements load failed:', err); }
};

window.warmupAIModels = async function() {
    try {
        const btn = document.getElementById('aiWarmupBtn');
        if (btn) { btn.disabled = true; btn.innerHTML = 'Loading...'; }
        const sid = document.getElementById('stationSelector')?.value || 1;
        const result = await apiFetch('/api/ai/warmup?station_id=' + sid, {method: 'POST'});
        showToast(`AI ready: ${result.tts_provider || 'provider available'}`);
        if (window.loadAIStatus) await window.loadAIStatus();
    } catch (err) {
        showToast(
            { message: 'Failed to load AI models', detail: String(err?.message || '').trim() },
            'error',
            { title: 'AI Host', duration: 9000 }
        );
    } finally {
        const btn = document.getElementById('aiWarmupBtn');
        if (btn) { btn.disabled = false; btn.innerHTML = '<span class="material-icons-round">memory</span> Warm Up AI'; }
    }
};

window.clearAICache = async function() {
    if (!confirm('Clear AI cache?')) return;
    try {
        await apiFetch('/api/ai/clear-cache', {method: 'POST'});
        showToast('AI cache cleared!');
        if (window.loadAIStatus) await window.loadAIStatus();
        if (window.loadAIAnnouncements) await window.loadAIAnnouncements();
    } catch (err) {
        showToast(
            { message: 'Failed to clear AI cache', detail: String(err?.message || '').trim() },
            'error',
            { title: 'AI Host', duration: 9000 }
        );
    }
};

window.showAIHostPanel = function() {
    if (window.loadAISettings) window.loadAISettings();
    if (window.loadAIStatus) window.loadAIStatus();
    if (window.loadAIAnnouncements) window.loadAIAnnouncements();
};
