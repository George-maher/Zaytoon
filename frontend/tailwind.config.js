/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        "bg-hud": "#050B14",
        "bg-hud-deep": "#04080F",
        "neon-cyan": "#00D9FF",
        "neon-blue": "#3AB6FF",
        "neon-green": "#00FF88",
        "neon-red": "#FF3355",
        "neon-amber": "#F0B000",
      },
      fontFamily: {
        display: ["Orbitron", "monospace"],
        body: ["Rajdhani", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
      keyframes: {
        "pulse-ring": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(0, 212, 255, 0.4)" },
          "50%": { boxShadow: "0 0 0 15px rgba(0, 212, 255, 0)" },
        },
        "pulse-ring-green": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(0, 255, 136, 0.4)" },
          "50%": { boxShadow: "0 0 0 15px rgba(0, 255, 136, 0)" },
        },
        "pulse-ring-red": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(255, 51, 85, 0.4)" },
          "50%": { boxShadow: "0 0 0 15px rgba(255, 51, 85, 0)" },
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
