import { useEffect, useRef } from "react";
import gsap from "gsap";

export default function ArcReactorCore({ status = "idle" }) {
  const containerRef = useRef(null);
  const coreRef = useRef(null);
  const ring1Ref = useRef(null);
  const ring2Ref = useRef(null);
  const ring3Ref = useRef(null);
  const glowRef = useRef(null);

  useEffect(() => {
    const ctx = gsap.context(() => {
      gsap.to(ring1Ref.current, {
        rotation: 360,
        duration: 20,
        repeat: -1,
        ease: "none",
      });
      gsap.to(ring2Ref.current, {
        rotation: -360,
        duration: 30,
        repeat: -1,
        ease: "none",
      });
      gsap.to(ring3Ref.current, {
        rotation: 360,
        duration: 45,
        repeat: -1,
        ease: "none",
      });
    }, containerRef);
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
      default: return "rgba(0, 212, 255, 0.4)";
    }
  };

  const getCoreColor = () => {
    switch (status) {
      case "listening": return "#00ff88";
      case "thinking": return "#00d4ff";
      case "speaking": return "#00f0ff";
      default: return "#00d4ff";
    }
  };

  return (
    <div ref={containerRef} className="relative w-48 h-48 flex items-center justify-center">
      {/* Outer glow */}
      <div ref={glowRef} className="absolute inset-0 rounded-full" style={{
        background: `radial-gradient(circle, ${getGlowColor()} 0%, transparent 70%)`,
        filter: "blur(20px)",
      }} />

      {/* Outer ring */}
      <svg ref={ring3Ref} className="absolute inset-0 w-full h-full" viewBox="0 0 200 200">
        <circle cx="100" cy="100" r="95" fill="none" stroke="rgba(0,212,255,0.08)" strokeWidth="0.5" />
        <circle cx="100" cy="100" r="95" fill="none" stroke={getCoreColor()} strokeWidth="1"
          strokeDasharray="8 180" opacity="0.4" />
        <circle cx="100" cy="100" r="95" fill="none" stroke={getCoreColor()} strokeWidth="0.5"
          strokeDasharray="3 250" strokeDashoffset="60" opacity="0.3" />
      </svg>

      {/* Middle ring */}
      <svg ref={ring2Ref} className="absolute inset-3 w-[calc(100%-24px)] h-[calc(100%-24px)]" viewBox="0 0 200 200">
        <circle cx="100" cy="100" r="88" fill="none" stroke="rgba(0,212,255,0.1)" strokeWidth="0.5" />
        <circle cx="100" cy="100" r="88" fill="none" stroke={getCoreColor()} strokeWidth="1.5"
          strokeDasharray="12 140" opacity="0.35" />
        <circle cx="100" cy="100" r="80" fill="none" stroke="rgba(0,240,255,0.06)" strokeWidth="0.5"
          strokeDasharray="4 60" />
      </svg>

      {/* Inner ring */}
      <svg ref={ring1Ref} className="absolute inset-6 w-[calc(100%-48px)] h-[calc(100%-48px)]" viewBox="0 0 200 200">
        <circle cx="100" cy="100" r="75" fill="none" stroke="rgba(0,212,255,0.12)" strokeWidth="0.5" />
        <circle cx="100" cy="100" r="75" fill="none" stroke={getCoreColor()} strokeWidth="1"
          strokeDasharray="6 100" strokeDashoffset="30" opacity="0.5" />
        {/* Tick marks */}
        {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => (
          <line key={deg} x1="100" y1="25" x2="100" y2="30"
            stroke={getCoreColor()} strokeWidth="0.8" opacity="0.3"
            transform={`rotate(${deg} 100 100)`} />
        ))}
      </svg>

      {/* Core circle */}
      <div ref={coreRef} className="relative z-10 w-20 h-20 rounded-full flex items-center justify-center" style={{
        background: `radial-gradient(circle, ${getCoreColor()}22 0%, transparent 70%)`,
        border: `1.5px solid ${getCoreColor()}55`,
        boxShadow: `0 0 30px ${getCoreColor()}33, inset 0 0 20px ${getCoreColor()}11`,
      }}>
        <div className="w-12 h-12 rounded-full flex items-center justify-center" style={{
          border: `1px solid ${getCoreColor()}44`,
          boxShadow: `0 0 15px ${getCoreColor()}22`,
        }}>
          <span className="text-lg font-display font-bold" style={{
            color: getCoreColor(),
            textShadow: `0 0 12px ${getCoreColor()}88`,
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
}
