// Focus: one thing to work on now. Desktop half.
// Backend: /api/plugins/focus/* (dashboard/plugin_api.py on the Hermes host), polled every 30 s.
// Every write is a direct user click; the pane never accepts, completes, defers or pins on its own.
// The same card renders inline in any chat when the agent writes ::focus{} alone on a line.
import * as sdk from '@hermes/plugin-sdk'
import {
  host, useQuery, useQueryClient, haptic, Button, Input, Codicon,
  STATUSBAR_AREAS, PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

const ID = 'focus'
const KEY = [ID, 'state']
let rest = null

const muted = 'text-xs leading-5 text-(--ui-text-secondary)'
const faint = 'text-[0.6875rem] leading-4 text-(--ui-text-tertiary)'

function unwrap(r) {
  const body = r && typeof r === 'object' && 'ok' in r ? r : r?.data ?? r
  if (!body) throw new Error('no response')
  return body
}

function useFocus() {
  return useQuery({
    queryKey: KEY,
    queryFn: async () => {
      if (!rest) throw new Error('backend not ready')
      const body = unwrap(await rest('/state'))
      if (body.ok !== true) throw new Error(body.error || 'state unavailable')
      return body.decision
    },
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: 1
  })
}

function useAct() {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const run = async (path, body, okMessage) => {
    if (!rest || busy) return
    setBusy(true)
    try {
      const r = unwrap(await rest(path, { method: 'POST', body }))
      if (r.ok !== true) throw new Error(r.error || 'failed')
      haptic('success')
      if (okMessage) host.notify({ kind: 'success', message: okMessage })
      qc.setQueryData(KEY, r.decision)
    } catch (e) {
      host.notifyError(e, 'Focus action failed')
    } finally {
      setBusy(false)
      qc.invalidateQueries({ queryKey: KEY })
    }
  }
  return { run, busy }
}

function openSource(f) {
  if (f?.source === 'session' && f.ref && host.openSession) {
    haptic('tap')
    Promise.resolve(host.openSession(f.ref)).catch(() => host.notify({ kind: 'error', message: 'Could not open the chat' }))
  } else if (f?.source === 'kanban') {
    host.navigate('/kanban')
  } else if (f?.resume) {
    navigator.clipboard?.writeText(f.resume)
    host.notify({ kind: 'info', message: `Copied: ${f.resume}` })
  }
}

function LeaseBar({ focus, left }) {
  const total = Math.max(1, (focus.lease_until - focus.accepted_at) / 60)
  const pct = Math.max(0, Math.min(100, (left / total) * 100))
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx('div', {
        className: 'h-1 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: 'h-full rounded-full bg-(--ui-accent) transition-[width] duration-700 ease-out',
          style: { width: `${pct}%` }
        })
      }),
      jsx('div', { className: faint, children: `${left} min left on the lease` })
    ]
  })
}

function FocusCard({ d, act }) {
  const f = d.focus
  const leased = d.mode === 'leased'
  const [proofOpen, setProofOpen] = useState(false)
  const [proof, setProof] = useState('')
  const submitDone = () => {
    if (proof.trim().length < 8) {
      host.notify({ kind: 'warning', message: 'Say what proves it is done' })
      return
    }
    act.run('/done', { proof: proof.trim() }, 'Done. Next one is ready.')
    setProof('')
    setProofOpen(false)
  }
  return jsxs('div', {
    className: 'flex flex-col gap-2.5 rounded-lg border border-(--ui-stroke-secondary) p-3 ' +
      (leased ? 'border-(--ui-accent)/60 ' : '') + 'transition-colors duration-300',
    children: [
      jsxs('div', {
        className: 'flex items-start gap-2',
        children: [
          jsx(Codicon, { name: leased ? 'target' : 'lightbulb', className: 'mt-0.5 text-(--ui-accent)' }),
          jsx('div', { className: 'text-sm font-medium leading-5 text-foreground', children: f.title })
        ]
      }),
      jsx('div', { className: muted, children: f.next_action }),
      jsx('div', { className: faint, children: `${f.gate_reason}: ${(f.evidence || []).slice(0, 3).join('; ')}` }),
      f.done_when && jsx('div', { className: faint, children: `Done when ${f.done_when}` }),
      leased && jsx(LeaseBar, { focus: f, left: d.lease_left_min }),
      proofOpen
        ? jsxs('div', {
            className: 'flex flex-col gap-1.5',
            children: [
              jsx(Input, {
                autoFocus: true,
                value: proof,
                placeholder: 'What proves it is done?',
                onChange: e => setProof(e.target.value),
                onKeyDown: e => {
                  if (e.key === 'Enter') submitDone()
                  if (e.key === 'Escape') setProofOpen(false)
                }
              }),
              jsxs('div', {
                className: 'flex gap-1.5',
                children: [
                  jsx(Button, { size: 'sm', disabled: act.busy, onClick: submitDone, children: 'Complete' }),
                  jsx(Button, { size: 'sm', variant: 'outline', onClick: () => setProofOpen(false), children: 'Cancel' })
                ]
              })
            ]
          })
        : jsxs('div', {
            className: 'flex flex-wrap gap-1.5',
            children: [
              leased
                ? jsx(Button, { size: 'sm', disabled: act.busy, onClick: () => setProofOpen(true), children: 'Done' })
                : jsx(Button, {
                    size: 'sm',
                    disabled: act.busy,
                    onClick: () => act.run('/accept', { candidate: f.id }, 'Focus leased'),
                    children: 'Focus on this'
                  }),
              jsx(Button, { size: 'sm', variant: 'outline', onClick: () => openSource(f), children: 'Open' }),
              jsx(Button, {
                size: 'sm',
                variant: 'outline',
                disabled: act.busy,
                onClick: () => act.run('/defer', { candidate: f.id, hours: leased ? 2 : 24 }, leased ? 'Released for 2 h' : 'Snoozed for a day'),
                children: leased ? 'Release' : 'Not today'
              })
            ]
          })
    ]
  })
}

function whyText(c) {
  const parts = (c.contributions || []).slice(0, 3).map(([k, v]) => `${k} ${v > 0 ? '+' : ''}${v}`).join(', ')
  return `${c.gate_reason} · score ${c.score}${parts ? ` (${parts})` : ''}\n${(c.evidence || []).join('; ')}`
}

function Row({ c, act }) {
  return jsxs('div', {
    className:
      'group flex w-full items-center gap-1 rounded-md border border-(--ui-stroke-secondary) pr-1 ' +
      'transition-all duration-200 hover:-translate-y-px hover:border-(--ui-accent)',
    children: [
      jsxs('button', {
        type: 'button',
        title: whyText(c),
        disabled: act.busy,
        onClick: () => act.run('/accept', { candidate: c.id }, 'Focus switched'),
        className: 'flex min-w-0 flex-1 items-center gap-2 px-2.5 py-1.5 text-left active:translate-y-px',
        children: [
          jsx('span', { className: `w-4 shrink-0 ${faint}`, children: c.n }),
          jsxs('span', {
            className: 'flex min-w-0 flex-col',
            children: [
              jsx('span', { className: 'truncate text-xs text-foreground', children: c.title }),
              jsx('span', {
                className: `${faint} ${c.gate <= 1 ? 'text-(--ui-accent)' : ''}`,
                children: c.gate_reason
              })
            ]
          })
        ]
      }),
      jsx('button', {
        type: 'button',
        title: c.pinned ? 'Unpin' : 'Pin: this matters, rank it above inferred work',
        'aria-label': c.pinned ? 'Unpin' : 'Pin',
        'aria-pressed': !!c.pinned,
        disabled: act.busy,
        onClick: () => act.run('/pin', { candidate: c.id, pinned: !c.pinned }, c.pinned ? 'Unpinned' : 'Pinned'),
        className:
          'flex h-6 w-6 shrink-0 items-center justify-center rounded transition-all duration-200 hover:scale-110 ' +
          (c.pinned ? 'text-(--ui-accent) opacity-100' : 'opacity-0 group-hover:opacity-70 focus-visible:opacity-100'),
        children: jsx(Codicon, { name: c.pinned ? 'pinned' : 'pin' })
      })
    ]
  })
}

function modelLine(dm) {
  if (!dm || !dm.on) return ''
  const n = (dm.judged || 0) + (dm.cached || 0)
  return ` · decision model ${dm.backend}: ${n} chats judged`
}

function FocusPane({ compact = false } = {}) {
  const q = useFocus()
  const act = useAct()
  if (q.isLoading) return jsx('div', { className: `p-3 ${muted}`, children: 'Reading open work…' })
  if (q.isError)
    return jsxs('div', {
      className: 'flex flex-col gap-2 p-3',
      children: [
        jsx('div', { className: 'text-sm text-foreground', children: 'Focus backend unavailable' }),
        jsx('div', { className: muted, children: String(q.error?.message || q.error) }),
        jsx('div', { className: faint, children: 'Enable the focus plugin on the Hermes host (hermes plugins enable focus), then restart the gateway.' }),
        jsx(Button, { size: 'sm', variant: 'outline', className: 'self-start', onClick: () => q.refetch(), children: 'Retry' })
      ]
    })
  const d = q.data
  if (d.mode === 'empty' || !d.focus)
    return jsx('div', { className: `p-3 ${muted}`, children: 'Nothing open. Tell the agent what you must get done, or run /now add.' })
  const today = d.today || {}
  const rows = (d.ladder || d.next || []).filter(c => c.id !== d.focus.id).slice(0, compact ? 3 : 6)
  return jsxs('div', {
    className: compact ? 'flex flex-col gap-2.5' : 'flex h-full flex-col gap-3 overflow-y-auto p-3',
    children: [
      d.interrupt &&
        jsxs('div', {
          className: 'flex flex-col gap-1.5 rounded-lg border border-(--ui-accent) p-2.5 animate-in fade-in slide-in-from-top-1',
          children: [
            jsx('div', { className: 'text-xs text-foreground', children: `More urgent: ${d.interrupt.title}` }),
            jsx('div', { className: faint, children: d.interrupt.gate_reason }),
            jsx(Button, {
              size: 'sm',
              className: 'self-start',
              disabled: act.busy,
              onClick: () => act.run('/accept', { candidate: d.interrupt.id }, 'Switched focus'),
              children: 'Switch to it'
            })
          ]
        }),
      jsx(FocusCard, { d, act }),
      rows.length > 0 &&
        jsx('div', { className: 'flex flex-col gap-1.5', children: rows.map(c => jsx(Row, { c, act }, c.id)) }),
      jsx('div', {
        className: `mt-auto ${faint}`,
        children:
          `${today.done || 0} done today · ${today.on_focus || 0} turns on focus · ${today.drift || 0} drifted · ` +
          `${d.counts?.candidates ?? 0} open items${modelLine(d.decision_model)}`
      })
    ]
  })
}

function FocusChip() {
  const q = useFocus()
  const d = q.data
  const label = !d ? 'focus …' : d.mode === 'leased' ? `${d.focus.title} · ${d.lease_left_min}m` : d.focus ? 'pick a focus' : 'no focus'
  return jsxs('button', {
    type: 'button',
    className: `flex max-w-[22rem] items-center gap-1 truncate px-1.5 ${faint} ${d?.interrupt ? 'text-(--ui-accent)' : ''}`,
    title: d?.interrupt ? `More urgent: ${d.interrupt.title}` : 'Focus',
    onClick: () => host.navigate('/focus'),
    children: [jsx(Codicon, { name: 'target' }), jsx('span', { className: 'truncate', children: label })]
  })
}

export default {
  id: ID,
  name: 'Focus',
  register(ctx) {
    rest = ctx.rest
    ctx.register({ id: 'pane', area: 'panes', title: 'Focus', data: { placement: 'right', width: '320px' }, render: () => jsx(FocusPane, {}) })
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 120, render: () => jsx(FocusChip, {}) })
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/focus' },
      render: () => jsx('div', { className: 'mx-auto h-full w-full max-w-xl', children: jsx(FocusPane, {}) })
    })
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/focus', label: 'Focus', codicon: 'target' } })
    // Inline card in any chat: the agent writes ::focus{} alone on a line. Attributes are ignored
    // (they are untrusted model output); the card always shows live backend state, and every
    // write is still a user click. Older Desktops without the area keep the pane and the chip.
    if (typeof sdk.TRANSCRIPT_DIRECTIVE_AREA === 'string') {
      ctx.register({
        id: 'inline-card',
        area: sdk.TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'focus',
          render: () => jsx('div', { className: 'my-1 w-full max-w-md', children: jsx(FocusPane, { compact: true }) })
        }
      })
    }
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: { id: 'focus.open', label: 'Focus: what should I work on now', keywords: ['focus', 'next', 'priority', 'one thing'], run: () => host.navigate('/focus') }
    })
  }
}
