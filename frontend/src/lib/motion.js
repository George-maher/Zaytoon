import { gsap } from "gsap";

/**
 * Motion — the one place an element animates into view.
 *
 * `ViewTransition` already owns the view-to-view move; this module owns the
 * other case: something that was not on screen a moment ago and now is
 * (palette, settings, the approval prompt, a panel's contents). Keeping both
 * in one vocabulary is the point — two files each with their own tween
 * settings, easing and reduced-motion check is how a product ends up with
 * three different entrance speeds and a "respects reduced motion" claim that
 * one of them quietly stopped honouring.
 *
 * The contract, in the order it matters:
 *
 * - **It never gates content.** The element is in the DOM before this is
 *   called and remains fully interactive throughout. Only `opacity` and
 *   `transform` move — never layout, never `visibility`, never `pointer-events`
 *   — so nothing here can delay mounting, focus, a keystroke or a click on
 *   "ALLOW". The animation runs *over* a view that is already usable.
 * - **It always returns a disposer**, and returns one even when it animated
 *   nothing, so every caller has exactly one cleanup shape. Interrupted
 *   mid-flight — a prompt answered while it was still fading in — the inline
 *   styles are cleared rather than left at whatever frame the tween reached.
 *   A leftover `opacity: 0` on a dismissed-then-remounted panel is the exact
 *   failure this makes impossible.
 * - **Reduced motion and a missing GSAP are the same answer.** No animation,
 *   no error, no inline style left behind.
 */

/** Reduced motion is a preference, not a suggestion. */
export function prefersReducedMotion() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Accept an element, a selector, or a collection of either.
 *
 * Anything unrecognised yields nothing rather than throwing: a selector that
 * no longer matches after a refactor should cost an absent animation, not a
 * crash inside an effect.
 */
function toElements(target) {
  if (!target) return [];
  if (typeof target === "string") {
    if (typeof document === "undefined") return [];
    return Array.from(document.querySelectorAll(target));
  }
  if (target.nodeType) return [target];
  if (typeof target.length === "number") return Array.from(target);
  return [];
}

const DISPOSE_NOTHING = () => {};

/**
 * Fade/rise `target` in.
 *
 * `y` and `scale` are the *from* offsets; they resolve to 0 / 1. Passing
 * `y: 0, scale: 1` therefore produces a pure fade, which is what a
 * full-viewport scrim wants — translating a layer that covers the whole
 * screen would open a gap at the edge and flash what is behind it.
 */
export function enter(target, opts = {}) {
  const {
    x = 0,
    y = 8,
    scale = 1,
    duration = 0.22,
    ease = "power2.out",
    delay = 0,
    stagger = 0,
  } = opts;

  const els = toElements(target);
  if (!els.length || prefersReducedMotion()) return DISPOSE_NOTHING;

  let tween;
  try {
    tween = gsap.fromTo(
      els,
      { opacity: 0, x, y, scale },
      {
        opacity: 1,
        x: 0,
        y: 0,
        scale: 1,
        duration,
        ease,
        delay,
        stagger,
        // Leave no inline styles behind on completion, or the element would
        // keep the tween's last frame and stop responding to the stylesheet.
        clearProps: "opacity,transform",
      },
    );
  } catch {
    // GSAP missing or a hostile host: the content is already rendered, so
    // doing nothing is the correct failure mode.
    return DISPOSE_NOTHING;
  }

  return () => {
    try {
      tween.kill();
    } catch {
      // Already finished; there is nothing left to kill.
    }
    els.forEach((el) => {
      el.style.opacity = "";
      el.style.transform = "";
    });
  };
}

/**
 * An overlay: a scrim plus the panel sitting on it.
 *
 * Two elements, two jobs, one call — because getting it wrong is invisible in
 * a screenshot and obvious in use: the scrim covers the viewport, so it only
 * fades, while the panel is where the motion belongs. Returns one disposer
 * for both, so a caller's cleanup stays a single line.
 */
export function enterOverlay(scrim, panel, opts = {}) {
  const { duration = 0.2, y = 10, scale = 0.985 } = opts;
  const stopScrim = enter(scrim, { y: 0, scale: 1, duration: duration * 0.75 });
  const stopPanel = enter(panel, { y, scale, duration });
  return () => {
    stopScrim();
    stopPanel();
  };
}
