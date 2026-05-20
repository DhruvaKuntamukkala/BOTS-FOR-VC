"""
speaker_hook.py
───────────────
A JavaScript snippet injected via add_init_script() BEFORE the meeting page
loads.  It uses a MutationObserver to watch the entire DOM for changes that
indicate who is speaking — without relying on any platform-specific CSS class
names or data attributes that change with every deploy.

Three complementary signals are watched simultaneously:

  1. aria-label changes  — WCAG-compliant UIs mark the active speaker with an
     aria-label that contains the word "speaking". This is the most reliable
     cross-platform signal (Meet, Zoom, Teams, Webex all use it).

  2. aria-live regions   — Live captions / transcripts use aria-live="polite"
     or aria-live="assertive". When a new caption line fires, the first
     "short" text node in that block is typically the speaker's name.

  3. Name-match scan     — Once participant names are known (populated via
     window._mindxSpeaker.setNames([...])) we scan the DOM every second for
     those names appearing near audio/video indicators.

Usage (from Python):
    await page.add_init_script(SPEAKER_HOOK_JS)  # before goto()
    ...
    # After participants list is known:
    await page.evaluate(f"window._mindxSpeaker.setNames({json.dumps(names)})")
    ...
    # Drain events periodically:
    events = await page.evaluate("window._mindxSpeaker.drain()")
    # events = [{name, start, end}, ...]
    ...
    # On stop, flush the current open interval:
    events = await page.evaluate("window._mindxSpeaker.flush()")
"""

SPEAKER_HOOK_JS = r"""
(function () {
    if (window._mindxSpeaker) return; // already injected

    const _state = {
        names: [],            // participant display names (set after join)
        active: {},           // Object mapping name -> { start, lastSeen }
        events: [],           // completed intervals [{name,start,end}]
        _meetingStart: Date.now() / 1000,
        running: false,       // Don't track anything during the lobby preview
        platform: 'generic',  // 'meet' | 'zoom' | 'teams' | 'webex' | 'zoho' | 'generic'
    };

    window._mindxSpeaker = {
        setNames:   function(names) { _state.names = names || []; },
        setPlatform: function(p) { _state.platform = p || 'generic'; },
        startTracking: function(elapsedAudio) {
            // Clears any lobby garbage and aligns timelines perfectly
            _state.events = [];
            _state.active = {};
            _state._meetingStart = (Date.now() / 1000) - elapsedAudio;
            _state.running = true;
        },
        drain:      function() { return _state.events.splice(0); },
        getCurrent: function() { return Object.keys(_state.active).join(', '); },
        flush:      function() {
            if (!_state.running) return [];
            const now = Date.now() / 1000;
            for (const name of Object.keys(_state.active)) {
                const info = _state.active[name];
                _state.events.push({
                    name:  name,
                    start: round2(info.start - _state._meetingStart),
                    end:   round2(info.lastSeen - _state._meetingStart + 0.5),
                });
            }
            _state.active = {};
            return _state.events.splice(0);
        },
    };

    function round2(n) { return Math.round(n * 100) / 100; }

    function recordSpeaker(name) {
        if (!_state.running) return;
        if (!name || typeof name !== 'string') return;
        name = name.trim();
        if (!name || name.length < 2 || name.length > 80) return;

        // Reject raw Material Icon ligatures (all lowercase with optional underscores)
        if (/^[a-z_]+$/.test(name)) return;

        const now = Date.now() / 1000;
        if (_state.active[name]) {
            _state.active[name].lastSeen = now;
        } else {
            _state.active[name] = { start: now, lastSeen: now };
        }
    }

    function checkSilence() {
        if (!_state.running) return;
        const now = Date.now() / 1000;
        for (const name of Object.keys(_state.active)) {
            if (now - _state.active[name].lastSeen > 2.0) {
                const info = _state.active[name];
                _state.events.push({
                    name:  name,
                    start: round2(info.start - _state._meetingStart),
                    end:   round2(info.lastSeen - _state._meetingStart + 0.5),
                });
                delete _state.active[name];
            }
        }
    }

    // ── Signal 1: aria-label scan ─────────────────────────────────────────
    const SPEAKING_RE = /^(.+?)\s*,?\s*is\s+speaking/i;
    const SPEAKING_RE2 = /^(.+?),\s*speaking\s*$/i;

    function scanAriaLabels(root) {
        if (!_state.running) return false;
        let found = false;
        const els = root.querySelectorAll('[aria-label]');
        for (const el of els) {
            const lbl = el.getAttribute('aria-label') || '';
            let m = SPEAKING_RE.exec(lbl) || SPEAKING_RE2.exec(lbl);
            if (m) { recordSpeaker(m[1]); found = true; }
        }
        // Also walk shadow DOM one level
        root.querySelectorAll('*').forEach(el => {
            if (el.shadowRoot) {
                if (scanAriaLabels(el.shadowRoot)) found = true;
            }
        });
        return found;
    }

    // ── Signal 2: aria-live & visual caption regions ─────────────────────
    function handleLiveRegion(node) {
        if (!_state.running) return;
        let root = node;
        let isCaption = false;
        for (let i = 0; i < 6; i++) {
            if (!root || !root.getAttribute) break;
            if (root.getAttribute('aria-live')) { isCaption = true; break; }
            const cls = (root.getAttribute('class') || '').toLowerCase();
            const jsname = root.getAttribute('jsname') || '';
            if (cls.includes('caption') || cls.includes('a4cqt') || jsname === 'dsss6e') {
                isCaption = true; break;
            }
            root = root.parentElement;
        }
        if (isCaption) {
            parseCaptionBlock(root);
        }
    }

    function parseCaptionBlock(liveRoot) {
        if (!_state.running) return;
        if (_state.names.length === 0) return;
        
        // Scan the entire mutated caption root block's text.
        // Google Meet actively pushes new spans/divs into this root while speaking.
        const t = (liveRoot.innerText || liveRoot.textContent || '').trim();
        if (t.length < 2) return;

        for (const n of _state.names) {
            if (t.includes(n)) {
                // Determine if this is an actual active utterance (length grows)
                recordSpeaker(n);
                return;
            }
        }
    }

    // ── MutationObserver ─────────────────────────────────────────────────
    const observer = new MutationObserver(mutations => {
        if (!_state.running) return;
        let hasLiveChange = false;
        for (const m of mutations) {
            if (m.type === 'attributes') {
                const attr = m.attributeName || '';
                if (attr === 'aria-label') {
                    const lbl = m.target.getAttribute('aria-label') || '';
                    const match = SPEAKING_RE.exec(lbl) || SPEAKING_RE2.exec(lbl);
                    if (match) { recordSpeaker(match[1]); }
                }
                if (attr === 'aria-live') hasLiveChange = true;
            }
            if (m.type === 'childList') {
                handleLiveRegion(m.target);
                // Reactively catch mute-state overlay buttons added to Meet tiles
                if (_state.platform === 'meet') {
                    checkMeetMuteButtonsInNodes(m.addedNodes);
                }
            }
        }
        if (hasLiveChange) {
            try { scanAriaLabels(document); } catch(e) {}
        }
    });

    function startObserver() {
        try {
            observer.observe(document.body || document.documentElement, {
                subtree: true,
                attributes: true,
                attributeFilter: ['aria-label', 'aria-live', 'data-is-muted', 'data-speaking'],
                childList: true,
            });
        } catch(e) {}
    }

    if (document.body) startObserver();
    else document.addEventListener('DOMContentLoaded', startObserver);

    // ── Polling (every 1 s) — strategy depends on platform ───────────────
    setInterval(function() {
        if (!_state.running) return;
        try {
            if (_state.platform === 'meet') {
                // Dispatch hover on tiles NOW so Meet renders tile overlays.
                // The actual scan is deferred 300 ms because Meet renders
                // overlay buttons asynchronously after the mouseover event.
                try {
                    document.querySelectorAll('[data-participant-id]').forEach(function(tile) {
                        tile.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true }));
                    });
                } catch(e) {}
                setTimeout(function() {
                    if (!_state.running) return;
                    let muteFound = false;
                    try { muteFound = scanMeetMuteButtons(); } catch(e) {}
                    if (!muteFound) { try { scanAriaLabels(document); } catch(e) {} }
                }, 300);
            } else if (_state.platform === 'zoom') {
                // Zoom: aria-label "is speaking" primary, audio-level-indicator secondary
                let found = false;
                try { found = scanAriaLabels(document); } catch(e) {}
                if (!found) { try { scanZoomActiveSpeaker(); } catch(e) {} }
            } else {
                // Teams / Webex / Zoho / generic: aria-label "is speaking" only
                try { scanAriaLabels(document); } catch(e) {}
            }
        } catch(e) {}
        checkSilence();
    }, 1000);

    // ── Signal 4: audio-level-indicator → tile name (Zoom) ───────────────
    function scanZoomActiveSpeaker() {
        if (!_state.running) return false;
        let found = false;
        const indicators = document.querySelectorAll('[class*="audio-level-indicator"]');
        for (const indicator of indicators) {
            let el = indicator.parentElement;
            for (let i = 0; i < 10; i++) {
                if (!el || el === document.body) break;
                const nameEl = el.querySelector(
                    '[class*="video-avatar__avatar-name"],[class*="video-avatar__avatar-title"]'
                );
                if (nameEl) {
                    const name = (nameEl.innerText || nameEl.textContent || '').split('\n')[0].trim();
                    if (name && name.length > 1 && name.length < 80 && !name.startsWith('(')) {
                        if (_state.names.length === 0 || _state.names.indexOf(name) !== -1) {
                            recordSpeaker(name);
                            found = true;
                        }
                        break; // Stop looking up for this indicator
                    }
                }
                el = el.parentElement;
            }
        }
        return found;
    }

    // ── Signal 5: Open Microphone (Google Meet) ──────────────────────────
    //
    // Two entry points:
    //   checkMeetMuteButtonsInNodes  — called from MutationObserver (reactive)
    //   scanMeetMuteButtons          — called from 300ms deferred poll (fallback)
    //
    // Hover dispatch is handled SEPARATELY in the 1s polling interval so that
    // the overlay has time to render before we scan.

    function _extractMuteButtonName(btn) {
        const lbl = btn.getAttribute(‘aria-label’) || ‘’;
        const match = lbl.match(/can’t mute (.+?)’s microphone remotely/i);
        if (!match) return null;
        const name = match[1].trim();
        if (_state.names.length > 0 && _state.names.indexOf(name) === -1) return null;
        return name;
    }

    // Called by MutationObserver when new nodes are added to the DOM.
    // Catches the mute-state overlay button the moment Meet injects it.
    function checkMeetMuteButtonsInNodes(addedNodes) {
        if (!_state.running) return;
        for (const node of addedNodes) {
            if (node.nodeType !== 1) continue;
            const candidates = node.tagName === ‘BUTTON’ ? [node] : [];
            if (node.querySelectorAll) {
                node.querySelectorAll(‘button[aria-label]’).forEach(function(b) { candidates.push(b); });
            }
            for (const btn of candidates) {
                const name = _extractMuteButtonName(btn);
                if (name) recordSpeaker(name);
            }
        }
    }

    // Called ~300ms after hover dispatch so the overlay has rendered.
    function scanMeetMuteButtons() {
        if (!_state.running) return false;
        let found = false;

        // Strategy A: Equalizer container — injected when mic is unmuted.
        const equalizers = document.querySelectorAll(‘[class*="JsqLM"], [class*="IisKdb"], [jsname="ptYiWe"]’);
        for (const eq of equalizers) {
            const style = window.getComputedStyle(eq);
            if (style.display === ‘none’ || style.visibility === ‘hidden’) continue;
            let p = eq.parentElement;
            for (let i = 0; i < 8; i++) {
                if (!p || p === document.body) break;
                const nameEls = p.querySelectorAll(‘[data-participant-id], [class*="zWfAFd"], [jsname="O0mBGb"], [class*="name"]’);
                for (const nameEl of nameEls) {
                    const t = (nameEl.innerText || nameEl.textContent || ‘’).split(‘\n’)[0].trim();
                    if (t && t.length > 1 && (_state.names.length === 0 || _state.names.indexOf(t) !== -1)) {
                        recordSpeaker(t);
                        found = true;
                    }
                }
                if (found) break;
                p = p.parentElement;
            }
        }

        // Strategy B: "You can’t mute [Name]’s microphone remotely" button.
        // Visible on tile overlay when that participant’s mic is ON.
        const cantMute = document.querySelectorAll(‘button[aria-label*="microphone remotely"]’);
        for (const btn of cantMute) {
            const name = _extractMuteButtonName(btn);
            if (name) { recordSpeaker(name); found = true; }
        }

        return found;
    }

})();
"""
