import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { App } from "./App";

const host = document.getElementById("root");
if (!host) throw new Error("index.html 里没有 #root —— 构建入口被改过？");

createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
