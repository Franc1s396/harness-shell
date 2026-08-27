import { useTranslation } from "react-i18next";

export type StatusBarProps = {
  runtimeState: string;
  sshState: string;
  hostKeyState: string;
  ptySize: { cols: number; rows: number } | null;
  agentWidth: number | null;
  route: "Direct" | "ProxyJump" | "unknown";
};

export function StatusBar({
  runtimeState,
  sshState,
  hostKeyState,
  ptySize,
  agentWidth,
  route,
}: StatusBarProps) {
  const { t } = useTranslation();
  const values = [
    [t("status.runtime"), runtimeState],
    [t("status.ssh"), sshState],
    [t("status.hostKey"), hostKeyState],
    [t("status.pty"), ptySize ? `${ptySize.cols}×${ptySize.rows}` : "unknown"],
    [t("status.agent"), agentWidth === null ? "collapsed" : `${agentWidth}px`],
    [t("status.route"), route],
  ];

  return (
    <footer className="flex h-[23px] items-center gap-5 overflow-hidden border-t border-line bg-panel px-3 font-mono text-[11px] text-ink-dim">
      {values.map(([label, value]) => (
        <span key={label} className="whitespace-nowrap">
          {label}: <span className="text-ink-muted">{value}</span>
        </span>
      ))}
    </footer>
  );
}
