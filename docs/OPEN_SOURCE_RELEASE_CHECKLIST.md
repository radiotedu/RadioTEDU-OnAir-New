# Open-source release checklist

RadioTEDU OnAir is being engineered so an operator can install, configure,
test, and recover it without an AI assistant. The source is **not yet licensed
for public redistribution**: `LICENSE.md` remains proprietary until TED
University approves and publishes an open-source license. Do not label or push
the application payload as open source before the legal items below are
complete.

## 1. Legal and identity decisions

- [ ] TED University selects and approves the application license (for example
  Apache-2.0, GPL-3.0-only, or another reviewed license) and replaces the root
  proprietary `LICENSE.md` with the exact approved text.
- [ ] Confirm whether the RadioTEDU, TED University, and product artwork
  may be redistributed. Open-source copyright permission does not itself grant
  trademark rights. If redistribution is restricted, replace branded assets in
  the public tree with neutral defaults and document downstream branding.
- [ ] Establish contributor terms (DCO or CLA), contribution guidelines, and a
  code of conduct after the application license is chosen.
- [ ] Confirm that example music, jingles, voices, models, screenshots, and
  station configuration are either excluded or independently redistributable.

## 2. Dependency and supply-chain evidence

- [x] `requirements.txt` is the reviewed direct-dependency input.
- [x] `requirements.lock` pins the exact Windows runtime/test graph used by CI,
  installer builds, and backend packaging.
- [x] PyInstaller and its build helpers are version-pinned by the backend build.
- [x] CI rejects JavaScript syntax errors and runs the complete Python,
  JavaScript, and desktop regression gates.
- [ ] Generate a root third-party notice/SBOM from the final release lock,
  including complete license texts and the separately shipped FFmpeg, WebView2,
  .NET, Ollama, model, and installer payload obligations.
- [ ] Add artifact signing and publish SHA-256/SBOM/provenance beside every
  installer release.

## 3. Configuration and secret boundary

- [x] The tracked-file credential scan finds no private keys, AWS keys, GitHub
  tokens, OpenAI keys, JWTs, `.env` files, PFX/P12 files, or key files.
- [x] Production source credentials live outside the repository and use the
  machine-scoped Windows credential vault.
- [ ] Replace institution-specific hosts, private IP addresses, usernames,
  commissioning reports, and local absolute paths in the public branch with
  clearly marked examples or private deployment overlays.
- [ ] Run an independent history scan before publication; scanning the current
  tree does not prove that older Git objects contain no secret.

## 4. No-AI operator requirement

- [x] AI and TTS are optional station-scoped services and are disabled for Lo-Fi.
- [x] Core startup, health, library, queue, jingle, output recovery, and public
  truth do not instantiate or require the AI host when AI is disabled.
- [x] The installer presents Ollama as an unchecked optional runtime.
- [x] Operator actions use visible controls, read-back verification, and
  actionable errors rather than command-line or prompt-dependent workflows.
- [ ] Validate the final installer on a clean Windows VM using only the written
  operator guide, with no developer tools or AI assistant present.

## 5. Broadcast release gate

- [x] One active queue owner is enforced transactionally; duplicate active
  dedupe keys are rejected by SQLite.
- [x] Jingle completion is at-most-once across the one-second scheduler polling
  boundary.
- [x] A blocked remote Icecast encoder cannot block program decoding; bounded
  PCM buffering, heartbeat/stall detection, and supervisor replacement are in
  place.
- [x] The UI cannot report live from a process or database row alone; listener
  media bytes are required.
- [ ] Prove the chosen public mount continuously delivers media bytes for the
  release soak period. A connected source socket or HTTP 200 header is not
  sufficient.
- [ ] Exercise Start, Stop, library selection, stream configuration, emergency
  takeover, jingle policy, queue reorder, and restart recovery in the final
  signed installer on a clean VM.

## Publication decision

Publication is permitted only when every unchecked legal, security,
redistribution, clean-machine, and end-to-end broadcast gate above has evidence
attached to the release. Passing unit tests alone is not sufficient.
