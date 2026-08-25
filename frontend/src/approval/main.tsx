import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApprovalApp } from "./ApprovalApp";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ApprovalApp />
  </StrictMode>,
);
