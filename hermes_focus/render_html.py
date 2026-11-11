"""The visual card: one self-contained HTML file, no external assets, no script.

Hermes Desktop renders it inline in a chat with ``::preview{file="<path>"}``. Colours and font come
from the app theme variables the preview frame injects (--foreground, --muted-foreground, --accent,
--border, --card), with fallbacks so the file also reads well in a browser. Buttons use the
documented ``data-hermes-send`` attribute: a click sends one plain sentence to the agent, which
calls the matching focus tool. The card itself never changes state.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import Any

_CSS = """
:root{--fg:var(--foreground,#e8e8ea);--mut:var(--muted-foreground,#9a9aa3);--acc:var(--accent,#7c9cff);
--line:var(--border,rgba(128,128,128,.28));--surf:var(--card,rgba(128,128,128,.06))}
*{box-sizing:border-box}
.fx{display:inline-block;width:560px;min-width:560px;color:var(--fg);font-size:13px;line-height:1.45}
.card{border:1px solid var(--line);border-radius:12px;background:var(--surf);padding:14px 16px}
.card.leased{border-color:var(--acc)}
h1{font-size:16px;line-height:1.3;margin:0 0 4px;font-weight:600;letter-spacing:-.01em}
.next{margin:0 0 10px}
.meta{color:var(--mut);font-size:12px;margin:2px 0}
.meta b{color:var(--fg);font-weight:500}
.bar{height:4px;border-radius:99px;background:var(--line);overflow:hidden;margin:10px 0 4px}
.bar i{display:block;height:100%;border-radius:99px;background:var(--acc);animation:grow .9s ease-out}
@keyframes grow{from{width:0}}
.row{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
button{font:inherit;font-size:12px;font-weight:500;border-radius:8px;padding:6px 12px;cursor:pointer;
border:1px solid var(--line);background:transparent;color:var(--fg);
transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease}
button:hover{transform:translateY(-1px);border-color:var(--acc);box-shadow:0 4px 14px -6px var(--acc)}
button:active{transform:translateY(0) scale(.98)}
button.go{background:var(--acc);border-color:var(--acc);color:#0b0b0f}
.alert{border:1px solid var(--acc);border-radius:10px;padding:9px 12px;margin-bottom:8px;
display:flex;align-items:center;gap:10px;animation:drop .35s ease-out}
.alert span{flex:1;min-width:0}
@keyframes drop{from{opacity:0;transform:translateY(-4px)}}
ol{list-style:none;margin:10px 0 0;padding:0;display:flex;flex-direction:column;gap:5px}
li{display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:9px;padding:6px 10px;
transition:border-color .15s ease,transform .15s ease}
li:hover{border-color:var(--acc);transform:translateX(2px)}
li .n{color:var(--mut);width:14px;text-align:right;flex:none;font-variant-numeric:tabular-nums}
li .t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{flex:none;font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:1px 8px}
.chip.hot{color:var(--acc);border-color:var(--acc)}
li button{padding:2px 9px;font-size:11px}
.foot{color:var(--mut);font-size:11px;margin-top:10px}
"""


def _t(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "-"


_SAFE_ID = re.compile(r"[A-Za-z0-9_:.\-]{1,80}")


def _button(label: str, verb: str, cid: Any = None, *, cls: str = "") -> str:
    """A button that sends one fixed sentence to the agent. Titles never go into the sentence:
    a chat title is untrusted text and must not become an instruction. Ids are checked."""
    target = ""
    if cid is not None:
        if not _SAFE_ID.fullmatch(str(cid)):
            return ""
        target = f" with candidate {cid}"
    text = f"Focus card: {verb}{target}, then show the focus card again."
    klass = f' class="{cls}"' if cls else ""
    return f'<button{klass} data-hermes-send="{escape(text, quote=True)}">{escape(label)}</button>'


def _why(f: dict[str, Any]) -> str:
    return "; ".join(str(e) for e in (f.get("evidence") or [])[:3])


def card(d: dict[str, Any]) -> str:
    mode, f = d.get("mode"), d.get("focus")
    parts: list[str] = ['<!doctype html><meta charset="utf-8"><style>', _CSS, '</style><div class="fx">']
    if mode == "empty" or not f:
        parts.append('<div class="card"><h1>Nothing open</h1><p class="meta">No blocked tasks, waiting chats, '
                     'failing crons or commitments. Tell the agent what you must get done.</p></div></div>')
        return "".join(parts)
    leased = mode == "leased"
    i = d.get("interrupt")
    if i:
        parts.append(
            f'<div class="alert"><span><b>More urgent:</b> {escape(str(i["title"]))} '
            f'<span class="meta">({escape(str(i["gate_reason"]))})</span></span>'
            + _button("Switch", "switch my focus. Call focus_accept", i["id"]) + "</div>")
    parts.append(f'<div class="card{" leased" if leased else ""}">')
    parts.append(f'<h1>{escape(str(f["title"]))}</h1>')
    parts.append(f'<p class="next">{escape(str(f.get("next_action", "")))}</p>')
    parts.append(f'<p class="meta"><b>{escape(str(f.get("gate_reason", "")))}</b> · {escape(_why(f))}</p>')
    if f.get("done_when"):
        parts.append(f'<p class="meta">Done when {escape(str(f["done_when"]))}</p>')
    if leased:
        total = max(1.0, (float(f.get("lease_until", 0)) - float(f.get("accepted_at", 0))) / 60)
        left = max(0, int(d.get("lease_left_min") or 0))
        pct = max(0, min(100, round(left / total * 100)))
        parts.append(f'<div class="bar"><i style="width:{pct}%"></i></div>')
        parts.append(f'<p class="meta">{left} min left, lease ends {_t(f.get("lease_until"))}</p>')
        parts.append('<div class="row">'
                     + _button("Done", "I finished my focus. Ask me in one line what proves it, then call focus_done",
                               cls="go")
                     + _button("Release", "release my focus for 2 hours. Call focus_defer with hours 2")
                     + "</div>")
    else:
        parts.append('<div class="row">'
                     + _button("Focus on this", "I accept this focus. Call focus_accept", f["id"], cls="go")
                     + _button("Not today", "not today for this one. Call focus_defer", f["id"])
                     + "</div>")
    parts.append("</div>")
    rows = [c for c in (d.get("ladder") or d.get("next") or []) if c.get("id") != f.get("id")][:5]
    if rows:
        parts.append("<ol>")
        for c in rows:
            hot = " hot" if int(c.get("gate", 4)) <= 1 else ""
            parts.append(
                f'<li><span class="n">{escape(str(c.get("n", "")))}</span>'
                f'<span class="t" title="{escape(_why(c), quote=True)}">{escape(str(c["title"]))}</span>'
                f'<span class="chip{hot}">{escape(str(c.get("gate_reason", "")))}</span>'
                + _button("Take", "switch my focus. Call focus_accept", c["id"]) + "</li>")
        parts.append("</ol>")
    today = d.get("today") or {}
    dm = d.get("decision_model") or {}
    foot = [f'{today.get("done", 0)} done today', f'{today.get("on_focus", 0)} turns on focus',
            f'{today.get("drift", 0)} drifted', f'{(d.get("counts") or {}).get("candidates", 0)} open items']
    if dm.get("on"):
        foot.append(f'decision model {escape(str(dm.get("backend", "")))}: '
                    f'{int(dm.get("judged", 0)) + int(dm.get("cached", 0))} chats judged')
    parts.append(f'<p class="foot">{" · ".join(foot)}</p></div>')
    return "".join(parts)
