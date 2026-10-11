/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#0b0e14", 900: "#11151d", 850: "#161b25", 800: "#1c2230", 700: "#283044", 600: "#3a4560" },
        gold: { 400: "#e8c46a", 500: "#d4a843" },
        tim: "#4f9cf9",
        alex: "#a78bfa",
        max: "#f59e0b",
        cio: "#e8c46a",
      },
      fontFamily: { sans: ["Inter", "system-ui", "sans-serif"], display: ["Fraunces", "Georgia", "serif"] },
    },
  },
  plugins: [],
};
