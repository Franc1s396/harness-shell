import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { AgentCommandError, ModelApiConfig } from "../../api/agent";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { AgentProviderDialog } from "./AgentProviderDialog";
import type {
  ProviderDraft,
  ProviderMutationFailure,
} from "./provider-config-actions";

export type ModelProvidersPanelProps = {
  configs: readonly ModelApiConfig[];
  loading: boolean;
  error: AgentCommandError | null;
  mutationError: ProviderMutationFailure | null;
  activeApiConfigIds: ReadonlySet<string>;
  onCreate: (draft: ProviderDraft, apiKey: string) => Promise<void>;
  onUpdate: (
    config: ModelApiConfig,
    draft: ProviderDraft,
    apiKey: string,
  ) => Promise<void>;
  onDelete: (config: ModelApiConfig) => Promise<void>;
  onRetry: () => Promise<void>;
};

export function ModelProvidersPanel(props: ModelProvidersPanelProps) {
  const { t } = useTranslation();
  const [editor, setEditor] = useState<"create" | ModelApiConfig | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ModelApiConfig | null>(null);
  const [busy, setBusy] = useState(false);

  const mutationCommandError = props.mutationError?.primaryError ?? null;

  const submitEditor = async (draft: ProviderDraft, apiKey: string) => {
    setBusy(true);
    try {
      if (editor === "create") {
        await props.onCreate(draft, apiKey);
      } else if (editor) {
        await props.onUpdate(editor, draft, apiKey);
      }
      setEditor(null);
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setBusy(true);
    try {
      await props.onDelete(pendingDelete);
      setPendingDelete(null);
    } catch {
      // The controlled parent exposes the structured mutation failure.
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-base font-semibold text-ink">
          {t("settings.modelProviders.title")}
        </h3>
        <Button onClick={() => setEditor("create")}>
          {t("settings.modelProviders.newProvider")}
        </Button>
      </div>

      {props.loading ? (
        <p className="text-sm text-ink-muted">
          {t("settings.modelProviders.loading")}
        </p>
      ) : null}
      {props.error ? (
        <div role="alert" className="grid gap-2 text-sm text-danger">
          <p><strong>{props.error.code}</strong>: {props.error.message}</p>
          <Button variant="secondary" onClick={() => void props.onRetry()}>
            {t("settings.modelProviders.retry")}
          </Button>
        </div>
      ) : null}
      {!props.loading && !props.error && props.configs.length === 0 ? (
        <p className="text-sm text-ink-muted">
          {t("settings.modelProviders.empty")}
        </p>
      ) : null}

      <div className="grid gap-2">
        {props.configs.map((config) => {
          const active = props.activeApiConfigIds.has(config.api_config_id);
          return (
            <article key={config.api_config_id} className="grid gap-2 rounded-lg border border-line p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h4 className="font-medium text-ink">{config.display_name}</h4>
                  <p className="break-all text-xs text-ink-muted">{config.api_type} · {config.model}</p>
                  <p className="break-all text-xs text-ink-dim">{config.base_url}</p>
                </div>
                <span className="text-xs text-ink-muted">
                  {t(config.enabled ? "settings.modelProviders.enabled" : "settings.modelProviders.disabled")}
                </span>
              </div>
              <p className="text-xs text-success">{t("settings.modelProviders.storedKey")}</p>
              {active ? (
                <p className="text-xs text-accent">{t("settings.modelProviders.activeRun")}</p>
              ) : null}
              <div className="flex justify-end gap-2">
                <Button
                  variant="secondary"
                  aria-label={`Edit ${config.display_name}`}
                  disabled={active}
                  onClick={() => setEditor(config)}
                >
                  {t("connections.edit")}
                </Button>
                <Button
                  variant="danger"
                  aria-label={`Delete ${config.display_name}`}
                  disabled={active}
                  onClick={() => setPendingDelete(config)}
                >
                  {t("common.delete")}
                </Button>
              </div>
            </article>
          );
        })}
      </div>

      {props.mutationError ? (
        <p role="alert" className="text-sm text-danger">
          {t("settings.modelProviders.primaryFailure")}: <strong>{props.mutationError.primaryError.code}</strong>
        </p>
      ) : null}
      {editor === "create" ? (
        <AgentProviderDialog
          open
          mode="create"
          config={null}
          busy={busy}
          error={mutationCommandError}
          onClose={() => setEditor(null)}
          onSubmit={submitEditor}
        />
      ) : editor ? (
        <AgentProviderDialog
          open
          mode="edit"
          config={editor}
          busy={busy}
          error={mutationCommandError}
          onClose={() => setEditor(null)}
          onSubmit={submitEditor}
        />
      ) : null}

      <Dialog
        open={pendingDelete !== null}
        busy={busy}
        title={t("settings.modelProviders.deleteTitle")}
        onClose={() => setPendingDelete(null)}
      >
        <p className="mt-3 text-sm text-ink-muted">
          {t("settings.modelProviders.deleteBody", {
            name: pendingDelete?.display_name ?? "",
          })}
        </p>
        {props.mutationError ? (
          <p role="alert" className="mt-3 text-sm text-danger">
            {t("settings.modelProviders.primaryFailure")}: <strong>{props.mutationError.primaryError.code}</strong>
          </p>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" disabled={busy} onClick={() => setPendingDelete(null)}>
            {t("common.cancel")}
          </Button>
          <Button variant="danger" disabled={busy} onClick={() => void confirmDelete()}>
            {t("settings.modelProviders.confirmDelete")}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
