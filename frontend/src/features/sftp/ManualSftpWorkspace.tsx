import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/controls";
import { EmptyState } from "../../components/ui/feedback";
import type {
  DeletePlanSummary,
  RemoteEntry,
} from "../../api/manual-sftp";
import { normalizeManualSftpError } from "../../api/manual-sftp";
import { SftpConfirmationDialogs, type SftpDialogState } from "./SftpConfirmationDialogs";
import { SftpDirectoryTree } from "./SftpDirectoryTree";
import { SftpFileTable } from "./SftpFileTable";
import { SftpLoadingIndicator } from "./SftpLoadingIndicator";
import { SftpRecoveryCenter } from "./SftpRecoveryCenter";
import { SftpTransferStrip } from "./SftpTransferStrip";
import type { ManualSftpController } from "./useManualSftpController";

export function ManualSftpWorkspace({
  controller,
  onSelectConnection,
}: {
  controller: ManualSftpController;
  onSelectConnection?: () => void;
}) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLElement>(null);
  const [pathInput, setPathInput] = useState("");
  const [dialog, setDialog] = useState<SftpDialogState>({ kind: "closed" });
  const [deletePlan, setDeletePlan] = useState<DeletePlanSummary | null>(null);
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uiError, setUiError] = useState<ReturnType<typeof normalizeManualSftpError> | null>(null);
  const { state } = controller;
  const path = state.requestedPath ?? state.listing?.path ?? state.context?.home ?? "/";
  const selected =
    state.listing?.entries.find((entry) => entry.path === state.selectedPath) ??
    null;

  useEffect(() => rootRef.current?.focus(), []);
  useEffect(() => setPathInput(path), [path]);
  useEffect(() => {
    if (
      state.terminal?.state === "cleanup_required" ||
      state.terminal?.state === "outcome_unknown"
    ) {
      setRecoveryOpen(true);
      void controller.loadRecoveries();
    }
  }, [controller.loadRecoveries, state.terminal?.operation_id, state.terminal?.state]);
  useEffect(() => {
    if (state.recoveries.length > 0) setRecoveryOpen(true);
  }, [state.recoveries.length]);

  const run = async (task: () => Promise<unknown>, close = true) => {
    setBusy(true);
    setUiError(null);
    try {
      await task();
      if (close) {
        setDialog({ kind: "closed" });
        setDeletePlan(null);
      }
      return true;
    } catch (error) {
      setUiError(normalizeManualSftpError(error));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const openEntry = (entry: RemoteEntry) => {
    if (entry.entry_type === "directory") {
      void controller.navigate(entry.path);
    } else if (entry.entry_type === "symlink") {
      setDialog({ kind: "linkLoading", entry });
      void run(async () => {
        const target = await controller.openLink(entry);
        setDialog({ kind: "closed" });
        if (target.entry_type === "directory") {
          await controller.navigate(target.path);
        }
      }, false).then((succeeded) => {
        if (!succeeded) setDialog({ kind: "closed" });
      });
    }
  };

  const openProperties = (entry: RemoteEntry, includeHash: boolean) => {
    setDialog({ kind: "propertiesLoading", entry, includeHash });
    void run(async () => {
      const inspected = await controller.inspectEntry(entry);
      const hash = includeHash ? await controller.hashFile(inspected) : null;
      setDialog({ kind: "properties", entry: inspected, hash });
    }, false).then((succeeded) => {
      if (!succeeded) setDialog({ kind: "closed" });
    });
  };

  const closeDialog = () => {
    if (state.preparation) void controller.discardPreparation();
    setDialog({ kind: "closed" });
    setDeletePlan(null);
  };

  if (!state.context) {
    if (state.contextLoading && controller.activeSession) {
      return (
        <section ref={rootRef} tabIndex={-1} className="grid h-full outline-none">
          <SftpLoadingIndicator label={t("sftp.loadingRemoteFiles")} />
        </section>
      );
    }
    return (
      <>
        <section ref={rootRef} tabIndex={-1} className="h-full outline-none">
          <EmptyState
            title={t("sftp.noSessionTitle")}
            body={(state.error ?? uiError)?.message ?? t("sftp.noSessionBody")}
            actions={
              onSelectConnection ? (
                <Button className="mt-3" onClick={onSelectConnection}>{t("sftp.selectConnection")}</Button>
              ) : undefined
            }
          />
        </section>
        <SftpRecoveryCenter
          open={recoveryOpen}
          loading={state.recoveriesLoading}
          recoveries={state.recoveries}
          onClose={() => setRecoveryOpen(false)}
          onInspect={(id) => void run(() => controller.inspectRecovery(id), false)}
          onExecute={(id, action) => void run(() => controller.executeRecovery(id, action), false)}
        />
      </>
    );
  }

  return (
    <section ref={rootRef} tabIndex={-1} aria-label={t("sftp.title")} className="flex h-full min-h-0 flex-col bg-app outline-none">
      <header className="flex flex-wrap items-center gap-2 border-b border-line bg-panel p-2">
        <Button variant="secondary" aria-label={t("sftp.parent")} onClick={() => void controller.navigate(parentPath(path))}>↑</Button>
        <label className="flex min-w-[220px] flex-1 items-center gap-2 text-sm">
          <span className="sr-only">{t("sftp.path")}</span>
          <input
            aria-label={t("sftp.path")}
            value={pathInput}
            className="min-w-0 flex-1 rounded border border-line bg-app px-3 py-2 font-mono text-sm text-ink"
            onChange={(event) => setPathInput(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void controller.navigate(pathInput);
            }}
          />
        </label>
        <Button variant="secondary" onClick={() => void controller.navigate(pathInput)}>{t("sftp.go")}</Button>
        <Button variant="secondary" onClick={() => void controller.refresh()}>{t("sftp.refresh")}</Button>
        <Button variant="secondary" disabled={state.listingLoading} onClick={() => setDialog({ kind: "upload" })}>{t("sftp.upload")}</Button>
        <Button variant="secondary" disabled={state.listingLoading} onClick={() => setDialog({ kind: "mkdir" })}>{t("sftp.newFolder")}</Button>
        <Button variant="secondary" disabled={state.listingLoading || !selected} onClick={() => selected && openProperties(selected, false)}>{t("sftp.properties")}</Button>
        <Button variant="secondary" onClick={() => setRecoveryOpen(true)}>{t("sftp.recoveries")} ({state.recoveriesLoading ? "…" : state.recoveries.length})</Button>
      </header>

      {state.error || uiError ? (
        <div role="alert" className="border-b border-danger/50 bg-danger/10 px-3 py-2 text-sm text-danger">
          <strong>{(state.error ?? uiError)?.code}</strong>: {(state.error ?? uiError)?.message}
        </div>
      ) : null}

      <div className="flex min-h-0 flex-1">
        <SftpDirectoryTree path={path} onListDirectories={controller.listTreeDirectories} onNavigate={(next) => void controller.navigate(next)} />
        <SftpFileTable
          entries={state.listingLoading ? [] : state.listing?.entries ?? []}
          loading={state.listingLoading}
          loadingLabel={t("sftp.loadingDirectory", { path })}
          selectedPath={state.selectedPath}
          onSelect={controller.select}
          onOpen={openEntry}
          onDownload={(entry) => void run(() => controller.prepareDownload(entry), false)}
          onRename={(entry) => setDialog({ kind: "rename", entry })}
          onMove={(entry) => setDialog({ kind: "move", entry })}
          onDelete={(entry) => setDialog({ kind: "delete", entry })}
          onProperties={(entry) => openProperties(entry, false)}
          onHash={(entry) => openProperties(entry, true)}
          onReadLinkTarget={(entry) => openProperties(entry, false)}
          onRefresh={() => void controller.refresh()}
          onParent={() => void controller.navigate(parentPath(path))}
        />
      </div>

      <SftpTransferStrip progress={state.transferProgress} onCancel={(id) => void run(() => controller.cancelOperation(id), false)} />
      <SftpConfirmationDialogs
        dialog={dialog}
        preparation={state.preparation}
        deletePlan={deletePlan}
        busy={busy}
        onClose={closeDialog}
        onUpload={(name) => void run(() => controller.prepareUpload(name), false)}
        onMkdir={(name) => void run(async () => { await controller.createDirectory(name); await controller.refresh(); })}
        onRename={(entry, name) => void run(async () => {
          const targetPath = childPath(path, name);
          try {
            await controller.renameEntry(entry.path, targetPath, false);
            await controller.refresh();
            setDialog({ kind: "closed" });
          } catch (error) {
            if (normalizeManualSftpError(error).code === "SFTP_TARGET_EXISTS") {
              // A separate confirmed command is required so Rust reacquires the target and hash.
              setDialog({ kind: "renameOverwrite", entry, targetPath });
              return;
            }
            throw error;
          }
        }, false)}
        onMove={(entry, targetPath) => void run(async () => {
          try {
            await controller.renameEntry(entry.path, targetPath, false);
            await controller.refresh();
            setDialog({ kind: "closed" });
          } catch (error) {
            if (normalizeManualSftpError(error).code === "SFTP_TARGET_EXISTS") {
              setDialog({ kind: "renameOverwrite", entry, targetPath });
              return;
            }
            throw error;
          }
        }, false)}
        onConfirmRenameOverwrite={(entry, targetPath) => void run(async () => {
          await controller.renameEntry(entry.path, targetPath, true);
          await controller.refresh();
        })}
        onDelete={(entry) => {
          if (entry.entry_type === "directory") {
            void run(async () => {
              const plan = await controller.preflightDelete(entry.path);
              setDeletePlan(plan);
              setDialog({ kind: "closed" });
            }, false);
            return;
          }
          void run(async () => {
            await controller.removeEntry(entry.path);
            await controller.refresh();
          });
        }}
        onExecuteDelete={(plan) => void run(async () => { await controller.executeDelete(plan.delete_plan_id); await controller.refresh(); })}
        onExecutePrepared={() => void run(async () => { await controller.executePrepared(); await controller.refresh(); })}
      />
      <SftpRecoveryCenter
        open={recoveryOpen}
        loading={state.recoveriesLoading}
        recoveries={state.recoveries}
        onClose={() => setRecoveryOpen(false)}
        onInspect={(id) => void run(() => controller.inspectRecovery(id), false)}
        onExecute={(id, action) => void run(() => controller.executeRecovery(id, action), false)}
      />
    </section>
  );
}

const parentPath = (path: string) => {
  if (path === "/") return "/";
  const parent = path.slice(0, path.lastIndexOf("/"));
  return parent || "/";
};

const childPath = (parent: string, name: string) =>
  parent === "/" ? `/${name}` : `${parent.replace(/\/$/, "")}/${name}`;
