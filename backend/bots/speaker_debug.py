"""
speaker_debug.py
────────────────
Dumps DOM state relevant to speaker detection to logs/debug_PLATFORM_N.json.

Run once per platform after joining to find the real selectors.
The dump captures:
  - All aria-labels on the page
  - All data-* attributes that look speaker/audio related
  - All class-name fragments containing: speaking, audio, muted, active, participant
  - The structure of aria-live regions (captions)
  - Shadow DOM content (one level deep)
"""

import json
import os
import time
from typing import Optional


_DUMP_JS = r"""
() => {
    const out = {
        ariaLabels: [],
        dataAttrs: [],
        classFragments: [],
        ariaLiveRegions: [],
        shadowAriaLabels: [],
        nameElements: [],
    };

    const AUDIO_KW = /speak|muted|audio|active|particip|roster|tile|video|speaker|sound/i;

    // 1. All aria-labels
    document.querySelectorAll('[aria-label]').forEach(el => {
        const lbl = el.getAttribute('aria-label') || '';
        if (lbl.length > 1 && lbl.length < 200) {
            out.ariaLabels.push({
                tag: el.tagName.toLowerCase(),
                label: lbl,
                cls: (el.className || '').substring(0, 80),
                role: el.getAttribute('role') || '',
            });
        }
    });

    // 2. data-* attributes matching audio/speaking keywords
    document.querySelectorAll('*').forEach(el => {
        for (const attr of el.attributes) {
            if (!attr.name.startsWith('data-')) continue;
            if (AUDIO_KW.test(attr.name) || AUDIO_KW.test(attr.value)) {
                out.dataAttrs.push({
                    tag: el.tagName.toLowerCase(),
                    attr: attr.name,
                    value: attr.value.substring(0, 100),
                    cls: (el.className || '').substring(0, 80),
                    ariaLabel: el.getAttribute('aria-label') || '',
                });
            }
        }
    });

    // 3. Unique class-name fragments matching audio/speaking keywords
    const seenCls = new Set();
    document.querySelectorAll('*').forEach(el => {
        const cls = el.className || '';
        if (typeof cls !== 'string') return;
        cls.split(/\s+/).forEach(c => {
            if (c.length > 2 && AUDIO_KW.test(c) && !seenCls.has(c)) {
                seenCls.add(c);
                out.classFragments.push(c);
            }
        });
    });

    // 4. aria-live regions content
    document.querySelectorAll('[aria-live]').forEach(region => {
        out.ariaLiveRegions.push({
            live: region.getAttribute('aria-live'),
            cls: (region.className || '').substring(0, 80),
            html: region.innerHTML.substring(0, 500),
            text: region.textContent.trim().substring(0, 300),
        });
    });

    // 5. Shadow DOM aria-labels (one level)
    document.querySelectorAll('*').forEach(el => {
        if (!el.shadowRoot) return;
        el.shadowRoot.querySelectorAll('[aria-label]').forEach(inner => {
            const lbl = inner.getAttribute('aria-label') || '';
            if (lbl.length > 1 && lbl.length < 200) {
                out.shadowAriaLabels.push({
                    hostTag: el.tagName.toLowerCase(),
                    hostCls: (el.className || '').substring(0, 60),
                    label: lbl,
                    innerTag: inner.tagName.toLowerCase(),
                });
            }
        });
    });

    // 6. Actual text content of name-bearing elements (for debugging scraper)
    const NAME_SELS = [
        '[class*="video-avatar__avatar-name"]',
        '[class*="video-avatar__avatar-title"]',
        '[class*="participants-item__display-name"]',
        '[class*="participants-item"] [class*="name"]',
        '[class*="participant-name"]',
        '[class*="display-name"]',
        '[class*="nameplate"]',
        '[class*="name-label"]',
        '[class*="roster"] [class*="name"]',
    ];
    for (const sel of NAME_SELS) {
        try {
            document.querySelectorAll(sel).forEach(el => {
                const text = (el.innerText || el.textContent || '').trim();
                if (text.length > 0 && text.length < 100) {
                    out.nameElements.push({ sel, text, cls: (el.className || '').substring(0, 60) });
                }
            });
        } catch(e) {}
    }

    return out;
}
"""


async def dump_speakers(page, platform: str, label: str = "") -> Optional[str]:
    """
    Evaluate the dump JS on `page` and save results to
    logs/debug_{platform}_{label}_{timestamp}.json.

    Returns the path of the saved file, or None on failure.
    """
    try:
        data = await page.evaluate(_DUMP_JS)
    except Exception as exc:
        print(f"[SpeakerDebug] JS eval failed: {exc}")
        return None

    os.makedirs("logs", exist_ok=True)
    ts = int(time.time())
    suffix = f"_{label}" if label else ""
    path = f"logs/debug_{platform}{suffix}_{ts}.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    n_labels = len(data.get("ariaLabels", []))
    n_data   = len(data.get("dataAttrs", []))
    n_cls    = len(data.get("classFragments", []))
    n_live   = len(data.get("ariaLiveRegions", []))
    print(f"[SpeakerDebug] Saved {path}  "
          f"(aria-labels={n_labels}, data-attrs={n_data}, "
          f"classes={n_cls}, live-regions={n_live})")
    return path
