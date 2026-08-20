const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const runtimeIndicator = require(path.resolve(
    __dirname,
    '..',
    '..',
    'app',
    'static',
    'js',
    'runtime-indicator.js',
));

test('keeps engine alive during a brief false poll after a true sample', () => {
    let state = runtimeIndicator.createRuntimeIndicatorState();

    state = runtimeIndicator.applyRuntimeIndicatorSample(state, {
        stationId: 7,
        observedAlive: true,
        nowMs: 1_000,
        graceMs: 4_500,
    });
    assert.equal(state.displayAlive, true);

    state = runtimeIndicator.applyRuntimeIndicatorSample(state, {
        stationId: 7,
        observedAlive: false,
        nowMs: 2_000,
        graceMs: 4_500,
    });
    assert.equal(state.displayAlive, true);
});

test('drops engine alive after the grace window expires', () => {
    let state = runtimeIndicator.createRuntimeIndicatorState();

    state = runtimeIndicator.applyRuntimeIndicatorSample(state, {
        stationId: 7,
        observedAlive: true,
        nowMs: 1_000,
        graceMs: 4_500,
    });
    state = runtimeIndicator.applyRuntimeIndicatorSample(state, {
        stationId: 7,
        observedAlive: false,
        nowMs: 6_000,
        graceMs: 4_500,
    });

    assert.equal(state.displayAlive, false);
});

test('resets grace state when station changes', () => {
    let state = runtimeIndicator.createRuntimeIndicatorState();

    state = runtimeIndicator.applyRuntimeIndicatorSample(state, {
        stationId: 3,
        observedAlive: true,
        nowMs: 1_000,
        graceMs: 4_500,
    });
    state = runtimeIndicator.applyRuntimeIndicatorSample(state, {
        stationId: 7,
        observedAlive: false,
        nowMs: 1_100,
        graceMs: 4_500,
    });

    assert.equal(state.displayAlive, false);
    assert.equal(state.stationId, 7);
});
