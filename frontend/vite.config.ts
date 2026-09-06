import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// @ts-expect-error process 是 Node.js 全局变量
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
      },
    },
  },

  // 为 Tauri 开发定制的 Vite 选项，仅在 tauri dev 或 tauri build 中应用。
  //
  // 1. 防止 Vite 遮蔽 Rust 错误。
  clearScreen: false,
  // 2. Tauri 要求固定端口；端口不可用时失败。
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. 告知 Vite 不监听 src-tauri。
      ignored: ["**/src-tauri/**"],
    },
  },
}));
