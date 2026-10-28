/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Shared palette (docs/DESIGN_SYSTEM.md §1 + the bg/border/text
        // tokens in styles/theme.css). Components that reach for a utility
        // class get the same values as the CSS variables -- no second copy
        // of the palette with a stale hue. Values are the Figma reference's
        // (NEXUS AI): cyan system, sky network, purple memory, amber warn.
        "bg-hud": "#010812",
        "bg-hud-deep": "#020d1a",
        "neon-cyan": "#00f5ff",
        "neon-blue": "#0ea5e9",
        "neon-green": "#22c55e",
        "neon-red": "#ec4899",
        "neon-amber": "#f59e0b",
        "neon-violet": "#a855f7",
        "hud-text": "#c6d4e7",
        "hud-mute": "#6b809c",
        "hud-faint": "#4d5f78",
        "hud-line": "#0b1d30",
        "hud-line-strong": "#144e63",
      },
      fontFamily: {
        // Arabic faces are listed *before* the Latin display faces on the
        // `font-sans`/`font-body` stacks. Orbitron, Rajdhani, Share Tech Mono
        // and JetBrains Mono contain no Arabic glyphs, so without Cairo /
        // Noto Kufi Arabic first in the list, Arabic text fell through to an
        // OS-substituted face and lost the HUD typography entirely.
        display: ["Orbitron", "Cairo", "Noto Kufi Arabic", "monospace"],
        body: ["Rajdhani", "Cairo", "Noto Kufi Arabic", "sans-serif"],
        mono: ["Share Tech Mono", "JetBrains Mono", "Cairo", "Fira Code", "monospace"],
        // Explicit Arabic stack for content that must never fall back to a
        // Latin face, e.g. the confirmation dialog's `dir="rtl"` block.
        arabic: ["Cairo", "Noto Kufi Arabic", "Tahoma", "sans-serif"],
      },
      keyframes: {
        "pulse-ring": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(0, 245, 255, 0.4)" },
          "50%": { boxShadow: "0 0 0 15px rgba(0, 245, 255, 0)" },
        },
        "pulse-ring-green": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(34, 197, 94, 0.4)" },
          "50%": { boxShadow: "0 0 0 15px rgba(34, 197, 94, 0)" },
        },
        "pulse-ring-red": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(236, 72, 153, 0.4)" },
          "50%": { boxShadow: "0 0 0 15px rgba(236, 72, 153, 0)" },
        },
        "fade-in": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "slide-up": {
          "0%": { opacity: "0", transform: "translateY(20px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "scan-line": {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(100vh)" },
        },
        "spin-slow": {
          from: { transform: "rotate(0deg)" },
          to: { transform: "rotate(360deg)" },
        },
      },
      animation: {
        "pulse-ring": "pulse-ring 2s infinite",
        "pulse-ring-green": "pulse-ring-green 2s infinite",
        "pulse-ring-red": "pulse-ring-red 2s infinite",
        "fade-in": "fade-in 0.3s ease-out",
        "slide-up": "slide-up 0.5s ease-out",
        "scan-line": "scan-line 4s linear infinite",
        "spin-slow": "spin-slow 8s linear infinite",
      },
    },
  },
  plugins: [],
};
