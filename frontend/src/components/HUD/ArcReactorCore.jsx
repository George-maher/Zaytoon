import { memo, useEffect, useRef } from "react";
import gsap from "gsap";

/**
 * ArcReactorCore - The central visual element representing Zaytoon's "brain"
 * Animated rings with status-based color changes
 */
export default memo(function ArcReactorCore({ status = "idle", size = 200 }) {
  const glowRef = useRef(null);
  const ringsRef = useRef([]);

  useEffect(() => {
    const ctx = gsap.context(() => {
      // Rotate rings at different speeds
      ringsRef.current.forEach((ring, i) => {
        if (!ring.current) return;
        gsap.to(ring.current, {
          rotation: i % 2 === 0 ? 360 : -360,
          duration: 20 + i * 10,
          repeat: -1,
          ease: "none",
        });
      });
    }, ringsRef.current);
    return () => ctx.revert();
  }, []);

  useEffect(() => {
    if (!glowRef.current) return;
    const ctx = gsap.context(() => {
      if (status === "thinking") {
        gsap.to(glowRef.current, {
          scale: 1.15,
          opacity: 0.9,
          duration: 0.4,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
      } else if (status === "speaking") {
        gsap.to(glowRef.current, {
          scale: 1.08,
          opacity: 0.7,
          duration: 0.8,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
      } else if (status === "listening") {
        gsap.to(glowRef.current, {
          scale: 1.05,
          opacity: 0.6,
          duration: 1.5,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
      } else if (status === "awaiting_confirmation") {
        gsap.to(glowRef.current, {
          scale: 1.1,
          opacity: 0.8,
          duration: 0.5,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
      } else {
        gsap.to(glowRef.current, {
          scale: 1,
          opacity: 0.4,
          duration: 2,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
      }
    }, glowRef);
    return () => ctx.revert();
  }, [status]);

  const getGlowColor = () => {
    switch (status) {
      case "listening": return "rgba(0, 255, 136, 0.6)";
      case "thinking": return "rgba(0, 212, 255, 0.8)";
      case "speaking": return "rgba(0, 240, 255, 0.7)";
      case "awaiting_confirmation": return "rgba(255, 179, 71, 0.7)";
      case "error": return "rgba(255, 51, 85, 0.7)";
      default: return "rgba(0, 212, 255, 0.4)";
    }
  };

  const getCoreColor = () => {
    switch (status) {
      case "listening": return "#00FF88";
      case "thinking": return "#00D4FF";
      case "speaking": return "#00F0FF";
      case "awaiting_confirmation": return "#FFB347";
      case "error": return "#FF3355";
      default: return "#00D4FF";
    }
  };

  return (
    <div className="relative" style={{ width: size, height: size }}>
      {/* Outer glow */}
      <div
        ref={glowRef}
        className="absolute inset-0 rounded-full"
        style={{
          background: `radial-gradient(circle, ${getGlowColor()} 0%, transparent 70%)`,
          filter: "blur(24px)",
          opacity: 0.6,
        }}
      />

      {/* Rotating rings */}
      <div className="absolute inset-0" style={{ transformStyle: "preserve-3d" }}>
        {[0, 1, 2, 3].map((i) => (
          <svg
            key={i}
            ref={(el) => { ringsRef.current[i] = el; }}
            className="absolute inset-0 w-full h-full"
            viewBox="0 0 200 200"
            style={{
              filter: i === 0 ? "blur(0.5px)" : "none",
              opacity: 0.8 - i * 0.15,
            }}
          >
            <circle
              cx="100" cy="100" r={90 - i * 15}
              fill="none"
              stroke="rgba(0, 212, 255, 0.08)"
              strokeWidth="0.5"
            />
            <circle
              cx="100" cy="100" r={90 - i * 15}
              fill="none"
              stroke="#00D4FF"
              strokeWidth={1}
              strokeDasharray="8 180"
              opacity="0.4"
            />
            <circle
              cx="100" cy="100" r={90 - i * 15}
              fill="none"
              stroke="#00D4FF"
              strokeWidth="0.5"
              strokeDasharray="3 250"
              strokeDashoffset={i * 60}
              opacity="0.3"
            />
          </svg>
        ))}
      </div>

      {/* Core */}
      <div className="relative z-10 flex items-center justify-center" style={{
        width: size * 0.35,
        height: size * 0.35,
        background: `radial-gradient(circle, ${getCoreColor()}22 0%, transparent 70%)`,
        border: `2px solid ${getCoreColor()}55`,
        borderRadius: "50%",
        boxShadow: `0 0 30px ${getCoreColor()}33, inset 0 0 20px ${getCoreColor()}11`,
      }}>
        <div style={{
          width: "60%",
          height: "60%",
          borderRadius: "50%",
          background: `radial-gradient(circle, ${getCoreColor()}33 0%, transparent 70%)`,
          border: `1px solid ${getCoreColor()}44`,
          boxShadow: `0 0 15px ${getCoreColor()}22`,
        }}>
          <span style={{
            color: getCoreColor(),
            textShadow: `0 0 12px ${getCoreColor()}88`,
            fontSize: "1.5rem",
            fontWeight: 700,
            fontFamily: "Orbitron, monospace",
          }}>Z</span>
        </div>
      </div>

      {/* Crosshair lines */}
      <svg className="absolute inset-0 w-full h-full pointer-events-none" viewBox="0 0 200 200">
        <line x1="100" y1="0" x2="100" y2="35" stroke={getCoreColor()} strokeWidth="0.3" opacity="0.2" />
        <line x1="100" y1="165" x2="100" y2="200" stroke={getCoreColor()} strokeWidth="0.3" opacity="0.2" />
        <line x1="0" y1="100" x2="35" y2="100" stroke={getCoreColor()} strokeWidth="0.3" opacity="0.2" />
        <line x1="165" y1="100" x2="200" y2="100" stroke={getCoreColor()} strokeWidth="0.3" opacity="0.2" />
      </svg>
    </div>
  );
});