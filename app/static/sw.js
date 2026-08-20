const CACHE_NAME = 'RadioTEDU-OnAir-shell-v24';
const SHELL_ASSETS = [
    '/',
    '/app',
    '/app/',
    '/login.html',
    '/static/onair/styles.css?v=17',
    '/static/onair/app.js?v=40',
    '/static/onair/guest-room.js?v=6',
    '/static/onair/assets/radiotedu-logo.png',
    '/static/onair/assets/radiotedu-onair-logo.png',
    '/static/icons/icon.png',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/manifest.json',
];
const SHELL_ASSET_SET = new Set(SHELL_ASSETS);
const SHELL_CANONICAL_PATHS = new Set(['/', '/app', '/app/', '/login.html']);

function normalizeShellCacheKey(requestUrl) {
    const url = new URL(requestUrl);
    const exactKey = `${url.pathname}${url.search}`;
    if (SHELL_ASSET_SET.has(exactKey)) {
        return exactKey;
    }
    if (SHELL_CANONICAL_PATHS.has(url.pathname)) {
        return url.pathname;
    }
    return exactKey;
}

function isShellAsset(requestUrl) {
    return SHELL_ASSET_SET.has(normalizeShellCacheKey(requestUrl));
}

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(async cache => {
            await cache.addAll(SHELL_ASSETS);
            if (typeof self.skipWaiting === 'function') {
                await self.skipWaiting();
            }
        })
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(async keys => {
            await Promise.all(
                keys
                    .filter(key => (
                        key.startsWith('cleanroom-shell-')
                        || key.startsWith('RadioTEDU-OnAir-')
                        || key.startsWith('radiotedu-broadcast-wall-shell-')
                    ) && key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            );
            if (self.clients && typeof self.clients.claim === 'function') {
                await self.clients.claim();
            }
        })
    );
});

self.addEventListener('fetch', event => {
    const { request } = event;
    if (request.method !== 'GET' || request.url.includes('/api/') || !isShellAsset(request.url)) {
        return;
    }
    const cacheKey = normalizeShellCacheKey(request.url);
    const canonicalNavigation = SHELL_CANONICAL_PATHS.has(new URL(request.url).pathname);
    event.respondWith(
        caches.open(CACHE_NAME).then(async cache => {
            if (canonicalNavigation) {
                try {
                    const response = await fetch(request);
                    if (response && response.ok) {
                        await cache.put(cacheKey, response.clone());
                    }
                    return response;
                } catch (error) {
                    const fallback = await cache.match(cacheKey);
                    if (fallback) {
                        return fallback;
                    }
                    throw error;
                }
            }
            try {
                const response = await fetch(request);
                if (response && response.ok) {
                    await cache.put(cacheKey, response.clone());
                }
                return response;
            } catch (error) {
                const hit = await cache.match(cacheKey);
                if (hit) {
                    return hit;
                }
                throw error;
            }
        })
    );
});
