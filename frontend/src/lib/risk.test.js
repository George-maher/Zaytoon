import { describe, it, expect } from 'vitest'
import { RISK_TONES, riskTone, riskColor, riskPillClass } from './risk'

/**
 * Two fields on a `require_confirmation` frame describe risk and they do not
 * share a vocabulary. Whatever the backend sent, the dialog must land on one
 * of three tones — and must never round an unrecognised risk down to "safe".
 */
describe('riskTone', () => {
  it('normalises every known word onto the three tones', () => {
    expect(RISK_TONES).toEqual(['safe', 'confirm', 'high'])
    expect(riskTone('safe')).toBe('safe')
    expect(riskTone('low')).toBe('safe')
    expect(riskTone('confirm')).toBe('confirm')
    expect(riskTone('moderate')).toBe('confirm')
    expect(riskTone('medium')).toBe('confirm')
    expect(riskTone('high')).toBe('high')
    expect(riskTone('dangerous')).toBe('high')
    expect(riskTone('critical')).toBe('high')
  })

  it('is case-insensitive', () => {
    expect(riskTone('DANGEROUS')).toBe('high')
    expect(riskTone('Safe')).toBe('safe')
  })

  it('prefers the permission tier over the regex level', () => {
    // The tier describes the class of action; the level grades one argument
    // set. When they disagree the tier leads — but see the next test.
    expect(riskTone('confirm', 'high')).toBe('confirm')
    expect(riskTone('safe', 'moderate')).toBe('safe')
  })

  it('grades from the level when the tier is absent or unknown', () => {
    expect(riskTone(undefined, 'high')).toBe('high')
    expect(riskTone('', 'low')).toBe('safe')
    expect(riskTone('banana', 'critical')).toBe('high')
  })

  it('defaults to confirm for anything it cannot classify', () => {
    // Unknown must never read as safe: that is the direction that lets an
    // unclassified action run unattended.
    expect(riskTone()).toBe('confirm')
    expect(riskTone('banana')).toBe('confirm')
    expect(riskTone(null, undefined)).toBe('confirm')
  })
})

describe('riskColor / riskPillClass', () => {
  it('maps each tone to a palette token, not a literal colour', () => {
    expect(riskColor('safe')).toBe('var(--hud-green)')
    expect(riskColor('high')).toBe('var(--hud-red)')
    expect(riskColor('confirm')).toBe('var(--hud-amber)')
    // Unknown tones take the cautious amber rather than falling through.
    expect(riskColor('weird')).toBe('var(--hud-amber)')
  })

  it('gives each tone the matching pill modifier', () => {
    expect(riskPillClass('safe')).toBe('hud-pill hud-pill--ok')
    expect(riskPillClass('high')).toBe('hud-pill hud-pill--danger')
    expect(riskPillClass('confirm')).toBe('hud-pill hud-pill--warn')
    expect(riskPillClass('weird')).toBe('hud-pill hud-pill--warn')
  })
})
