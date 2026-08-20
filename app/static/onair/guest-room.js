(() => {
  'use strict';
  const panel = document.getElementById('guestRoomPanel');
  if (!panel) return;
  const state = globalThis.radioTEDUOnAirState;
  if (!state) return;
  const byId = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  let room = null; let studioId = 0; let talkbackStream = null; let talkbackRecorder = null;

  function selectedStudioId() {
    return Number(state.joinedStudioId || state.selectedStudioId || (state.studios || [])[0]?.id || 0);
  }
  function result(id, text, kind = '') { const node = byId(id); node.textContent = text || ''; node.className = `action-result ${kind}`; }
  async function request(path, options = {}) { return api(path, { idempotent: ['POST', 'PUT', 'PATCH', 'DELETE'].includes(String(options.method || 'GET').toUpperCase()), ...options }); }

  function participantCard(session) {
    const lobby = session.status === 'lobby'; const admitted = session.status === 'admitted'; const onAir = Boolean(session.is_on_air);
    const controls = lobby
      ? `<button class="button primary compact" data-guest-action="admit" data-session-id="${session.id}">Admit off-air</button><button class="button danger compact" data-guest-action="reject" data-session-id="${session.id}">Reject</button>`
      : admitted
        ? `<button class="button ${onAir ? 'danger' : 'primary'} compact" data-guest-action="onair" data-session-id="${session.id}" data-value="${onAir ? '0' : '1'}">${onAir ? 'Take off air' : 'Take on air'}</button><button class="button secondary compact" data-guest-action="mute" data-session-id="${session.id}" data-value="${session.is_muted ? '0' : '1'}">${session.is_muted ? 'Unmute' : 'Mute'}</button><label>Gain <input data-guest-gain="${session.id}" type="range" min="-24" max="12" step="1" value="${Number(session.gain_db || 0)}"></label><button class="button danger compact" data-guest-action="kick" data-session-id="${session.id}">Kick</button>`
        : `<span>${esc(session.status)}</span>`;
    return `<div class="guest-participant ${onAir ? 'guest-on-air' : ''}"><div><b>${esc(session.display_name)}</b><br><small>${session.is_connected ? esc(session.connection_quality || 'connected') : 'disconnected'} · ${lobby ? 'Lobby — cannot be heard' : onAir ? 'ON AIR' : 'Off air'}</small></div><div class="guest-participant-controls">${controls}</div></div>`;
  }

  function render() {
    if (!room) return;
    const sessions = room.sessions || [];
    byId('guestRoomParticipants').innerHTML = sessions.length ? sessions.map(participantCard).join('') : '<p class="card-copy">No guests are connected. Create a one-time link to invite someone.</p>';
    const onAir = sessions.filter((item) => item.status === 'admitted' && item.is_on_air).length;
    byId('guestRoomState').textContent = onAir ? `${onAir} guest${onAir === 1 ? '' : 's'} on air` : 'Safe off-air';
    byId('guestRoomState').classList.toggle('danger', onAir > 0);
    const recording = room.recording;
    byId('guestStopRecordButton').hidden = !recording || recording.status !== 'recording';
    byId('guestRecordButton').disabled = Boolean(recording);
    if (recording) {
      const consents = sessions.filter((item) => item.status === 'admitted').length;
      result('guestRecordingResult', recording.status === 'recording' ? `Recording #${recording.id} is active.` : `Waiting for ${consents} admitted guest consent response(s).`);
    } else result('guestRecordingResult', 'Recording is off.');
  }

  async function refresh() {
    const nextStudio = selectedStudioId();
    if (!nextStudio) { room = null; byId('guestRoomParticipants').innerHTML = '<p class="card-copy">Join a studio to manage remote guests.</p>'; return; }
    studioId = nextStudio;
    try { room = await request(`/api/studios/${studioId}/guest-room`); delete panel.dataset.accessHidden; panel.hidden = state.activeView !== 'onair'; render(); }
    catch (error) { if (Number(error.status || 0) === 403) { panel.dataset.accessHidden = 'true'; panel.hidden = true; } else result('guestInviteResult', errorMessage(error), 'error'); }
  }

  async function ensureStudio() {
    const selected = selectedStudioId();
    if (selected) { studioId = selected; return selected; }
    const studio = await ensureOperatorStudioOwnership(Number(state.stationId));
    studioId = Number(studio.id);
    return studioId;
  }

  byId('createGuestInviteButton').addEventListener('click', async () => {
    try { await ensureStudio(); const invite = await request(`/api/studios/${studioId}/guest-invites`, { method: 'POST' }); await navigator.clipboard.writeText(invite.join_url); result('guestInviteResult', `Copied a one-time link. It expires at ${new Date(invite.expires_at).toLocaleTimeString()}.`, 'success'); await refresh(); }
    catch (error) { result('guestInviteResult', errorMessage(error), 'error'); }
  });
  byId('allGuestsOffAirButton').addEventListener('click', async () => { if (!studioId) return; try { await request(`/api/studios/${studioId}/guest-room/all-off-air`, { method: 'POST' }); result('guestInviteResult', 'Verified: every guest is off-air.', 'success'); await refresh(); } catch (error) { result('guestInviteResult', errorMessage(error), 'error'); } });
  byId('guestRoomParticipants').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-guest-action]'); if (!button) return;
    const sessionId = Number(button.dataset.sessionId); const action = button.dataset.guestAction;
    try {
      if (['admit', 'reject', 'kick'].includes(action)) await request(`/api/studios/${studioId}/guest-room/${sessionId}/${action}`, { method: 'POST' });
      if (action === 'onair') await request(`/api/studios/${studioId}/guest-room/${sessionId}/audio`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on_air: button.dataset.value === '1' }) });
      if (action === 'mute') await request(`/api/studios/${studioId}/guest-room/${sessionId}/audio`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ muted: button.dataset.value === '1' }) });
      await refresh();
    } catch (error) { result('guestInviteResult', errorMessage(error), 'error'); }
  });
  byId('guestRoomParticipants').addEventListener('change', async (event) => { const input = event.target.closest('[data-guest-gain]'); if (!input) return; try { await request(`/api/studios/${studioId}/guest-room/${Number(input.dataset.guestGain)}/audio`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ gain_db: Number(input.value) }) }); await refresh(); } catch (error) { result('guestInviteResult', errorMessage(error), 'error'); } });

byId('guestRecordButton').addEventListener('click', async () => { try { const recording = await request(`/api/studios/${studioId}/guest-recordings`, { method: 'POST' }); result('guestRecordingResult', recording.status === 'recording' ? 'Recording started.' : 'Consent requested. Recording will start only after everyone agrees.', 'success'); await refresh(); window.dispatchEvent(new CustomEvent('radiotedu:guest-recordings-changed')); } catch (error) { result('guestRecordingResult', errorMessage(error), 'error'); } });
byId('guestStopRecordButton').addEventListener('click', async () => { try { await request(`/api/studios/${studioId}/guest-recordings/${room.recording.id}/stop`, { method: 'POST' }); result('guestRecordingResult', 'Recording stopped and finalized.', 'success'); await refresh(); window.dispatchEvent(new CustomEvent('radiotedu:guest-recordings-changed')); } catch (error) { result('guestRecordingResult', errorMessage(error), 'error'); } });

  async function startTalkback(event) {
    event.preventDefault();
    if (!studioId || talkbackRecorder) return;
    try {
      talkbackStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
      await request(`/api/studios/${studioId}/guest-room/talkback/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: Number(state.stationId), input_format: 'webm' }) });
      talkbackRecorder = new MediaRecorder(talkbackStream, { mimeType: 'audio/webm;codecs=opus' });
      talkbackRecorder.ondataavailable = async (chunk) => { if (chunk.data.size) await request(`/api/studios/${studioId}/guest-room/talkback/chunk?station_id=${Number(state.stationId)}`, { method: 'POST', body: chunk.data }); };
      talkbackRecorder.start(120); byId('guestTalkbackButton').textContent = 'Talking privately — release to stop';
    } catch (error) { result('guestInviteResult', errorMessage(error), 'error'); stopTalkback(); }
  }
  async function stopTalkback() {
    if (talkbackRecorder && talkbackRecorder.state !== 'inactive') talkbackRecorder.stop();
    talkbackRecorder = null; talkbackStream?.getTracks().forEach((track) => track.stop()); talkbackStream = null;
    byId('guestTalkbackButton').textContent = 'Hold to talk privately';
    if (studioId) await request(`/api/studios/${studioId}/guest-room/talkback/stop?station_id=${Number(state.stationId)}`, { method: 'POST' }).catch(() => {});
  }
  ['pointerdown', 'touchstart'].forEach((name) => byId('guestTalkbackButton').addEventListener(name, startTalkback));
  ['pointerup', 'pointercancel', 'pointerleave', 'touchend'].forEach((name) => byId('guestTalkbackButton').addEventListener(name, stopTalkback));
  window.setInterval(refresh, 2000); window.addEventListener('load', refresh);
})();
