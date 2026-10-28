/**
 * Voice state machine tests for `lib/voiceState.js`.
 *
 * Covers: the seven known states, capture gate, unknown→recovering normalization,
 * legal transitions, and the `canCapture` / `canTransition` helpers.
 *
 * "An unknown state suppresses capture" is the key property: a state the
 * client has never seen is mapped to `RECOVERING` and therefore suppresses
 * the microphone — never `STANDBY`, because that would arm capture on an
 * unrecognized frame.
 */

import { describe, test, expect } from "vitest";

import {
  VOICE_STATES,
  LEGAL_TRANSITIONS,
  CAPTURING_STATES,
  ACTIVE_COMMAND_STATES,
  normalizeVoiceState,
  canTransition,
  isCapturing,
  isActiveCommand,
  canCapture,
  voiceStateBadge,
} from "../lib/voiceState";

const S = VOICE_STATES;

describe("voiceState — known states", () => {
  test("has exactly seven frozen states", () => {
    const vals = Object.values(S);
    expect(vals).toHaveLength(7);
    // `toHaveSize` is a jest-extended matcher, not a Vitest one.
    expect(new Set(vals).size).toBe(7);
  });

  test("states are string literals", () => {
    expect(S.DISABLED).toBe("disabled");
    expect(S.STANDBY).toBe("standby");
    expect(S.LISTENING).toBe("listening");
    expect(S.PROCESSING).toBe("processing");
    expect(S.AWAITING_CONFIRMATION).toBe("awaiting_confirmation");
    expect(S.SPEAKING).toBe("speaking");
    expect(S.RECOVERING).toBe("recovering");
  });
});

describe("voiceState — capture gate", () => {
  test("STANDBY and LISTENING are capturing", () => {
    expect(isCapturing(S.STANDBY)).toBe(true);
    expect(isCapturing(S.LISTENING)).toBe(true);
  });

  test("other states are not capturing", () => {
    expect(isCapturing(S.PROCESSING)).toBe(false);
    expect(isCapturing(S.AWAITING_CONFIRMATION)).toBe(false);
    expect(isCapturing(S.SPEAKING)).toBe(false);
    expect(isCapturing(S.RECOVERING)).toBe(false);
    expect(isCapturing(S.DISABLED)).toBe(false);
  });
});

describe("voiceState — unknown state normalization", () => {
  test("unknown state normalizes to RECOVERING", () => {
    expect(normalizeVoiceState("unknown-state")).toBe(S.RECOVERING);
    expect(normalizeVoiceState("")).toBe(S.RECOVERING);
    expect(normalizeVoiceState(null)).toBe(S.RECOVERING);
    expect(normalizeVoiceState(undefined)).toBe(S.RECOVERING);
  });

  test("known states pass through normalize", () => {
    expect(normalizeVoiceState(S.STANDBY)).toBe(S.STANDBY);
    expect(normalizeVoiceState(S.LISTENING)).toBe(S.LISTENING);
  });
});

describe("voiceState — canTransition", () => {
  test("legal transitions from STANDBY", () => {
    expect(canTransition(S.STANDBY, S.LISTENING)).toBe(true);
    expect(canTransition(S.STANDBY, S.PROCESSING)).toBe(true);
    expect(canTransition(S.STANDBY, S.SPEAKING)).toBe(true);
    expect(canTransition(S.STANDBY, S.DISABLED)).toBe(true);
    expect(canTransition(S.STANDBY, S.RECOVERING)).toBe(true);
    expect(canTransition(S.STANDBY, S.AWAITING_CONFIRMATION)).toBe(false);
  });

  test("legal transitions from LISTENING", () => {
    expect(canTransition(S.LISTENING, S.PROCESSING)).toBe(true);
    expect(canTransition(S.LISTENING, S.SPEAKING)).toBe(true);
    expect(canTransition(S.LISTENING, S.STANDBY)).toBe(true);
    expect(canTransition(S.LISTENING, S.DISABLED)).toBe(true);
    expect(canTransition(S.LISTENING, S.RECOVERING)).toBe(true);
  });

  test("illegal transitions are rejected", () => {
    expect(canTransition(S.DISABLED, S.SPEAKING)).toBe(false);
    expect(canTransition(S.PROCESSING, S.DISABLED)).toBe(true); // allowed
  });
});

describe("voiceState — isActiveCommand", () => {
  test("PROCESSING, AWAITING_CONFIRMATION, SPEAKING are active command states", () => {
    expect(isActiveCommand(S.PROCESSING)).toBe(true);
    expect(isActiveCommand(S.AWAITING_CONFIRMATION)).toBe(true);
    expect(isActiveCommand(S.SPEAKING)).toBe(true);
  });

  test("STANDBY, LISTENING, RECOVERING, DISABLED are not active command states", () => {
    expect(isActiveCommand(S.STANDBY)).toBe(false);
    expect(isActiveCommand(S.LISTENING)).toBe(false);
    expect(isActiveCommand(S.RECOVERING)).toBe(false);
    expect(isActiveCommand(S.DISABLED)).toBe(false);
  });
});

describe("voiceState — badge", () => {
  test("badge text and tone for each state (English)", () => {
    expect(voiceStateBadge(S.STANDBY, "en")).toEqual({
      key: "standby",
      text: "READY",
      tone: "active",
    });
    expect(voiceStateBadge(S.LISTENING, "en")).toEqual({
      key: "listening",
      text: "REC",
      tone: "active",
    });
    expect(voiceStateBadge(S.PROCESSING, "en")).toEqual({
      key: "processing",
      text: "WORK",
      tone: "busy",
    });
    expect(voiceStateBadge(S.AWAITING_CONFIRMATION, "en")).toEqual({
      key: "awaiting_confirmation",
      text: "WAIT",
      tone: "wait",
    });
    expect(voiceStateBadge(S.SPEAKING, "en")).toEqual({
      key: "speaking",
      text: "TTS",
      tone: "busy",
    });
    expect(voiceStateBadge(S.RECOVERING, "en")).toEqual({
      key: "recovering",
      text: "ERR",
      tone: "error",
    });
    expect(voiceStateBadge(S.DISABLED, "en")).toEqual({
      key: "disabled",
      text: "",
      tone: "idle",
    });
  });

  test("badge text and tone for each state (Arabic)", () => {
    expect(voiceStateBadge(S.STANDBY, "ar")).toEqual({
      key: "standby",
      text: "جاهز",
      tone: "active",
    });
    expect(voiceStateBadge(S.LISTENING, "ar")).toEqual({
      key: "listening",
      text: "أسجل",
      tone: "active",
    });
    expect(voiceStateBadge(S.PROCESSING, "ar")).toEqual({
      key: "processing",
      text: "أعمل",
      tone: "busy",
    });
    expect(voiceStateBadge(S.AWAITING_CONFIRMATION, "ar")).toEqual({
      key: "awaiting_confirmation",
      text: "موافقة",
      tone: "wait",
    });
    expect(voiceStateBadge(S.SPEAKING, "ar")).toEqual({
      key: "speaking",
      text: "أتكلّم",
      tone: "busy",
    });
    expect(voiceStateBadge(S.RECOVERING, "ar")).toEqual({
      key: "recovering",
      text: "خطأ",
      tone: "error",
    });
    expect(voiceStateBadge(S.DISABLED, "ar")).toEqual({
      key: "disabled",
      text: "",
      tone: "idle",
    });
  });
});

describe("voiceState — capture gate with speaking", () => {
  test("canCapture returns false when speaking is true", () => {
    expect(canCapture(S.STANDBY, { speaking: true })).toBe(false);
    expect(canCapture(S.LISTENING, { speaking: true })).toBe(false);
  });

  test("canCapture returns true when speaking is false/absent", () => {
    expect(canCapture(S.STANDBY)).toBe(true);
    expect(canCapture(S.LISTENING)).toBe(true);
  });
});