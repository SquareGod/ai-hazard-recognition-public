import vinext from "vinext";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [vinext()],
  server: {
    host: "0.0.0.0",
    port: 3000,
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8010",
        changeOrigin: true,
        // Inside Docker the backend sees a non-loopback peer, so this gateway
        // must inject the stream-control key; otherwise protected endpoints
        // (video sources, streams, hazards, media tickets...) return 401.
        // Native development stays loopback-only and needs no key here.
        ...(process.env.VITE_API_PROXY_CONTROL_KEY
          ? { headers: { "X-Control-Key": process.env.VITE_API_PROXY_CONTROL_KEY } }
          : {}),
      },
    },
  },
});
