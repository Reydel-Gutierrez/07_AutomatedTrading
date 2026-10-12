/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#0b0e14", 900: "#11151d", 850: "#161b25", 800: "#1c2230", 700: "#283044", 600: "#3a4560" },
        gold: { 300: "#e4cf9f", 400: "#dcc287", 500: "#cdb27a" },
        tim: "#4f9cf9",
        alex: "#a78bfa",
        max: "#f59e0b",
        cio: "#dcc287",
      },
      fontFamily: { sans: ["Inter", "system-ui", "sans-serif"], display: ["Fraunces", "Georgia", "serif"] },
    },
  },
  plugins: [],
};
