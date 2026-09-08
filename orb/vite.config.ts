import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Built output is served by roger/server.py at /orb (index) and /orb/assets/* (static).
export default defineConfig({
  plugins: [react()],
  base: "/orb/",
  build: { outDir: "../roger/static/orb", emptyOutDir: true },
})
