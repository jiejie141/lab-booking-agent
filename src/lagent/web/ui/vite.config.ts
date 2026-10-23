// defineConfig 取自 vitest/config 而不是 vite：vite 的 UserConfigExport
// 不认识 test 字段，直接写会报 "Object literal may only specify known properties"。
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// 构建产物直接落到 ../dist（即 src/lagent/web/dist），由 api.py 托管。
// 之所以不用默认的 ui/dist：那样静态资源会多一层路径，而 api.py 的托管代码
// 越短越容易讲清楚（也只有一处路径需要保证不穿越）。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/assets-app/", // 见 api.py 的 /assets-app/{path} 路由，与 Vite 默认 /assets/ 区分开
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    // 单页工具，不需要 sourcemap 进仓库；本地排查用 vite dev 即可
    sourcemap: false,
    rollupOptions: {
      output: {
        // 内容哈希：文件名即内容指纹。这条不是审美偏好 ——
        // 它让「HTML 是新的、JS 是旧的」这个 2026-09-27 踩过的灵异问题
        // 从设计上不可能发生（HTML 引用的文件名变了，浏览器只能拿新的）。
        entryFileNames: "assets/app-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
});
