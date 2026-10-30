import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { installTips } from "./tips";
import "./styles.css";
import "./responsive.css";

installTips();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
