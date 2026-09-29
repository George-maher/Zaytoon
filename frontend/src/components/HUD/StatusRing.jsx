import { memo, useEffect, useRef } from "react";
import gsap from "gsap";

/**
 * StatusRing - Animated ring showing current status
 */
export default memo(function StatusRing({ status = "idle", size = 220 }) {
  const ringRef = useRef(null);
  const dashRef = useRef(null);

  useEffect(() => {
    if (!dashRef.current) return;
    const ctx = gsap.context(() => {
      const speed = {
        idle: 12,
        connected: 10,
        listening: 4,
        thinking: 1.5,
        speaking: 3,
        awaiting_confirmation: 2,
        awaiting_command: 6,
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
      case "listening": return "#00FF88";
      case "thinking": return "#00D4FF";
      case "speaking": return "#00F0FF";
      case "awaiting_confirmation": return "#FFB347";
      case "awaiting_command": return "#FFB347";
      case "error": return "#FF3355";
      case "connected": return "#00D4FF";
      default: return "#00D4FF";
    }
  };

  const getGlowIntensity = () => {
    switch (status) {
      case "listening": return "0 0 20px rgba(0,255,136,0.3)";
      case "thinking": return "0 0 25px rgba(0,212,255,0.4)";
      case "speaking": return "0 0 20px rgba(0,240,255,0.3)";
      case "awaiting_confirmation": return "0 0 25px rgba(255,179,71,0.4)";
      case "error": return "0 0 20px rgba(255,51,85,0.4)";
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
});