import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served from https://<user>.github.io/spark-gate/ on GitHub Pages.
export default defineConfig({
  base: process.env.SG_BASE ?? "/spark-gate/",
  plugins: [react()],
});
