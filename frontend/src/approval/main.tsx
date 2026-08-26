import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApprovalApp } from "./ApprovalApp";
import { initializeLocale } from "../stores/locale-store";
import "../styles/globals.css";

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("#root is missing.");
const root = createRoot(rootElement);

void initializeLocale().then(() => {
  root.render(<StrictMode><ApprovalApp /></StrictMode>);
}).catch((error: unknown) => {
  rootElement.textContent = `Localization initialization failed: ${error instanceof Error ? error.message : String(error)}`;
});
