import { useEffect, useRef } from "react";
import gsap from "gsap";

export default function StatusRing({ status = "idle", size = 220 }) {
  const ringRef = useRef(null);
  const dashRef = useRef(null);

  useEffect(() => {
    if (!dashRef.current) return;
    const ctx = gsap.context(() => {
      const speed = {
        idle: 12,
        listening: 4,
        thinking: 1.5,
        speaking: 3,
      }[status] || 12;

      gsap.to(dashRef.current, {
        strokeDashoffset: -1000,
        duration: speed,
        repeat: -1,
        ease: "none",
      });
    }, ringRef);
    return () => ctx.revert();
  }, [status]);

  const getColor = () => {
    switch (status) {
      case "listening": return "#00ff88";
      case "thinking": return "#00d4ff";
      case "speaking": return "#00f0ff";
      default: return "#00d4ff";
    }
  };

  const getGlowIntensity = () => {
    switch (status) {
      case "listening": return "0 0 20px rgba(0,255,136,0.3)";
      case "thinking": return "0 0 25px rgba(0,212,255,0.4)";
      case "speaking": return "0 0 20px rgba(0,240,255,0.3)";
      default: return "0 0 10px rgba(0,212,255,0.1)";
    }
  };

  const r = size / 2 - 4;
  const circumference = 2 * Math.PI * r;

  return (
    <div ref={ringRef} className="absolute inset-0 flex items-center justify-center pointer-events-none"
      style={{ width: size, height: size }}>
      <svg width={size} height={size} className="absolute" style={{ filter: `drop-shadow(${getGlowIntensity()})` }}>
        {/* Base ring */}
        <circle cx={size/2} cy={size/2} r={r} fill="none"
          stroke="rgba(0,212,255,0.06)" strokeWidth="1" />
        {/* Animated ring */}
        <circle ref={dashRef} cx={size/2} cy={size/2} r={r} fill="none"
          stroke={getColor()} strokeWidth="1.5"
          strokeDasharray={`${circumference * 0.15} ${circumference * 0.85}`}
          strokeLinecap="round" opacity="0.6" />
      </svg>
    </div>
  );
}
