/**
 * UI-6 / UI-9 accessibility: non-visual announcements.
 *
 * Every announcement is published to an aria-live region (screen readers) and, when the
 * user has switched voice on, spoken with the browser's Web Speech API. If speech is not
 * available, the text announcement still happens (UI-6 fallback).
 */
type Listener = (text: string) => void;

const listeners = new Set<Listener>();
let voiceEnabled = false;

export const speechSupported = (): boolean =>
  typeof window !== "undefined" && "speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined";

export function initialVoiceEnabled(): boolean {
  try {
    return window.localStorage.getItem("srems.voice") === "1";
  } catch {
    return false;
  }
}

export function setVoiceEnabled(on: boolean): void {
  voiceEnabled = on;
  try {
    window.localStorage.setItem("srems.voice", on ? "1" : "0");
  } catch {
    /* storage unavailable: keep the in-memory setting */
  }
  if (!on && speechSupported()) window.speechSynthesis.cancel();
}

/** Spoken form of a floor number: B1 is read as "basement 1". */
export const spokenFloor = (floor: number | null | undefined): string =>
  floor === null || floor === undefined ? "unknown floor" : floor === -1 ? "basement 1" : `floor ${floor}`;

/**
 * Publish an announcement. ``replace`` drops speech that is still queued (used for fast
 * floor-by-floor updates so the voice never lags behind the car).
 */
export function announce(text: string, options: { replace?: boolean; urgent?: boolean } = {}): void {
  listeners.forEach((listener) => listener(text));
  if (!voiceEnabled || !speechSupported()) return;
  try {
    if (options.replace || options.urgent) window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "en-US";
    utterance.rate = options.urgent ? 0.95 : 1.05;
    window.speechSynthesis.speak(utterance);
  } catch {
    /* speech failed: the aria-live text above is the fallback */
  }
}

export function subscribeAnnouncements(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
