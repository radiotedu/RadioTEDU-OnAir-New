"""Back up then remove non-audio files from configured music libraries."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sqlite3, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.file_security import audio_upload_extensions

DB=Path(os.environ.get('CLEANROOM_DB_PATH',r'C:\ProgramData\RadioTEDU\OnAir\cleanroom.db'))
AUDIO=set(audio_upload_extensions())|{'.wma'}
COVER_ART={'.jpg','.jpeg','.png','.webp','.gif','.bmp','.tif','.tiff'}

def sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def roots()->list[tuple[int,Path]]:
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
    try:
        return [(int(r['station_id']),Path(str(r['value']))) for r in con.execute("select station_id,value from station_settings where key='music_library_folder' and trim(value)<>'' order by station_id")]
    finally: con.close()

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--backup-dir',type=Path,required=True); args=ap.parse_args()
    found=[]
    for sid,root in roots():
        if not root.exists(): continue
        for p in root.rglob('*'):
            if p.is_file() and p.suffix.lower() not in AUDIO and p.suffix.lower() not in COVER_ART:
                found.append((sid,root,p))
    print(json.dumps({'candidate_count':len(found),'dry_run':args.dry_run,'samples':[str(x[2]) for x in found[:25]]},ensure_ascii=True))
    if args.dry_run: return 0
    args.backup_dir.mkdir(parents=True,exist_ok=False)
    manifest=[]
    for sid,root,p in found:
        rel=Path(f'station-{sid}') / p.relative_to(root)
        target=args.backup_dir / rel
        target.parent.mkdir(parents=True,exist_ok=True)
        before=sha256(p); shutil.copy2(p,target)
        if sha256(target)!=before: raise RuntimeError(f'backup verification failed: {p}')
        p.unlink()
        manifest.append({'station_id':sid,'original':str(p),'backup':str(target),'bytes':target.stat().st_size,'sha256':before})
    (args.backup_dir/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'removed_count':len(manifest),'backup_dir':str(args.backup_dir)},ensure_ascii=True))
    return 0

if __name__=='__main__': raise SystemExit(main())


