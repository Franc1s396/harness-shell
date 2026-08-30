import { useState } from "react";
import { useTranslation } from "react-i18next";

import { normalizeManualSftpError } from "../../api/manual-sftp";
import { SftpLoadingIndicator } from "./SftpLoadingIndicator";

export type TreeDirectory = {
  name: string;
  path: string;
};

export function SftpDirectoryTree({
  path,
  onListDirectories,
  onNavigate,
}: {
  path: string;
  onListDirectories: (path: string) => Promise<TreeDirectory[]>;
  onNavigate: (path: string) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [children, setChildren] = useState<Map<string, TreeDirectory[]>>(new Map());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const toggle = async (directory: TreeDirectory) => {
    if (expanded.has(directory.path)) {
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(directory.path);
        return next;
      });
      return;
    }
    setLoading((current) => new Set(current).add(directory.path));
    setError(null);
    try {
      const listed = await onListDirectories(directory.path);
      setChildren((current) => new Map(current).set(directory.path, listed));
      setExpanded((current) => new Set(current).add(directory.path));
    } catch (cause) {
      const normalized = normalizeManualSftpError(cause);
      setError(`${normalized.code}: ${normalized.message}`);
    } finally {
      setLoading((current) => {
        const next = new Set(current);
        next.delete(directory.path);
        return next;
      });
    }
  };
  return (
    <nav
      aria-label={t("sftp.tree")}
      className="hidden min-h-0 w-56 shrink-0 overflow-auto border-r border-line bg-panel p-2 lg:block"
    >
      <div role="tree">
        <TreeNode directory={{ name: "/", path: "/" }} depth={0} currentPath={path} expanded={expanded} children={children} loading={loading} onToggle={toggle} onNavigate={onNavigate} />
      </div>
      {error ? <p role="alert" className="p-2 text-xs text-danger">{error}</p> : null}
    </nav>
  );
}

function TreeNode({ directory, depth, currentPath, expanded, children, loading, onToggle, onNavigate }: {
  directory: TreeDirectory;
  depth: number;
  currentPath: string;
  expanded: ReadonlySet<string>;
  children: ReadonlyMap<string, TreeDirectory[]>;
  loading: ReadonlySet<string>;
  onToggle: (directory: TreeDirectory) => Promise<void>;
  onNavigate: (path: string) => void;
}) {
  const { t } = useTranslation();
  const isExpanded = expanded.has(directory.path);
  const isLoading = loading.has(directory.path);
  return (
    <div role="treeitem" aria-expanded={isExpanded}>
      <div className="flex items-center" style={{ paddingInlineStart: `${depth * 12}px` }}>
        <button type="button" aria-label={`${t("sftp.tree")} ${directory.name}`} aria-busy={isLoading} disabled={isLoading} className="grid w-6 shrink-0 place-items-center rounded py-1 text-ink-muted hover:bg-raised" onClick={() => void onToggle(directory)}>
          {isLoading ? (
            <SftpLoadingIndicator
              compact
              label={t("sftp.loadingTreeDirectory", { path: directory.path })}
            />
          ) : isExpanded ? "▾" : "▸"}
        </button>
        <button type="button" aria-current={directory.path === currentPath ? "page" : undefined} className="min-w-0 flex-1 truncate rounded px-1 py-1.5 text-left text-sm text-ink-muted hover:bg-raised hover:text-ink aria-[current=page]:bg-accent-soft aria-[current=page]:text-accent" onClick={() => onNavigate(directory.path)}>
          {directory.name}
        </button>
      </div>
      {isExpanded ? (children.get(directory.path) ?? []).map((child) => (
        <TreeNode key={child.path} directory={child} depth={depth + 1} currentPath={currentPath} expanded={expanded} children={children} loading={loading} onToggle={onToggle} onNavigate={onNavigate} />
      )) : null}
    </div>
  );
}
