'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  let services, tracks = [], programs = [], selectedStation = 0, dragIndex = -1, creationToken = '', busy = false;
  const extensions = /\.(mp3|flac|wav|ogg|opus|m4a|aac|aiff?|wma)$/i;
  const jsonOptions = (method, value) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  const message = (text, error = false) => { $('eventResult').textContent = text; $('eventResult').className = 'action-result' + (error ? ' error' : ''); };
  const endpoint = () => '/api/stations/' + selectedStation + '/special-events';
  function renderTracks() {
    $('eventTrackCount').textContent = tracks.filter((t) => t.selected !== false).length + ' / ' + tracks.length;
    $('eventTracks').innerHTML = tracks.map((t, i) => '<li draggable="true" data-event-track="' + i + '"><input type="checkbox" aria-label="Şarkıyı seç" data-event-select="' + i + '" ' + (t.selected !== false ? 'checked' : '') + '><span>' + services.escapeHtml(t.relative_path || t.title) + '</span><button type="button" class="button ghost compact" aria-label="Yukarı taşı" data-event-up="' + i + '">↑</button><button type="button" class="button ghost compact" aria-label="Aşağı taşı" data-event-down="' + i + '">↓</button></li>').join('');
  }
  function move(from, to) {
    if (from < 0 || to < 0 || from >= tracks.length || to >= tracks.length) return;
    tracks.splice(to, 0, tracks.splice(from, 1)[0]); renderTracks();
  }
  function reset() {
    $('eventForm').reset(); $('eventId').value = ''; $('eventBoundary').value = 'boundary'; tracks = []; renderTracks();
    creationToken = window.crypto.randomUUID().replaceAll('-', '');
    const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Europe/Istanbul' });
    $('eventStartDate').value = today; $('eventEndDate').value = today;
    $('eventAudioFields').hidden = false; message('Yeni planı oluşturun; henüz yayına uygulanmadı.');
  }
  function renderPrograms() {
    $('eventProgramList').innerHTML = programs.length ? programs.map((p) => '<article class="planner-plan-card"><div><h3>' + services.escapeHtml(p.name) + '</h3><p>' + (p.enabled ? (p.active_now ? 'Şu anda zaman aralığında' : 'Planlandı') : 'Duraklatıldı') + ' · ' + p.track_count + ' şarkı · ' + (p.annual ? 'Her yıl' : 'Tek dönem') + '</p><p>' + services.escapeHtml(p.starts_on + ' — ' + p.ends_on + ' / ' + p.local_start + '–' + p.local_end + ' / ' + p.timezone) + '</p><p>' + services.escapeHtml(p.metadata_title || 'Şarkı metadata’sı') + '</p></div><div class="planner-plan-actions"><button type="button" class="button secondary" data-event-edit="' + p.id + '">Düzenle</button><button type="button" class="button ghost" data-event-toggle="' + p.id + '">' + (p.enabled ? 'Duraklat' : 'Etkinleştir') + '</button><button type="button" class="button danger" data-event-delete="' + p.id + '">Sil</button></div></article>').join('') : '<div class="empty-state">Bu kanalda henüz özel yayın yok.</div>';
  }
  async function refresh() { const result = await services.api(endpoint()); programs = result.programs || []; renderPrograms(); return result; }
  async function load(stationId) {
    if (!services) return;
    const list = services.getStations();
    $('eventStation').innerHTML = list.map((s) => '<option value="' + Number(s.id) + '">' + services.escapeHtml(s.name || String(s.id)) + '</option>').join('');
    selectedStation = Number(stationId || selectedStation || list[0]?.id || 0);
    $('eventStation').value = String(selectedStation);
    if (!selectedStation) return message('Önce bir kanal oluşturun.', true);
    try { await refresh(); reset(); } catch (e) { message(e.message, true); }
  }
  function edit(id) {
    const p = programs.find((p) => p.id === id); if (!p) return;
    const fields = { eventId:'id', eventName:'name', eventMode:'mode', eventStartDate:'starts_on', eventEndDate:'ends_on', eventStartTime:'local_start', eventEndTime:'local_end', eventTimezone:'timezone', eventPriority:'priority', eventMetadata:'metadata_title', eventFolder:'source_folder', eventOrder:'order', eventBoundary:'start_policy', eventCrossfade:'crossfade_seconds' };
    Object.entries(fields).forEach(([id, key]) => { $(id).value = p[key] ?? (key === 'start_policy' ? 'boundary' : ''); });
    [['eventAnnual','annual'],['eventLoop','repeat_playlist'],['eventEnabled','enabled'],['eventRecursive','recursive']].forEach(([id,key]) => { $(id).checked = Boolean(p[key]); });
    tracks = p.tracks.map((t) => ({ ...t, selected:true })); renderTracks();
    $('eventAudioFields').hidden = p.mode === 'metadata'; message('Plan düzenleniyor: ' + p.name);
  }
  async function scan() {
    const result = await services.api('/api/special-event-media/preview-folder', { ...jsonOptions('POST', { folder:$('eventFolder').value.trim(), recursive:$('eventRecursive').checked }), timeoutMs:120000 });
    if (!result.complete) throw new Error('Klasör taraması tamamlanmadı. Önceki liste korundu.');
    const previous = new Map(tracks.filter((t)=>t.path).map((t)=>[t.path,t]));
    const incoming = new Map(result.tracks.map((t)=>[t.path,t]));
    const retained = tracks.filter((t)=>incoming.has(t.path)).map((t)=>({...incoming.get(t.path),selected:t.selected!==false}));
    tracks = retained.concat(result.tracks.filter((t)=>!previous.has(t.path)).map((t)=>({...t,selected:true}))); renderTracks();
    message(tracks.length + ' şarkı bulundu. Seçimi ve sırayı kontrol edip kaydedin.');
  }
  async function chooseFolder() { $('eventFolderFiles').click(); }
  async function chooseNativeFolder() {
    const picked = await services.pickOperatorPath('folder', $('eventFolder').value, 'Özel yayın şarkı klasörü');
    if (picked.selected && picked.folder) { $('eventFolder').value = picked.folder; await scan(); }
  }
  function folderFiles(files) {
    const eligible = [...files].filter((file) => extensions.test(file.name));
    if (!eligible.length) return message('Seçilen klasörde desteklenen ses dosyası yok.', true);
    if (eligible.length > 2000) return message('En fazla 2000 şarkı seçilebilir; daha küçük bir klasör seçin.', true);
    if (eligible.some((file) => file.size > 512 * 1024 * 1024 || file.size === 0)) return message('Boş dosyaları veya 512 MB sınırını aşan şarkıları ayırın.', true);
    tracks = eligible.sort((a,b) => a.webkitRelativePath.localeCompare(b.webkitRelativePath)).map((file) => ({ file, title:file.name.replace(/\.[^.]+$/, ''), relative_path:file.webkitRelativePath, selected:true, artist:'', duration:0 }));
    $('eventFolder').value = ''; renderTracks(); message('Seçili şarkılar kaydedilirken uygulamanın ayrı havuzuna aktarılacak.');
  }
  async function save(event) {
    event.preventDefault(); if (busy) return; busy = true; $('eventSave').disabled = true;
    try {
      const selected = tracks.filter((t) => t.selected !== false);
      if ($('eventMode').value === 'playlist') {
        if (!selected.length) throw new Error('En az bir şarkı seçin.');
        for (let i=0;i<selected.length;i++) { const track=selected[i]; if (track.file && !track.path) { message('Şarkılar aktarılıyor: '+(i+1)+' / '+selected.length); const body=new FormData(); body.append('file',track.file); const uploaded=await services.api('/api/special-event-media/upload',{method:'POST',body,timeoutMs:180000}); Object.assign(track,uploaded.track); } }
      }
      const payload = { client_id:creationToken, name:$('eventName').value.trim(), mode:$('eventMode').value, starts_on:$('eventStartDate').value, ends_on:$('eventEndDate').value, local_start:$('eventStartTime').value, local_end:$('eventEndTime').value, timezone:$('eventTimezone').value.trim(), priority:Number($('eventPriority').value), annual:$('eventAnnual').checked, enabled:$('eventEnabled').checked, repeat_playlist:$('eventLoop').checked, metadata_title:$('eventMetadata').value.trim(), source_folder:$('eventFolder').value.trim(), recursive:$('eventRecursive').checked, order:$('eventOrder').value, start_policy:$('eventBoundary').value, crossfade_seconds:Number($('eventCrossfade').value), tracks:$('eventMode').value==='metadata'?[]:selected.map((t)=>({path:t.path,title:t.title,artist:t.artist||'',duration:t.duration||0})) };
      const id=$('eventId').value;
      message('Plan kaydediliyor ve kalıcı kayıt doğrulanıyor…');
      const result=await services.api(endpoint()+(id?'/'+id:''),jsonOptions(id?'PUT':'POST',payload));
      await refresh(); const actual=programs.find((p)=>p.id===result.program.id);
      if (!actual || Object.keys(payload).filter((key)=>key!=='client_id').some((key)=>JSON.stringify(actual[key])!==JSON.stringify(payload[key]))) throw new Error('Kaydedilen plan doğrulanamadı; listeyi yenileyip kontrol edin.');
      $('eventId').value=actual.id; message('Kaydedildi ve doğrulandı. Tarih/saat geldiğinde bu kanalda uygulanacak.');
    } catch(e) { message(e.message,true); } finally { busy=false; $('eventSave').disabled=false; }
  }
  function mount(injected) {
    services=injected; if (!$('eventForm')) return;
    $('eventForm').addEventListener('submit',save);
    $('eventNew').addEventListener('click',reset);
    $('eventMode').addEventListener('change',()=>{ $('eventAudioFields').hidden=$('eventMode').value==='metadata'; });
    $('eventStation').addEventListener('change',async()=>{selectedStation=Number($('eventStation').value);try{await refresh();reset();}catch(e){message(e.message,true);}});
    $('eventPickFolder').addEventListener('click',()=>chooseFolder().catch((e)=>message(e.message,true)));
    $('eventNativeFolder').addEventListener('click',()=>chooseNativeFolder().catch((e)=>message(e.message,true)));
    $('eventScanFolder').addEventListener('click',()=>scan().catch((e)=>message(e.message,true)));
    $('eventFolderFiles').addEventListener('change',()=>folderFiles($('eventFolderFiles').files));
    $('eventSelectAll').addEventListener('click',()=>{ tracks.forEach((t)=>t.selected=true);renderTracks(); });
    $('eventSelectNone').addEventListener('click',()=>{ tracks.forEach((t)=>t.selected=false);renderTracks(); });
    $('eventTracks').addEventListener('change',(e)=>{ if(e.target.dataset.eventSelect!==undefined){tracks[Number(e.target.dataset.eventSelect)].selected=e.target.checked;renderTracks();} });
    $('eventTracks').addEventListener('click',(e)=>{const up=e.target.closest('[data-event-up]'),down=e.target.closest('[data-event-down]');if(up)move(Number(up.dataset.eventUp),Number(up.dataset.eventUp)-1);if(down)move(Number(down.dataset.eventDown),Number(down.dataset.eventDown)+1);});
    $('eventTracks').addEventListener('dragstart',(e)=>{const row=e.target.closest('[data-event-track]');if(row){dragIndex=Number(row.dataset.eventTrack);e.dataTransfer.setData('text/plain',String(dragIndex));}});
    $('eventTracks').addEventListener('dragover',(e)=>e.preventDefault());
    $('eventTracks').addEventListener('drop',(e)=>{e.preventDefault();const row=e.target.closest('[data-event-track]');if(row)move(dragIndex,Number(row.dataset.eventTrack));dragIndex=-1;});
    $('eventProgramList').addEventListener('click',async(e)=>{
      const editButton=e.target.closest('[data-event-edit]'),toggle=e.target.closest('[data-event-toggle]'),remove=e.target.closest('[data-event-delete]');
      try {
        if(editButton)edit(editButton.dataset.eventEdit);
        if(toggle){const p=programs.find((p)=>p.id===toggle.dataset.eventToggle);await services.api(endpoint()+'/'+p.id,jsonOptions('PUT',{...p,enabled:!p.enabled}));await refresh();if(programs.find((item)=>item.id===p.id)?.enabled===p.enabled)throw new Error('Plan durumu doğrulanamadı.');message('Plan durumu kaydedildi ve doğrulandı.');}
        if(remove){if(Number(remove.dataset.armed||0)<Date.now()){remove.dataset.armed=String(Date.now()+20000);remove.textContent='Silmek için tekrar tıklayın';return;}await services.api(endpoint()+'/'+remove.dataset.eventDelete,{method:'DELETE'});await refresh();if(programs.some((p)=>p.id===remove.dataset.eventDelete))throw new Error('Plan silme doğrulanamadı.');reset();}
      }catch(e){message(e.message,true);}
    });
  }
  window.RadioSpecialEvents={mount,load};
})();
