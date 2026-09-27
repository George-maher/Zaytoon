import { useEffect, useRef } from "react";
import gsap from "gsap";

export default function useGsapTimeline(deps = []) {
  const ref = useRef(null);
  const timelineRef = useRef(null);

  useEffect(() => {
    if (!ref.current) return;
    const ctx = gsap.context(() => {
      timelineRef.current = gsap.timeline();
    }, ref);
    return () => {
      ctx.revert();
      timelineRef.current = null;
    };
  }, deps);

  return { ref, timeline: timelineRef };
}

export function useGsapFadeIn(delay = 0) {
  const ref = useRef(null);

  useEffect(() => {
    if (!ref.current) return;
    const ctx = gsap.context(() => {
      gsap.fromTo(ref.current,
        { opacity: 0, y: 12 },
        { opacity: 1, y: 0, duration: 0.5, delay, ease: "power2.out" }
      );
    }, ref);
    return () => ctx.revert();
  }, [delay]);

  return ref;
}
