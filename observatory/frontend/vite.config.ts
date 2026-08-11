import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built output is committed (observatory/static/) and served by the task-2
// Datasette plugin at /observatory/static/<path> -- see plugin.py:static_view
// and index_view (index.html itself is copied to observatory/static/index.html,
// replacing the task-2 placeholder but keeping its <!--OBSERVATORY_BOOTSTRAP-->
// marker, which plugin.py's _shell_html() still swaps for the bootstrap
// <script> tag at request time).
//
// Filenames are deterministic (no content-hash suffixes) so a rebuild
// produces a clean, reviewable diff instead of churning every asset name on
// every build -- this is a small, developer-tool-grade bundle, not a
// cache-busted public CDN asset.
export default defineConfig({
  plugins: [react()],
  base: "/observatory/static/",
  build: {
    outDir: "../static",
    emptyOutDir: true, // static/ is entirely build output (index.html included) -- safe to wipe and regenerate
    assetsDir: "assets",
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name].[ext]",
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
