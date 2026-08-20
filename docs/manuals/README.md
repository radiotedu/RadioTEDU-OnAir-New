# Broadcast Console Manuals

Two print-ready A4 manuals are generated from `generate_manuals.py`:

- `RadioTEDU-OnAir-Worker-Operations-Manual.pdf` is the internal worker edition. It uses verified RadioTEDU OnAir screenshots and covers station selection, start/stop continuity, managed media, queue control, adjustable jingles, emergency tab-audio takeover, optional services, settings, diagnostics, shift handover, and incident response.
- `Deterministic-Broadcast-Console-User-Manual.pdf` is the brand-neutral public edition. Its interface figures are drawn specifically for the manual and contain no RadioTEDU branding, station names, mounts, URLs, private addresses, credentials, or named internal services.

## Build

From the repository root:

```powershell
python docs\manuals\generate_manuals.py
```

Final PDFs are written to `output\pdf`.

The generator validates page counts, extractable text, minimum file integrity, and a forbidden-term list for the public edition. Final releases must also be rendered to PNG and visually inspected page by page.
