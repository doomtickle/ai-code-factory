---
name: shown-select
base: 3d14acd1d4bbd5ee14bccd7c3bfcf420447ad180
reference: a455a193e03f7f90c49e884a4041887c5ea30663
tier: smoke
area: dashboard
---
Work only from commit 3d14acd1d4bbd5ee14bccd7c3bfcf420447ad180 (`base`). Do not look at any later history, other branches or refs, or the reflog; the task is to solve this from the code as it stands at `base`.

Dashboard: let the owner select and copy part of the "What do you need from me?" pop-up.

Goal: the Thoughtfield's pop-up (the `#shown` panel, drawn by `scripts/dashboard-ui/field-shown.js`) holds the Coordinator's list of what waits on the owner, with commands the owner may want to copy. Today the owner cannot reliably select a few words of it and copy them with the browser's own copy (Ctrl+C or right-click, Copy): a selection started in the text disappears by itself within a moment, and pressing or moving the pointer inside the panel also acts on the 3D scene behind it. Fix both, so that selecting part of the text by mouse drag works and the selection stays until the owner clears it or the text itself changes.

Files: `scripts/dashboard-ui/field-shown.js` and `field-shown.css`; the page code that handles pointer clicks and hover over the sky (`field-sky.js`, `field-hud.js`); the browser test `scripts/test_field_shown_browser.js`; the dashboard guide (`docs/guides/06-dashboard.md`) and `scripts/CLAUDE.md` where they describe the pop-up.

Known facts:
- The page polls `/api/state` regularly and redraws the panel from each poll.
- The COPY button's rules in `scripts/CLAUDE.md` stay exactly as they are: it writes to the clipboard only on a click, only the text on screen, and is dark for a while after the text changes. The page never reads the clipboard. The owner's own browser copy is a separate path and needs none of that.
- The text is agent-influenced and is drawn as plain text. Keep it that way.
- Read `scripts/CLAUDE.md` before editing anything under `scripts/dashboard*` or `scripts/dashboard-ui/`.

Constraints: no new dependency, no change to the CSP or headers, nothing new sent anywhere. Give the selection a visible highlight that reads in the dashboard's theme.

Done when:
- Part of the pop-up's text can be selected with a mouse drag and with a programmatic range, and a state poll that brings the same text leaves the selection and the text node untouched.
- When the text does change, the panel shows the new text and a selection of the old text is gone.
- A click or pointer movement inside the pop-up no longer selects or highlights things in the 3D scene behind it.
- The selection has a visible highlight colour.
- The dashboard guide and `scripts/CLAUDE.md` say the owner can select part of the text and copy it with the browser's own copy, and that a poll does not clear the selection unless the text changes.
Tests required: extend `scripts/test_field_shown_browser.js` for the first two Done-when items (selection survives a same-text poll; changed text replaces it and drops the old selection). Run that file whole; no other browser test.
Out of scope: the COPY button's behaviour, the pop-up's layout, auto-close or ageing, any other panel.
Risk: low
