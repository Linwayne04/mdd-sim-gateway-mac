import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { Softphone as Phone, microphoneMessage, RELAY_UNAVAILABLE, RELAY_UNREACHABLE } from './softphone.js'
import { useI18n } from './i18n.jsx'
import { useContactNames } from './contactNames.js'

const GREEN = '#22c55e'
const RED = '#ef4444'
const DTMF_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#']

// Keep incoming-call registration independent of the page the administrator happens to be
// viewing. The Calls page owns its selected line (it needs the same Phone for outbound calls),
// while this hub owns every other enabled line. That gives every line exactly one browser
// Contact and makes a call that was held by the engine appear immediately after sign-in.
export default function GlobalSoftphone({ instances, excludedId, showToast }) {
  const { t } = useI18n()
  const phones = useRef(new Map())
  const wanted = useRef(new Set())
  const callRef = useRef(null)
  const clearTimer = useRef(null)
  const [call, setCallState] = useState(null)
  const [muted, setMuted] = useState(false)
  const [duration, setDuration] = useState(0)
  const [keypad, setKeypad] = useState(false)
  const [dtmfSeq, setDtmfSeq] = useState('')

  const setCall = (next) => {
    callRef.current = typeof next === 'function' ? next(callRef.current) : next
    setCallState(callRef.current)
  }

  // Only identity and display name matter here. Periodic status refreshes replace the instance
  // objects, but must not tear every SIP registration down and build it again.
  const lineKey = useMemo(() => instances.map((line) => `${line.id}:${line.name || ''}`).sort().join('|'), [instances])

  useEffect(() => {
    const desired = new Set(instances
      .map((line) => String(line.id))
      .filter((id) => excludedId === null || excludedId === undefined || id !== String(excludedId)))
    wanted.current = desired

    for (const [id, phone] of phones.current.entries()) {
      if (!desired.has(id) && callRef.current?.id !== id) {
        phone.stop()
        phones.current.delete(id)
      }
    }

    for (const line of instances) {
      const id = String(line.id)
      if (!desired.has(id) || phones.current.has(id)) continue
      api.softphone(id).then((prov) => {
        if (!prov?.enabled || !wanted.current.has(id) || phones.current.has(id)) return
        let phone
        const onEvent = (type, data) => {
          if (type === 'incoming') {
            // Different lines can ring at the same instant. Once one call owns the browser's
            // microphone, reject a second one as busy instead of replacing the visible call.
            if (callRef.current && callRef.current.id !== id && callRef.current.state !== 'ended') {
              phone.rejectBusy()
              return
            }
            clearTimeout(clearTimer.current)
            setMuted(false)
            setKeypad(false)
            setDtmfSeq('')
            setCall({ id, line: line.name || id, number: data?.from || t('Unknown'), state: 'incoming' })
          } else if (type === 'active') {
            setCall((current) => current?.id === id
              ? { ...current, state: 'active', startedAt: current.startedAt || Date.now() } : current)
          } else if (type === 'ended' || type === 'failed') {
            if (callRef.current?.id !== id) return
            setCall((current) => current ? { ...current, state: 'ended', endCause: data?.cause } : current)
            setMuted(false)
            setKeypad(false)
            clearTimer.current = setTimeout(() => setCall(null), 1800)
          } else if (type === 'mediafallback') {
            // Answered without a microphone: the caller is audible, the user is not. Say so
            // on the overlay for the whole call, not just in a toast that scrolls away.
            setCall((current) => current?.id === id ? { ...current, listenOnly: true } : current)
            showToast?.(t(microphoneMessage(data)))
          } else if (type === 'mediafail') {
            // Last resort: not even a silent track could be built, so the call really died.
            showToast?.(t(microphoneMessage(data)))
          } else if (type === 'audioblocked') {
            showToast?.(t('Browser blocked call audio. Click the page once and try again.'))
          } else if (type === 'relayunavailable') {
            showToast?.(t(RELAY_UNAVAILABLE))
          } else if (type === 'relayunreachable') {
            showToast?.(t(RELAY_UNREACHABLE))
          }
        }
        phone = new Phone(onEvent, null, () => api.softphone(id))
        if (!phone.start(prov, prov.host || location.hostname)) return
        phones.current.set(id, phone)
      }).catch(() => {})
    }
  }, [lineKey, excludedId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {
    clearTimeout(clearTimer.current)
    for (const phone of phones.current.values()) phone.stop()
    phones.current.clear()
  }, [])

  // Asked for before the hook can early-return, and with the number it is ringing from: a
  // caller who is in the address book should be named on the very first frame of the overlay.
  const callerName = useContactNames(call?.number ? [call.number] : [], call?.id)[call?.number] || ''

  useEffect(() => {
    if (call?.state !== 'active' || !call.startedAt) { setDuration(0); return }
    const timer = setInterval(() => setDuration(Math.floor((Date.now() - call.startedAt) / 1000)), 500)
    return () => clearInterval(timer)
  }, [call?.state, call?.startedAt])

  const sendTone = useCallback((key) => {
    const active = callRef.current
    if (active?.state !== 'active') return
    const phone = phones.current.get(active.id)
    if (!phone) return
    phone.sendDTMF(key)
    setDtmfSeq((current) => (current + key).slice(-32))
  }, [])

  useEffect(() => {
    if (!(keypad && call?.state === 'active')) return
    const onKey = (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (!/^[0-9*#]$/.test(event.key)) return
      event.preventDefault()
      sendTone(event.key)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [keypad, call?.state, sendTone])

  if (!call) return null
  const phone = phones.current.get(call.id)
  const answer = () => { phone?.unlockAudio(); phone?.answer() }
  const decline = () => { phone?.reject(); setKeypad(false); setCall({ ...call, state: 'ended', endCause: 'Rejected' }) }
  const hangup = () => { phone?.hangup(); setKeypad(false); setCall({ ...call, state: 'ended' }) }
  const toggleMute = () => {
    const next = !muted
    setMuted(next)
    phone?.setMuted(next)
  }
  const toggleKeypad = () => { setDtmfSeq(''); setKeypad((open) => !open) }
  const clock = `${String(Math.floor(duration / 60)).padStart(2, '0')}:${String(duration % 60).padStart(2, '0')}`

  return <div style={{ position: 'fixed', inset: 0, zIndex: 200, background: 'rgba(6,10,20,0.86)',
    backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
    <div className="card" role="dialog" aria-modal="true" style={{ padding: keypad ? 24 : 40,
      width: 'min(390px, calc(100vw - 24px))', maxHeight: 'calc(100dvh - 24px)', overflowY: 'auto', textAlign: 'center',
      boxShadow: '0 20px 60px rgba(0,0,0,.65)' }}>
      <div style={{ fontSize: 13, color: 'var(--text-mute)', letterSpacing: 1, textTransform: 'uppercase' }}>
        {t(call.state === 'incoming' ? 'Incoming call' : call.state === 'active' ? 'Connected' : 'Call ended')}
      </div>
      <div style={{ margin: keypad ? '12px auto' : '24px auto', width: keypad ? 64 : 104,
        height: keypad ? 64 : 104, borderRadius: '50%', display: 'grid',
        placeItems: 'center', background: `${call.state === 'active' ? GREEN : '#3b82f6'}22`,
        color: call.state === 'active' ? GREEN : '#60a5fa', fontSize: keypad ? 28 : 38, fontWeight: 800 }}>
        {(call.number || '?').replace(/\D/g, '').slice(-2) || '?'}
      </div>
      <div className={callerName ? '' : 'mono'} style={{ fontSize: 26, fontWeight: 800 }}>{callerName || call.number}</div>
      {!!callerName && <div className="mono" style={{ fontSize: 13, color: 'var(--text-mute)' }}>{call.number}</div>}
      <div style={{ fontSize: 13, color: 'var(--text-mute)', marginTop: 7 }}>{call.line}</div>
      {call.state === 'active' && <div className="mono" style={{ color: GREEN, marginTop: 12 }}>{clock}</div>}
      {call.listenOnly && call.state !== 'ended' && <div style={{ fontSize: 12, color: '#f59e0b', marginTop: 8 }}>
        {t('Listen only · the other side cannot hear you')}
      </div>}

      {call.state === 'active' && keypad && <div style={{ maxWidth: 220, margin: '18px auto 0', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div className="mono" style={{ minHeight: 40, padding: '8px 12px', borderRadius: 8,
          background: 'var(--surface-2, rgba(255,255,255,0.06))', border: '1px solid var(--border, rgba(255,255,255,0.12))',
          fontSize: 20, letterSpacing: 2, overflow: 'hidden', whiteSpace: 'nowrap', direction: 'rtl',
          color: dtmfSeq ? 'var(--text)' : 'var(--text-mute)' }}>
          {dtmfSeq || t('Type or tap keys')}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
          {DTMF_KEYS.map((key) => <button key={key} type="button" className="btn btn-ghost"
            style={{ minHeight: 44, fontSize: 18 }} onClick={() => sendTone(key)}>{key}</button>)}
        </div>
      </div>}

      {call.state === 'incoming' && <div style={{ display: 'flex', justifyContent: 'center', gap: 56, marginTop: 34 }}>
        <ActionButton label={t('Decline')} icon="✕" color={RED} onClick={decline} />
        <ActionButton label={t('Answer')} icon="✆" color={GREEN} onClick={answer} pulse />
      </div>}
      {call.state === 'active' && <div style={{ display: 'flex', justifyContent: 'center', gap: keypad ? 22 : 28, marginTop: keypad ? 20 : 34 }}>
        <ActionButton label={t(muted ? 'Unmute' : 'Mute')} icon={muted ? '🔇' : '🎙'} color="#3b82f6" onClick={toggleMute} pressed={muted} />
        <ActionButton label={t('Keypad')} icon="⌨" color="#a78bfa" onClick={toggleKeypad} pressed={keypad} />
        <ActionButton label={t('Hangup')} icon="✕" color={RED} onClick={hangup} />
      </div>}
      {call.state === 'ended' && <div style={{ color: 'var(--text-mute)', marginTop: 28 }}>{t('Call ended')}</div>}
    </div>
    <style>{`@keyframes global-ringpulse{0%{box-shadow:0 0 0 0 ${GREEN}88}70%{box-shadow:0 0 0 16px ${GREEN}00}100%{box-shadow:0 0 0 0 ${GREEN}00}}`}</style>
  </div>
}

function ActionButton({ label, icon, color, onClick, pulse = false, pressed }) {
  return <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
    <button type="button" onClick={onClick} aria-label={label} aria-pressed={pressed}
      style={{ width: 68, height: 68, borderRadius: '50%', border: 'none', cursor: 'pointer',
        fontSize: 26, background: color, color: '#fff', animation: pulse ? 'global-ringpulse 1.4s infinite' : 'none' }}>{icon}</button>
    <span style={{ fontSize: 13, color: 'var(--text-soft)' }}>{label}</span>
  </div>
}
