"""Assemble tested native POSIX packages, including runtime and codec tools."""
import argparse,json,os,platform,shutil,subprocess,tarfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--tools',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1];version=(root/'VERSION').read_text().strip();commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip();osname=platform.system();arch=platform.machine();out=root/'release';out.mkdir(exist_ok=True)
stage=out/('RadioTEDU-OnAir-'+version+'-'+osname+'-'+arch);stage.mkdir(exist_ok=False);app=stage/'RadioTEDU-OnAir';shutil.copytree(root/'dist'/'RadioTEDU-OnAir',app)
(app/'tools'/'bin').mkdir(parents=True)
for name in ['ffmpeg','ffprobe']:shutil.copy2(a.tools/'bin'/name,app/'tools/bin'/name)
licenses=stage/'ThirdPartyLicenses';licenses.mkdir()
for label,name in [('fdk','NOTICE'),('openssl','LICENSE.txt'),('ffmpeg','COPYING.LGPLv2.1'),('ffmpeg','LICENSE.md')]:
    f=root/'.codec-build'/label/name
    if f.exists():shutil.copy2(f,licenses/(label+'-'+name))
start=r'''#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
export RADIOTEDU_FFMPEG_PATH="$HERE/tools/bin/ffmpeg"
export RADIOTEDU_FFPROBE_PATH="$HERE/tools/bin/ffprobe"
exec "$HERE/RadioTEDU-OnAir" "$@"
'''
(app/'start-onair.sh').write_text(start);(app/'start-onair.sh').chmod(0o755)
install=r'''#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EXPECTED_OS=__OS__
EXPECTED_ARCH=__ARCH__
if [ "$(uname -s)" != "$EXPECTED_OS" ] || [ "$(uname -m)" != "$EXPECTED_ARCH" ]; then
  echo "This package is for $EXPECTED_OS $EXPECTED_ARCH."; exit 2
fi
if [ "$EXPECTED_OS" = Darwin ]; then BASE="$HOME/Applications/RadioTEDU-OnAir"; else BASE="$HOME/.local/opt/RadioTEDU-OnAir"; fi
TARGET="$BASE/versions/__VERSION__-__COMMIT__"
mkdir -p "$BASE/versions"
TEMP=
if [ ! -d "$TARGET" ]; then
  TEMP="$TARGET.partial.$$"
  trap 'if [ -n "$TEMP" ]; then rm -rf -- "$TEMP"; fi' EXIT HUP INT TERM
  mkdir "$TEMP"
  cp -R "$HERE/RadioTEDU-OnAir/." "$TEMP/"
  mv "$TEMP" "$TARGET"
  TEMP=
fi
if [ "$EXPECTED_OS" = Darwin ]; then
  APP="$HOME/Applications/RadioTEDU OnAir.app"
  mkdir -p "$APP/Contents/MacOS"
  printf '#!/bin/sh\nexec "%s/start-onair.sh" "$@"\n' "$TARGET" > "$APP/Contents/MacOS/RadioTEDU-OnAir"
  chmod 755 "$APP/Contents/MacOS/RadioTEDU-OnAir"
  cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleExecutable</key><string>RadioTEDU-OnAir</string><key>CFBundleIdentifier</key><string>com.radiotedu.onair</string><key>CFBundleName</key><string>RadioTEDU OnAir</string><key>CFBundleVersion</key><string>__VERSION__</string><key>LSUIElement</key><true/></dict></plist>
PLIST
else
  mkdir -p "$HOME/.local/share/applications"
  printf '[Desktop Entry]\nType=Application\nName=RadioTEDU OnAir\nExec="%s/start-onair.sh"\nTerminal=false\nCategories=Audio;\n' "$TARGET" > "$HOME/.local/share/applications/radiotedu-onair.desktop"
fi
printf '\nInstalled: %s\nStart: "%s/start-onair.sh"\nPanel: http://127.0.0.1:18110/app\n' "$TARGET" "$TARGET"
'''.replace('__OS__',osname).replace('__ARCH__',arch).replace('__VERSION__',version).replace('__COMMIT__',commit[:7])
installer=stage/('Kur.command' if osname=='Darwin' else 'Kur.sh');installer.write_text(install);installer.chmod(0o755)
subprocess.run(['sh','-n',str(installer)],check=True);subprocess.run(['sh','-n',str(app/'start-onair.sh')],check=True)
subprocess.run([str(app/'start-onair.sh'),'--check'],check=True)
subprocess.run(['python',str(root/'installer/verify_portable_startup.py'),str(app/'RadioTEDU-OnAir')],check=True,env={**os.environ,'RADIOTEDU_FFMPEG_PATH':str(app/'tools/bin/ffmpeg'),'RADIOTEDU_FFPROBE_PATH':str(app/'tools/bin/ffprobe')})
# Exercise the actual installer without launching sources; HOME is isolated.
checkhome=root/'.installer-check-home';checkhome.mkdir()
subprocess.run(['sh',str(installer)],check=True,env={**os.environ,'HOME':str(checkhome)})
if osname=='Darwin':
    subprocess.run(['sh','-n',str(checkhome/'Applications/RadioTEDU OnAir.app/Contents/MacOS/RadioTEDU-OnAir')],check=True)
(stage/'ONCE_BUNU_OKU.txt').write_text('RadioTEDU OnAir '+version+' — '+osname+' '+arch+'\n\nKurulum: '+installer.name+'. Linux: sh Kur.sh. macOS: Kur.command dosyasına çift tıklayın. Python, FFmpeg/ffprobe ve FDK-AAC pakete dahildir. Şarkılar, yayın hesapları ve mevcut veritabanı dahil değildir.\nPanel: http://127.0.0.1:18110/app\nİlk yönetici parolası yeni veri dizinindeki initial-admin-password.txt dosyasındadır.\nLinux veri: ~/.local/share/radiotedu/onair. macOS veri: ~/Library/Application Support/RadioTEDU/OnAir.\nmacOS paketi geliştirici sertifikasıyla imzalanmamıştır. Linux paketi Ubuntu 24.04/glibc 2.39 veya uyumlu yeni sistemler içindir.\nYeni bilgisayarda kanal ve medya klasörlerini ayarlayın; mevcut mounta ikinci yayıncı açmadan önce kontrollü devir yapın.\n',encoding='utf-8')
meta={'version':version,'commit':commit,'os':osname,'architecture':arch,'python_bundled':True,'ffmpeg_bundled':True,'fdk_aac_bundled':True,'isolated_http_startup':'passed','installer_test':'passed','source_application_commit':'a59d1d867094f63c1a087182bc361a7a5bd00c01','build_only_changes_after_application_commit':True}
(stage/'PAKET_DOGRULAMA.json').write_text(json.dumps(meta,indent=2))
with tarfile.open(str(stage)+'.tar.gz','w:gz') as t:t.add(stage,arcname=stage.name)
print(json.dumps({'archive':str(stage)+'.tar.gz',**meta}))
