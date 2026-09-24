# RadioTEDU OnAir çalma geçmişi ve gece yedeği

Her müzik/jingle parçası tamamlandığında istasyon, UTC zamanı, sanatçı, şarkı
adı, dosya yolu, program/sunucu, mount'lar ve oynatma sayısı
`music_usage_log` içindeki append-only/hash-chain kayda yazılır. CSV'ler bu
kalıcı kaydın masaüstü kopyasıdır; yayın akışının sahibi değildir.

Varsayılan klasör:

```text
%USERPROFILE%\Desktop\RadioTEDU Play History
```

İçerik:

```text
RadioTEDU-play-history-daily.csv       # geçerli UTC gününün parçaları
RadioTEDU-play-counts-daily.csv        # geçerli gün, parça başına sayım
RadioTEDU-play-history-total.csv       # bütün radyoların tüm geçmişi
RadioTEDU-play-counts-total.csv        # tamamlanan olay ve toplam oynatma
RadioTEDU-play-history-manifest.json   # checksum + hash doğrulaması
daily\YYYY-MM-DD-*.csv                # gün arşivi
legacy\                                # önceki ProgramData raporları
```

CSV'deki `station_name`, `stream_mounts`, `song_title` ve `artist` alanları
insan tarafından okunabilir rapor içindir. Eski `ProgramData\RadioTEDU\OnAir\Exports\MusicUsage`
dosyaları `legacy` altında korunur; ledger satırları yeniden yazılmaz.

## Otomatik görevler

- `RadioTEDU-OnAir-PlayHistory-Export`: beş dakikada bir CSV'leri yeniler.
- `RadioTEDU-OnAir-PlayHistory-GitHub`: her gece exporter'ı çalıştırır, yerel
  Git mirror'ında commit oluşturur ve özel
  [`radiotedu/RadioTEDU-OnAir-Play-History`](https://github.com/radiotedu/RadioTEDU-OnAir-Play-History)
  deposuna push eder.

Kontrol:

```powershell
schtasks /Query /TN RadioTEDU-OnAir-PlayHistory-Export /FO LIST
schtasks /Query /TN RadioTEDU-OnAir-PlayHistory-GitHub /FO LIST
```

Manuel yenileme ve yedek:

```powershell
py -3 .\scripts\export_play_history.py `
  --db-path 'C:\ProgramData\RadioTEDU\OnAir\cleanroom.db'
powershell -ExecutionPolicy Bypass -File .\scripts\backup_play_history_to_github.ps1
```

GitHub'a yalnızca CSV/JSON raporları gider. Icecast/TinyIce parolaları, JWT,
credential vault, SQLite veritabanı, medya ve FFmpeg süreçleri dışarı
çıkarılmaz. `gh auth status` komutunda `radiotedu` hesabı etkin olmalıdır.

Manifest'teki `integrity.valid` değeri `true` ve `record_count` toplam olay
sayısı olmalıdır. Doğrulama hatasında dosyaları değiştirmeyin; son GitHub
commit'ini koruyup SQLite çevrim-içi yedeğiyle inceleme yapın. Bu kontrol yayın
süreçlerini durdurmaz.
