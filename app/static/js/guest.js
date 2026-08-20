(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { invite: '', sessionToken: sessionStorage.getItem('onairGuestSession') || '', stream: null, ws: null, pc: null, muted: false, reconnect: 0, recordingId: 0 };

  function message(text, error = false) { $('message').textContent = text || ''; $('message').style.color = error ? '#ff6577' : '#39d98a'; }
  async function post(path, body) {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
    return payload;
  }

  async function enumerate() {
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === 'audioinput');
    $('microphone').innerHTML = devices.map((device, index) => `<option value="${device.deviceId}">${device.label || `Microphone ${index + 1}`}</option>`).join('');
    const context = new AudioContext();
    const analyser = context.createAnalyser(); analyser.fftSize = 512;
    context.createMediaStreamSource(state.stream).connect(analyser);
    const values = new Uint8Array(analyser.fftSize);
    const draw = () => { analyser.getByteTimeDomainData(values); let peak = 0; values.forEach((v) => { peak = Math.max(peak, Math.abs(v - 128)); }); $('meterFill').style.width = `${Math.min(100, peak * 2.5)}%`; requestAnimationFrame(draw); };
    draw();
  }

  async function redeem(name) {
    const result = await post('/api/guest/redeem', { invite_token: state.invite, display_name: name });
    state.sessionToken = result.session_token;
    sessionStorage.setItem('onairGuestSession', state.sessionToken);
  }

  async function connectWebSocket() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${scheme}//${location.host}/ws/guest`);
    state.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: 'guest.auth', session_token: state.sessionToken }));
    ws.onmessage = async (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === 'guest.authenticated') { state.reconnect = 0; await startPeer(payload.ice_servers || []); }
      if (payload.type === 'webrtc.answer' && state.pc) await state.pc.setRemoteDescription({ type: 'answer', sdp: payload.sdp });
      if (payload.type === 'webrtc.ice' && payload.candidate && state.pc) await state.pc.addIceCandidate(payload.candidate);
      if (payload.type === 'guest.error' || payload.type === 'webrtc.error') message(payload.detail || 'Connection failed.', true);
    };
    ws.onclose = () => { if (state.sessionToken) { const delay = Math.min(10000, 500 * (2 ** state.reconnect++)); $('roomStatus').textContent = 'Connection lost. Reconnecting safely…'; setTimeout(connectWebSocket, delay); } };
  }

  async function startPeer(iceServers) {
    if (!state.stream) await enumerate();
    if (state.pc) state.pc.close();
    const pc = new RTCPeerConnection({ iceServers }); state.pc = pc;
    state.stream.getAudioTracks().forEach((track) => pc.addTrack(track, state.stream));
    pc.ontrack = (event) => { $('returnAudio').srcObject = event.streams[0] || new MediaStream([event.track]); };
    pc.onicecandidate = (event) => { if (event.candidate && state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: 'webrtc.ice', candidate: event.candidate.toJSON() })); };
    pc.onconnectionstatechange = () => { $('roomStatus').textContent = pc.connectionState === 'connected' ? 'Connected. You are safe in the lobby until the presenter puts you on air.' : `Audio connection: ${pc.connectionState}`; };
    const offer = await pc.createOffer(); await pc.setLocalDescription(offer);
    state.ws.send(JSON.stringify({ type: 'webrtc.offer', sdp: offer.sdp }));
  }

  async function poll() {
    if (!state.sessionToken) return;
    try {
      const session = await post('/api/guest/session', { session_token: state.sessionToken });
      const labels = { lobby: 'Waiting in the lobby', admitted: session.is_on_air ? 'You are ON AIR' : 'Admitted — off air', kicked: 'Removed by the presenter', rejected: 'The presenter ended this invitation' };
      $('roomHeading').textContent = labels[session.status] || session.status;
      $('roomStatus').textContent = session.is_on_air ? 'Your microphone can be heard on the broadcast.' : 'Your microphone cannot be heard on the broadcast.';
      const consent = session.recording_consent;
      state.recordingId = Number(consent?.recording_id || 0);
      $('consentPanel').classList.toggle('hidden', !consent || consent.decision !== 'pending');
    } catch (error) { message(error.message, true); }
  }

  $('joinForm').addEventListener('submit', async (event) => { event.preventDefault(); try { await redeem($('displayName').value); $('joinPanel').classList.add('hidden'); $('devicePanel').classList.remove('hidden'); await enumerate(); } catch (error) { message(error.message, true); } });
  $('microphone').addEventListener('change', async () => { state.stream?.getTracks().forEach((track) => track.stop()); state.stream = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: { exact: $('microphone').value }, echoCancellation: true, noiseSuppression: true }, video: false }); });
  $('connectButton').addEventListener('click', async () => { $('devicePanel').classList.add('hidden'); $('roomPanel').classList.remove('hidden'); await connectWebSocket(); setInterval(poll, 2000); poll(); });
  $('muteButton').addEventListener('click', async () => { state.muted = !state.muted; state.stream?.getAudioTracks().forEach((track) => { track.enabled = !state.muted; }); await post('/api/guest/mute', { session_token: state.sessionToken, muted: state.muted }); $('muteButton').textContent = state.muted ? 'Unmute myself' : 'Mute myself'; });
  $('leaveButton').addEventListener('click', () => { state.sessionToken = ''; sessionStorage.removeItem('onairGuestSession'); state.ws?.close(); state.pc?.close(); state.stream?.getTracks().forEach((track) => track.stop()); location.reload(); });
  async function consent(accepted) { try { await post('/api/guest/consent', { session_token: state.sessionToken, recording_id: state.recordingId, accepted }); $('consentPanel').classList.add('hidden'); message(accepted ? 'Your recording consent was saved.' : 'Recording was declined and will not start.'); } catch (error) { message(error.message, true); } }
  $('consentYes').addEventListener('click', () => consent(true)); $('consentNo').addEventListener('click', () => consent(false));

  state.invite = new URLSearchParams(location.hash.slice(1)).get('invite') || '';
  if (state.sessionToken) { $('joinPanel').classList.add('hidden'); $('devicePanel').classList.remove('hidden'); enumerate().catch((error) => message(error.message, true)); }
  else if (!state.invite) message('This invitation link is missing or incomplete. Ask the presenter for a new link.', true);
})();
