import { memo, useEffect, useRef } from "react";
import gsap from "gsap";

/**
 * RadarSweep - Rotating radar sweep effect behind the orb
 */
function RadarSweep({ status = "idle", size = 500 }) {
  const containerRef = useRef(null);
  const sweepRef = useRef(null);

  useEffect(() => {
    if (!sweepRef.current) return;
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

      gsap.to(sweepRef.current, {
        rotation: 360,
        duration: speed,
        repeat: -1,
        ease: "none",
        transformOrigin: "center center",
      });
    }, containerRef);
    return () => ctx.revert();
  }, [status]);

  return (
    <div ref={containerRef} className="relative w-full h-full flex items-center justify-center pointer-events-none"
      style={{ width: size, height: size }}>
      <div className="absolute inset-0" style={{ borderRadius: "50%" }}>
        {/* Radar background rings */}
        <svg className="absolute inset-0 w-full h-full" viewBox="0 0 500 500">
          {[1, 2, 3, 4, 5].map((i) => (
            <circle
              key={i}
              cx="250" cy="250"
              r={220 - i * 40}
              fill="none"
              stroke="rgba(0, 212, 255, 0.03)"
              strokeWidth="0.5"
            />
          ))}
          {/* Quadrant lines */}
          <line x1="250" y1="0" x2="250" y2="500" stroke="rgba(0,212,255,0.05)" strokeWidth="0.5" />
          <line x1="0" y1="250" x2="500" y2="250" stroke="rgba(0,212,255,0.05)" strokeWidth="0.5" />
          <line x1="79" y1="79" x2="421" y2="421" stroke="rgba(0,212,255,0.03)" strokeWidth="0.5" />
          <line x1="421" y1="79" x2="79" y2="421" stroke="rgba(0,212,255,0.03)" strokeWidth="0.5" />
        </svg>

        {/* Sweep beam */}
        <div ref={sweepRef} className="absolute" style={{
          width: "100%",
          height: "100%",
          transformOrigin: "center center",
        }}>
          <svg className="w-full h-full" viewBox="0 0 500 500">
            <defs>
              <radialGradient id="sweepGradient" cx="50%" cy="50%" r="50%">
                <stop offset="0%" stopColor={status === "listening" ? "#00FF88" : status === "thinking" ? "#00D4FF" : "#00D4FF"} stopOpacity="0.3" />
                <stop offset="70%" stopColor={status === "listening" ? "#00FF88" : "#00D4FF"} stopOpacity="0.1" />
                <stop offset="100%" stopColor="transparent" stopOpacity="0" />
              </radialGradient>
            </defs>
            <path
              d="M250,250 L250,30 A220,220 0 0,1 250,470 Z"
              fill="url(#sweepGradient)"
              opacity="0.6"
            />
          </svg>
        </div>

        {/* Center pulse */}
        <div className="absolute" style={{
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: "80px",
          height: "80px",
          borderRadius: "50%",
          background: `radial-gradient(circle, ${status === "listening" ? "rgba(0,255,136,0.2)" : status === "thinking" ? "rgba(0,212,255,0.15)" : "rgba(0,212,255,0.1)"} 0%, transparent 70%)`,
          filter: "blur(20px)",
          animation: "pulse-ring 2s ease-out infinite",
        }} />
      </div>
    </div>
  );
}

export default memo(RadarSweep);