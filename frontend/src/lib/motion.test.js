/**
 * Motion: the entrance contract.
 *
 * Every entrance animation in the product goes through these two functions,
 * so these tests are the place where "does it ever gate content", "does it
 * ever touch layout" and "what happens when GSAP is absent" are answered
 * once instead of component by component.
 *
 * The assertions that matter are the *refusals*: no layout property appears
 * in either keyframe object, a missing selector or a throwing GSAP still
 * yields a callable disposer, and reduced motion produces no tween at all
 * rather than a tween that is somehow skipped afterwards.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { gsap } from "gsap";
import { enter, enterOverlay, prefersReducedMotion } from "./motion";

/** Properties whose animation forces layout or a repaint of the page. */
const LAYOUT_PROPERTIES = ["width", "height", "top", "left", "right", "bottom", "margin", "padding"];

describe("motion", () => {
  let fromTo;

  beforeEach(() => {
    fromTo = vi.spyOn(gsap, "fromTo").mockReturnValue({ kill: vi.fn() });
    delete window.matchMedia;
    document.body.innerHTML = "";
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete window.matchMedia;
    document.body.innerHTML = "";
  });

  function mount() {
    const node = document.createElement("div");
    document.body.appendChild(node);
    return node;
  }

  /** Pretend the user asked the system to stop moving things. */
  function reducedMotion(matches) {
    window.matchMedia = (query) => ({ matches, media: query });
  }

  describe("enter", () => {
    it("moves opacity and transform only — never layout", () => {
      const node = mount();
      enter(node, { y: 12, scale: 0.9, duration: 0.3 });

      expect(fromTo).toHaveBeenCalledTimes(1);
      const [targets, from, to] = fromTo.mock.calls[0];
      expect(targets).toEqual([node]);
      expect(Object.keys(from).sort()).toEqual(["opacity", "scale", "x", "y"]);
      expect(to.clearProps).toBe("opacity,transform");
      expect(to.duration).toBe(0.3);

      const serialised = JSON.stringify([from, to]);
      for (const prop of LAYOUT_PROPERTIES) {
        expect(serialised).not.toContain(`"${prop}"`);
      }
    });

    it("returns a disposer that kills the tween and clears the styles", () => {
      const kill = vi.fn();
      fromTo.mockReturnValue({ kill });
      const node = mount();
      // What a half-finished tween leaves behind — which is exactly what must
      // not survive, or a remounted panel would come back invisible.
      node.style.opacity = "0";
      node.style.transform = "translateY(12px)";

      const stop = enter(node);
      stop();

      expect(kill).toHaveBeenCalledTimes(1);
      expect(node.style.opacity).toBe("");
      expect(node.style.transform).toBe("");
    });

    it("animates nothing under reduced motion, and still returns a disposer", () => {
      reducedMotion(true);
      const stop = enter(mount());

      expect(fromTo).not.toHaveBeenCalled();
      expect(typeof stop).toBe("function");
      expect(() => stop()).not.toThrow();
    });

    it("accepts a selector and staggers a collection", () => {
      document.body.innerHTML = '<div class="row"></div><div class="row"></div>';
      enter(".row", { stagger: 0.04, y: 6 });

      expect(fromTo).toHaveBeenCalledTimes(1);
      expect(fromTo.mock.calls[0][0]).toHaveLength(2);
      expect(fromTo.mock.calls[0][2].stagger).toBe(0.04);
    });

    it("returns a disposer, and animates nothing, when the selector is empty", () => {
      const stop = enter(".there-is-no-such-class");

      expect(fromTo).not.toHaveBeenCalled();
      expect(() => stop()).not.toThrow();
    });

    it("returns a disposer when GSAP itself refuses to run", () => {
      fromTo.mockImplementation(() => {
        throw new Error("no gsap here");
      });
      const stop = enter(mount());

      expect(typeof stop).toBe("function");
      expect(() => stop()).not.toThrow();
    });
  });

  describe("enterOverlay", () => {
    it("fades the scrim and moves the panel, in that proportion", () => {
      const scrim = mount();
      const panel = mount();

      enterOverlay(scrim, panel, { duration: 0.24 });

      expect(fromTo).toHaveBeenCalledTimes(2);
      const [scrimCall, panelCall] = fromTo.mock.calls;

      expect(scrimCall[0]).toEqual([scrim]);
      expect(panelCall[0]).toEqual([panel]);

      // The scrim covers the viewport: translating it would open a gap at the
      // edge and flash whatever sits behind the overlay mid-transition.
      expect(scrimCall[1]).toMatchObject({ y: 0, scale: 1 });
      expect(scrimCall[2].duration).toBeLessThan(panelCall[2].duration);

      // The motion belongs to the panel.
      expect(panelCall[1]).toMatchObject({ y: 10, scale: 0.985 });
      expect(panelCall[2]).toMatchObject({ duration: 0.24 });
    });

    it("disposes both tweens through one returned function", () => {
      const scrimKill = vi.fn();
      const panelKill = vi.fn();
      fromTo.mockReturnValueOnce({ kill: scrimKill }).mockReturnValueOnce({ kill: panelKill });

      const stop = enterOverlay(mount(), mount());
      stop();

      expect(scrimKill).toHaveBeenCalledTimes(1);
      expect(panelKill).toHaveBeenCalledTimes(1);
    });

    it("disposes cleanly when reduced motion meant nothing started", () => {
      reducedMotion(true);
      const stop = enterOverlay(mount(), mount());

      expect(fromTo).not.toHaveBeenCalled();
      expect(() => stop()).not.toThrow();
    });
  });

  describe("prefersReducedMotion", () => {
    it("reads the preference rather than assuming one", () => {
      reducedMotion(true);
      expect(prefersReducedMotion()).toBe(true);

      reducedMotion(false);
      expect(prefersReducedMotion()).toBe(false);

      delete window.matchMedia;
      expect(prefersReducedMotion()).toBe(false);
    });
  });
});
