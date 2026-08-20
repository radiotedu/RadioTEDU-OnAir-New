# RadioTEDU OnAir release-hardening checklist

This checklist is the release authority for the deterministic, mouse-operated
RadioTEDU OnAir desktop application. A release is publishable only when every
required gate below is checked and its evidence is reproducible.

## Safety boundaries

- [x] Keep the production broadcast restricted to the approved `/lofi` mount.
- [x] Perform destructive, emergency, start, stop, and settings tests only
  against an isolated database and backend.
- [x] Require a deliberate second click for stream stop and emergency takeover.
- [x] Preserve queue order and the interrupted item across operator stop/resume.
- [x] Keep AI, voting, Juke, and other optional services unable to control the
  core broadcast or prevent music continuity.
- [x] Keep credentials, source passwords, tokens, and protected configuration
  out of source control, logs, screenshots, and release notes.

## Operator control and UI

- [x] Provide dedicated On Air, Media, Automation, Emergency, Services,
  Settings, and Diagnostics menus.
- [x] Make every normal broadcast operation reachable with a mouse.
- [x] Verify station selection, menu navigation, scrolling, buttons, forms,
  checkboxes, folder browsing, file browsing, and confirmation controls.
- [x] Verify Start/resume from the On Air view.
- [x] Verify Stop stream with two mouse clicks and confirm that the playlist is
  retained.
- [x] Verify emergency takeover requires an explicit arm and confirmation.
- [x] Verify the emergency source is an operator-selected public-service page,
  not the `/lofi` mount.
- [x] Verify the jingle interval is operator-adjustable from 1 to 100 songs,
  with 2 as the default and 3 supported.
- [x] Remove horizontal page overflow caused by the invisible file input while
  keeping the native file picker mouse-operable.
- [x] Mark unavailable optional readiness checks as optional instead of
  presenting them as required failures.
- [x] Keep disabled controls visibly disabled and provide an explicit result,
  activity entry, or toast after consequential actions.

## Runtime and broadcast durability

- [x] Confirm runtime stop terminates scheduler and output workers without
  clearing or advancing the queue.
- [x] Confirm startup and stop mutations use read-back verification.
- [x] Prevent unread FFmpeg stderr pipes from blocking long-running streams.
- [x] Add bounded metadata retry backoff so transient Icecast admin failures do
  not create a retry storm.
- [x] Preserve immediate metadata delivery attempts when the track changes.
- [x] Confirm the public station status checks the actual Icecast mount and
  does not report a false live state from process health alone.
- [x] Add cache and failure hysteresis to the public mount probe.
- [x] Honor an explicit shared data root in both source and packaged operation.
- [x] Preserve a valid managed dependency when Windows temporarily denies an
  executable replacement during bootstrap.

## Media, automation, and optional systems

- [x] Verify managed music-folder and jingle-folder controls are visible and
  mouse-operable.
- [x] Verify exact replacement, recursive scan, and unreadable-file policies
  remain operator choices.
- [x] Verify the queue, search, paging, and deterministic jingle controls are
  present in the operator console.
- [x] Verify Ollama, shared AI, AI radio, voting agent, voting web backend, Juke
  agent, and Juke web backend controls expose health, start/stop/restart,
  repository update, and database update actions where applicable.
- [x] Keep every optional service disabled by default in a fresh isolated
  installation.
- [x] Keep normal broadcasting independent of AI and optional-service health.

## Automated release gates

- [x] Focused backend/runtime/UI hardening tests pass.
- [x] Browser-script tests pass: 118/118.
- [x] Final full Python suite passes after the last source change: 804 passed,
  3 skipped, and 3 subtests.
- [x] Packaged backend smoke test passes.
- [ ] Installer build completes from the exact release commit.
- [ ] Installer install/launch/uninstall smoke test passes in isolation.
- [ ] Installer SHA-256 is recorded and published.

## Live and mouse commissioning gates

- [x] Isolated mouse login succeeds.
- [x] All seven operator menus open by mouse.
- [x] Native music-folder picker opens from the Media menu.
- [x] Native jingle-file picker opens and cancels by mouse after the overflow
  fix.
- [x] Emergency takeover arms by mouse without being confirmed on production.
- [x] Diagnostics self-check runs by mouse.
- [x] Isolated Start and two-click Stop both verify successfully.
- [ ] Recheck the production public status and listener response for `/lofi`.
- [ ] Confirm no unapproved production mount is live.
- [ ] Confirm the production backend and Ollama health after deployment.

## Publication gate

- [x] Review the final diff for secrets and unintended files.
- [ ] Commit the exact tested source state.
- [ ] Build artifacts from that commit without source changes afterward.
- [ ] Publish the installer with an explicit **RadioTEDU** label.
- [ ] Update the GitHub release and checksum asset.
- [ ] Verify the GitHub tag, commit, installer asset, and checksum agree.
- [ ] Record that the Windows installer is unsigned unless a trusted
  code-signing certificate is applied.
