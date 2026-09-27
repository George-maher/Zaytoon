export default function StatusDot({ connected, listening, speaking }) {
  let color;
  let anim;
  if (listening) {
    color = "#00ff88";
    anim = "pulse-ring-green 2s infinite";
  } else if (speaking) {
    color = "#00f0ff";
    anim = "pulse-ring 2s infinite";
  } else if (connected) {
    color = "#00d4ff";
    anim = "none";
  } else {
    color = "#ff3355";
    anim = "pulse-ring-red 2s infinite";
  }

  return (
    <span className="relative inline-flex items-center justify-center w-2 h-2">
      <span className="absolute inset-0 rounded-full" style={{
        background: color,
        animation: anim,
        opacity: 0.3,
      }} />
      <span className="relative w-1.5 h-1.5 rounded-full" style={{
        background: color,
        boxShadow: `0 0 6px ${color}, 0 0 12px ${color}44`,
      }} />
    </span>
  );
}
