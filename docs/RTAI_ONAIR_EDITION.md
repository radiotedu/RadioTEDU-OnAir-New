# rtAI OnAir edition

`rtAI OnAir` is the white-label edition for other radio stations. It reuses the
same deterministic playout, local AI, native folder pickers, scheduling,
streaming, diagnostics, and verified recovery code as RadioTEDU OnAir.

It deliberately excludes the RadioTEDU voting campaign, external RadioTEDU
adapters, Juke service controls, product-media catalog, and the fixed RadioTEDU
16-mount quality plan. Those API families return `404 feature_not_in_edition`,
their background services do not start, and their operator panels are hidden.

Build the deterministic source release by double-clicking
`BUILD-rtAI-onair.bat`. The result is written to `dist\editions` as both a
directory and a SHA-256-manifested ZIP. Building the edition never installs or
starts it, so it cannot compete with this PC's RadioTEDU broadcast.

For a runtime, install the normal bundled dependencies in the release directory
or build its existing Windows desktop bundle. Start it with
`START-rtAI-onair.bat`; that launcher selects the `rtai-onair` profile before
the backend starts. All routine station and folder operations remain available
through the UI.
