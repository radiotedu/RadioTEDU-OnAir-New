"""Repair filename-derived title/artist pollution without overwriting good tags."""
from __future__ import annotations
import argparse,json,re,sqlite3
from pathlib import Path
from reconcile_library_metadata import AUDIO_EXTENSIONS, DB_PATH, canonical, discover, load_cache, parse_filename, read_rows, tag_snapshot, write_tags, norm_text

BAD=re.compile(r'(?i)(official|lyrics?|youtube|spotdown|no copyright|royalty free|free music|test-codex|\[|\]|__|\b\d{1,4}\s*[-._])')

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--backup',type=Path,required=True); args=ap.parse_args()
    con=sqlite3.connect(DB_PATH,timeout=60); con.execute('pragma busy_timeout=60000'); con.row_factory=sqlite3.Row
    rows=read_rows(con); profiles=discover(con); cache=load_cache(); backup={}; changed=0; scanned=0
    for sid,root in profiles:
        if not root.exists(): continue
        for path in root.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS: continue
            scanned+=1; row=rows.get((sid,canonical(path)),{})
            parsed=parse_filename(path,root)
            title=str(row.get('title') or '').strip(); artist=str(row.get('artist') or '').strip(); album=str(row.get('album') or '').strip()
            if (not title or BAD.search(title)) and parsed['title']: title=parsed['title']
            if (not artist or BAD.search(artist)) and parsed['artist']: artist=parsed['artist']
            key=f'{norm_text(parsed["artist"] or artist)}\t{norm_text(parsed["title"] or title)}'
            mb=cache.get(key,{}) if isinstance(cache,dict) else {}
            if mb.get('musicbrainz_recordingid'):
                title=mb.get('title') or title; artist=mb.get('artist') or artist; album=album or mb.get('album','')
            elif not album and parsed['album']:
                album=parsed['album']
            values={'title':title,'artist':artist,'album':album,'musicbrainz_recordingid':str(mb.get('musicbrainz_recordingid') or row.get('musicbrainz_recordingid') or '')}
            if not values['title'] or (values['title']==str(row.get('title') or '').strip() and values['artist']==str(row.get('artist') or '').strip() and values['album']==str(row.get('album') or '').strip() and not mb.get('musicbrainz_recordingid')): continue
            snap=tag_snapshot(path)
            if snap: backup[str(path)]=snap
            if not write_tags(path,values): continue
            con.execute("update tracks set title=?,artist=?,album=case when ?<>'' then ? else album end,musicbrainz_recordingid=case when ?<>'' then ? else musicbrainz_recordingid end where station_id=? and lower(file_path)=lower(?)",(values['title'],values['artist'],values['album'],values['album'],values['musicbrainz_recordingid'],values['musicbrainz_recordingid'],sid,str(path)))
            changed+=1
            if changed%25==0:
                con.commit(); args.backup.parent.mkdir(parents=True,exist_ok=True); args.backup.write_text(json.dumps(backup,ensure_ascii=False,indent=2),encoding='utf-8')
    con.commit(); args.backup.parent.mkdir(parents=True,exist_ok=True); args.backup.write_text(json.dumps(backup,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'scanned_audio':scanned,'changed':changed,'tag_backup':str(args.backup),'backup_files':len(backup)},ensure_ascii=True)); return 0
if __name__=='__main__': raise SystemExit(main())


