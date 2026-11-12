"""The Fetch/Health HTML as it was drawn until #715 took it off the page (avo, 12 Nov 2026: every Fetch control and
all status live in the Grafana board).

``control_render.render_fetch`` now draws only what Grafana does not carry. The part renderers it used to join
(the top bar, Health right now, the providers, one group a file with its source boxes, the queue, Cross-check and
the weekly backups) are still in ``control_render`` so that the removal reverts cleanly; the tests of those parts
read them through ``old_fetch``. Both go together in the follow-up that deletes the parts.
"""
from __future__ import annotations

from RevenueChaser.core.html_safe import esc
from RevenueChaser.module_settings import control_render as cr


def old_fetch(view: dict) -> str:
    if view.get("error"):
        return cr._err(view, "Fetch/Health")
    view = cr._ready(view)
    groups = "".join(cr._fetch_group(f, view) for f in view["files"])
    return ('<section class="ctl" aria-label="Data/Health · Fetch/Health"><div class="wrap">'
            f'{cr._header(view, "Fetch/Health")}{cr._topbar(view)}{cr._health(view)}{cr._providers(view)}'
            f'<details class="sec" id="sec-universe" open><summary><h2>Universe</h2><span class="secs">'
            f'{esc(view["universe_summary"])} · the tier sets the next fetches and fills use</span></summary>'
            f'{cr._universe(view)}{cr._legend(view)}</details>'
            '<div class="ffilter" id="ffilter" hidden></div><div class="emptyst" id="ffempty" hidden></div>'
            f'<div class="fgrps">{groups}</div>{cr._picture_control(view)}{cr._queue(view)}{cr._cross(view)}'
            f'{cr._backups(view)}</div></section>')
