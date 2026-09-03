import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { initializeBackendClient } from "./api/bootstrap";
import { initializeLocale } from "./stores/locale-store";
import "./styles/globals.css";

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("#root is missing.");
const root = ReactDOM.createRoot(rootElement);

void initializeBackendClient().then(() => initializeLocale()).then(() => {
  root.render(<React.StrictMode><App /></React.StrictMode>);
}).catch((error: unknown) => {
  rootElement.textContent = `Runtime initialization failed: ${error instanceof Error ? error.message : String(error)}`;
});
