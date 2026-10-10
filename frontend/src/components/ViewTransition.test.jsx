/**
 * ViewTransition must never gate rendering on an animation.
 *
 * The contract that matters is the failure mode: if GSAP is slow, missing or
 * the user has asked for reduced motion, the screen still has to be there.
 * So every assertion here is about what is in the DOM and when, and about
 * the tween being *skipped* rather than awaited.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { gsap } from "gsap";
import ViewTransition from "./ViewTransition";

describe("ViewTransition", () => {
  let fromTo;

  beforeEach(() => {
    fromTo = vi.spyOn(gsap, "fromTo").mockReturnValue({ kill: () => {} });
    delete window.matchMedia;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete window.matchMedia;
  });

  it("renders its children immediately, with no waiting on a tween", () => {
    render(
      <ViewTransition viewKey="home" className="shell">
        <p>hello</p>
      </ViewTransition>,
    );
    // Synchronously present — a transition that delayed mounting would make
    // every screen race a timer.
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(fromTo).not.toHaveBeenCalled();
  });

  it("does not animate the first paint: that is App's entrance, not a change", () => {
    render(
      <ViewTransition viewKey="home">
        <p>home</p>
      </ViewTransition>,
    );
    expect(fromTo).not.toHaveBeenCalled();
  });

  it("animates a real change of view over content that is already mounted", () => {
    const { rerender } = render(
      <ViewTransition viewKey="home" className="shell">
        <p>home</p>
      </ViewTransition>,
    );
    rerender(
      <ViewTransition viewKey="workspace" className="shell">
        <p>workspace</p>
      </ViewTransition>,
    );
    expect(screen.getByText("workspace")).toBeInTheDocument();
    expect(fromTo).toHaveBeenCalledTimes(1);
    // Only transform and opacity — never layout, never visibility.
    expect(fromTo.mock.calls[0][1]).toEqual({ opacity: 0, y: 8 });
    expect(fromTo.mock.calls[0][2]).toMatchObject({ opacity: 1, y: 0 });
  });

  it("leaves no inline styles behind for the stylesheet to fight", () => {
    const { rerender } = render(
      <ViewTransition viewKey="home">
        <p>home</p>
      </ViewTransition>,
    );
    rerender(
      <ViewTransition viewKey="tasks">
        <p>tasks</p>
      </ViewTransition>,
    );
    expect(fromTo.mock.calls[0][2].clearProps).toBe("opacity,transform");
  });

  it("honours prefers-reduced-motion: the view simply appears", () => {
    window.matchMedia = (query) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      addEventListener() {},
      removeEventListener() {},
    });
    const { rerender } = render(
      <ViewTransition viewKey="home">
        <p>home</p>
      </ViewTransition>,
    );
    rerender(
      <ViewTransition viewKey="workspace">
        <p>workspace</p>
      </ViewTransition>,
    );
    expect(screen.getByText("workspace")).toBeInTheDocument();
    expect(fromTo).not.toHaveBeenCalled();
  });

  it("falls back to an unstyled view when GSAP refuses to run", () => {
    fromTo.mockImplementation(() => {
      throw new Error("no gsap here");
    });
    const { rerender } = render(
      <ViewTransition viewKey="home">
        <p>home</p>
      </ViewTransition>,
    );
    rerender(
      <ViewTransition viewKey="workspace">
        <p>workspace</p>
      </ViewTransition>,
    );
    // The content was never hidden, so a broken animation costs nothing.
    expect(screen.getByText("workspace")).toBeInTheDocument();
  });

  it("keeps the wrapper's layout classes so children size as before", () => {
    render(
      <ViewTransition viewKey="home" className="flex-1 flex flex-col min-h-0">
        <p>home</p>
      </ViewTransition>,
    );
    expect(document.querySelector(".flex-1.flex.flex-col.min-h-0")).toBeInTheDocument();
  });
});
