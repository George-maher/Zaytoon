import { useLayoutEffect, useRef } from "react";
import { gsap } from "gsap";
import { prefersReducedMotion } from "../lib/motion";

/**
 * ViewTransition — the one place a change of view is animated.
 *
 * The contract, in order of importance:
 *
 * - **It never delays mounting.** Children are rendered on the first paint
 *   and stay in the DOM throughout; the animation runs *over* content that is
 *   already there. A transition that gated rendering would make every screen
 *   depend on a timer, and a test that reads the DOM 100ms after a click
 *   would be racing a tween. Only transforms and opacity move — never layout,
 *   never visibility.
 * - **The first paint is not a transition.** App runs its own entrance; this
 *   component only animates a real change of `viewKey`, so Home does not fade
 *   in under the user on load.
 * - **Reduced motion is honoured**, and so is a missing GSAP: either one
 *   means the view simply appears, because the view was already there.
 *
 * The wrapper is a plain `div` that keeps its own layout classes, so the
 * children inside it (`flex-1`, `min-h-0`) behave exactly as they did when
 * they were mounted directly.
 */
export default function ViewTransition({ viewKey, className, style, children }) {
  const ref = useRef(null);
  const mounted = useRef(false);

  useLayoutEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return undefined;
    }
    const el = ref.current;
    if (!el || prefersReducedMotion()) return undefined;

    let tween;
    try {
      tween = gsap.fromTo(
        el,
        { opacity: 0, y: 8 },
        {
          opacity: 1,
          y: 0,
          duration: 0.26,
          ease: "power2.out",
          // Leave no inline styles behind, or the wrapper would keep the
          // tween's last frame forever and stop responding to the stylesheet.
          clearProps: "opacity,transform",
        },
      );
    } catch {
      // GSAP missing or hostile host: the content is already rendered, so
      // doing nothing is the correct failure mode.
      return undefined;
    }
    // Killed mid-flight by a faster navigation, the wrapper would be left at
    // whatever frame it reached — so put it back before letting go.
    return () => {
      tween.kill();
      el.style.opacity = "";
      el.style.transform = "";
    };
  }, [viewKey]);

  return (
    <div ref={ref} className={className} style={style}>
      {children}
    </div>
  );
}
