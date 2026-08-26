import type { SVGProps } from "react";

export type ShellIconName =
  | "agent"
  | "connections"
  | "files"
  | "menu"
  | "security"
  | "settings"
  | "sftp"
  | "terminal";

const paths: Record<ShellIconName, string> = {
  agent:
    "M12 3a4 4 0 0 0-4 4v1H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7a2 2 0 0 0-2-2h-2V7a4 4 0 0 0-4-4Zm-2 5V7a2 2 0 1 1 4 0v1h-4Zm-2 4h2v2H8v-2Zm6 0h2v2h-2v-2Zm-6 4h8v1H8v-1Z",
  connections:
    "M7 3a2 2 0 0 0-2 2v4h2V5h10v4h2V5a2 2 0 0 0-2-2H7Zm-2 8a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h6v-2H5v-6h14v6h-6v2h6a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2H5Zm6 5v5h2v-5h-2Z",
  files:
    "M4 4a2 2 0 0 1 2-2h5l2 3h5a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm2 3v11h12V7H6Z",
  menu: "M4 6h16v2H4V6Zm0 5h16v2H4v-2Zm0 5h16v2H4v-2Z",
  security:
    "m12 2 8 3v6c0 5.25-3.4 9.7-8 11-4.6-1.3-8-5.75-8-11V5l8-3Zm0 2.13L6 6.38V11c0 4.1 2.5 7.75 6 8.87 3.5-1.12 6-4.77 6-8.87V6.38l-6-2.25Z",
  settings:
    "m10.3 2-.5 2.1a8 8 0 0 0-1.7 1L6 4.5 4.5 6l.6 2.1a8 8 0 0 0-1 1.7L2 10.3v2.1l2.1.5a8 8 0 0 0 1 1.7l-.6 2.1L6 18.2l2.1-.6a8 8 0 0 0 1.7 1l.5 2.1h2.1l.5-2.1a8 8 0 0 0 1.7-1l2.1.6 1.5-1.5-.6-2.1a8 8 0 0 0 1-1.7l2.1-.5v-2.1l-2.1-.5a8 8 0 0 0-1-1.7l.6-2.1-1.5-1.5-2.1.6a8 8 0 0 0-1.7-1L12.4 2h-2.1ZM11.35 8a3 3 0 1 1 0 6 3 3 0 0 1 0-6Z",
  sftp: "M4 3h16v6h-2V5H6v14h12v-4h2v6H4V3Zm9 5 5 4-5 4v-3H8v-2h5V8Z",
  terminal: "M4 4h16v16H4V4Zm2 2v12h12V6H6Zm1.5 3L10 11.5 7.5 14 6 12.5 8.5 10 6 7.5 7.5 6 10 8.5 7.5 11 9 12.5 7.5 14 6 12.5 8.5 10 6 7.5 7.5 6 10 8.5 7.5 11Zm3.5 5h5v2h-5v-2Z",
};

export function ShellIcon({
  name,
  ...props
}: SVGProps<SVGSVGElement> & { name: ShellIconName }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="currentColor"
      width="18"
      height="18"
      {...props}
    >
      <path d={paths[name]} />
    </svg>
  );
}
