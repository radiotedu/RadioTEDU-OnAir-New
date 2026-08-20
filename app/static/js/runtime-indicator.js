(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    root.CleanroomRuntimeIndicator = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const DEFAULT_ENGINE_ALIVE_GRACE_MS = 2000;

    function createRuntimeIndicatorState() {
        return {
            stationId: null,
            lastAliveAtMs: 0,
            displayAlive: false,
            lastObservedAlive: null,
        };
    }

    function normalizeStationId(rawStationId) {
        const stationId = Number(rawStationId);
        if (!Number.isFinite(stationId) || stationId <= 0) {
            return null;
        }
        return Math.trunc(stationId);
    }

    function applyRuntimeIndicatorSample(
        previousState,
        {
            stationId = null,
            observedAlive = null,
            nowMs = Date.now(),
            graceMs = DEFAULT_ENGINE_ALIVE_GRACE_MS,
        } = {},
    ) {
        const nextState = {
            ...createRuntimeIndicatorState(),
            ...(previousState && typeof previousState === 'object' ? previousState : {}),
        };
        const normalizedStationId = normalizeStationId(stationId);
        const normalizedNowMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
        const normalizedGraceMs = Math.max(0, Number(graceMs) || 0);

        if (nextState.stationId !== normalizedStationId) {
            nextState.stationId = normalizedStationId;
            nextState.lastAliveAtMs = 0;
            nextState.displayAlive = false;
            nextState.lastObservedAlive = null;
        }

        nextState.lastObservedAlive = observedAlive;
        if (observedAlive === true) {
            nextState.lastAliveAtMs = normalizedNowMs;
        }

        const withinGrace = nextState.lastAliveAtMs > 0
            && (normalizedNowMs - nextState.lastAliveAtMs) <= normalizedGraceMs;
        nextState.displayAlive = observedAlive === true || withinGrace;
        return nextState;
    }

    return {
        DEFAULT_ENGINE_ALIVE_GRACE_MS,
        createRuntimeIndicatorState,
        applyRuntimeIndicatorSample,
    };
}));
