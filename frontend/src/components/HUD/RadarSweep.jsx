import { useEffect, useRef } from "react";
import gsap from "gsap";

export default function RadarSweep({ status = "idle" }) {
  const sweepRef = useRef(null);
  const containerRef = useRef(null);

  useEffect(() => {
    const ctx = gsap.context(() => {
      gsap.to(sweepRef.current, {
        rotation: 360,
        duration: status === "thinking" ? 3 : 6,
        repeat: -1,
        ease: "none",
      });
    }, containerRef);
    return () => ctx.revert();
  }, [status]);

  const getOpacity = () => {
    switch (status) {
      case "listening": return 0.25;
      case "thinking": return 0.35;
      case "speaking": return 0.2;
      default: return 0.12;
    }
  };

  return (
    <div ref={containerRef} className="absolute inset-0 pointer-events-none">
      <svg className="w-full h-full" viewBox="0 0 400 400" style={{ opacity: getOpacity() }}>
        {/* Concentric rings */}
        <circle cx="200" cy="200" r="190" fill="none" stroke="#00d4ff" strokeWidth="0.3" strokeDasharray="4 8" />
        <circle cx="200" cy="200" r="150" fill="none" stroke="#00d4ff" strokeWidth="0.3" strokeDasharray="3 10" />
        <circle cx="200" cy="200" r="110" fill="none" stroke="#00d4ff" strokeWidth="0.3" strokeDasharray="2 12" />
        <circle cx="200" cy="200" r="70" fill="none" stroke="#00d4ff" strokeWidth="0.3" strokeDasharray="2 14" />

        {/* Cross lines */}
        <line x1="200" y1="10" x2="200" y2="390" stroke="#00d4ff" strokeWidth="0.2" opacity="0.3" />
        <line x1="10" y1="200" x2="390" y2="200" stroke="#00d4ff" strokeWidth="0.2" opacity="0.3" />
        <line x1="60" y1="60" x2="340" y2="340" stroke="#00d4ff" strokeWidth="0.15" opacity="0.2" />
        <line x1="340" y1="60" x2="60" y2="340" stroke="#00d4ff" strokeWidth="0.15" opacity="0.2" />

        {/* Sweep beam */}
        <g ref={sweepRef} style={{ transformOrigin: "200px 200px" }}>
          <defs>
            <linearGradient id="sweepGrad" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stopColor="#00d4ff" stopOpacity="0" />
              <stop offset="100%" stopColor="#00d4ff" stopOpacity="0.4" />
            </linearGradient>
          </defs>
          <path d="M200,200 L200,10 A190,190 0 0,1 368,65 Z" fill="url(#sweepGrad)" />
          <line x1="200" y1="200" x2="200" y2="10" stroke="#00d4ff" strokeWidth="0.8" opacity="0.6" />
        </g>
      </svg>
    </div>
  );
}
