import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  diagnosticsApi,
  normalizeDiagnosticsCommandError,
  type DiagnosticsCommandError,
} from "../../api/diagnostics";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { type LanguageMode } from "../../i18n/locale";
import { useLocaleStore } from "../../stores/locale-store";
import { useWorkspaceUiStore } from "../../stores/workspace-ui-store";

export type SettingsCategory = "general" | "modelProviders";

export type SettingsDialogProps = {
  open: boolean;
  initialCategory: SettingsCategory;
  onClose: () => void;
  modelProviders: ReactNode;
};

function LanguageSetting() {
  const { t } = useTranslation();
  const languageMode = useLocaleStore((state) => state.languageMode);

  return (
    <label className="grid gap-1 text-xs text-ink-muted">
      <span>{t("settings.language")}</span>
      <select
        aria-label={t("settings.language")}
        className="rounded border border-line bg-input px-2 py-2 text-sm text-ink"
        value={languageMode}
        onChange={(event) => {
          void useLocaleStore
            .getState()
            .setLanguageMode(event.target.value as LanguageMode)
            .then(() =>
              useWorkspaceUiStore.getState().bumpLayoutRevision(),
            );
        }}
      >
        <option value="system">{t("language.system")}</option>
        <option value="zh-CN">{t("language.zhCN")}</option>
        <option value="zh-TW">{t("language.zhTW")}</option>
        <option value="en">{t("language.en")}</option>
      </select>
    </label>
  );
}

function DiagnosticsSetting({ open }: { open: boolean }) {
  const { t } = useTranslation();
  const requestGeneration = useRef(0);
  const [logDirectory, setLogDirectory] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<DiagnosticsCommandError | null>(null);

  useEffect(() => {
    if (!open) {
      requestGeneration.current += 1;
      return;
    }

    const generation = ++requestGeneration.current;
    setLoading(true);
    setLogDirectory(null);
    setError(null);
    void diagnosticsApi
      .getLogDirectory()
      .then((directory) => {
        if (requestGeneration.current !== generation) return;
        setLogDirectory(directory);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (requestGeneration.current !== generation) return;
        setError(normalizeDiagnosticsCommandError(cause));
        setLoading(false);
      });

    return () => {
      requestGeneration.current += 1;
    };
  }, [open]);

  const handleOpen = async () => {
    const generation = requestGeneration.current;
    setOpening(true);
    setError(null);
    try {
      await diagnosticsApi.openLogDirectory();
    } catch (cause) {
      if (requestGeneration.current === generation) {
        setError(normalizeDiagnosticsCommandError(cause));
      }
    } finally {
      if (requestGeneration.current === generation) setOpening(false);
    }
  };

  return (
    <section className="grid gap-3 border-t border-line pt-5">
      <div>
        <h3 className="text-sm font-semibold text-ink">
          {t("settings.diagnostics.title")}
        </h3>
        <p className="mt-1 text-xs text-ink-muted">
          {t("settings.diagnostics.description")}
        </p>
      </div>
      {loading ? (
        <p className="text-xs text-ink-muted">
          {t("settings.diagnostics.loading")}
        </p>
      ) : null}
      {logDirectory ? (
        <div className="grid gap-2">
          <span className="text-xs text-ink-muted">
            {t("settings.diagnostics.path")}
          </span>
          <code className="break-all rounded border border-line bg-input px-3 py-2 text-xs text-ink">
            {logDirectory}
          </code>
          <div>
            <Button
              variant="secondary"
              disabled={opening}
              onClick={() => void handleOpen()}
            >
              {opening
                ? t("settings.diagnostics.opening")
                : t("settings.diagnostics.open")}
            </Button>
          </div>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-xs text-danger">
          <strong>{error.code}</strong>: {error.message}
        </p>
      ) : null}
    </section>
  );
}

function GeneralSettings({ open }: { open: boolean }) {
  return (
    <div className="grid gap-6">
      <LanguageSetting />
      <DiagnosticsSetting open={open} />
    </div>
  );
}

export function SettingsDialog({
  open,
  initialCategory,
  onClose,
  modelProviders,
}: SettingsDialogProps) {
  const { t } = useTranslation();
  const [category, setCategory] =
    useState<SettingsCategory>(initialCategory);
  const wasOpen = useRef(false);

  useEffect(() => {
    if (open && !wasOpen.current) setCategory(initialCategory);
    wasOpen.current = open;
  }, [initialCategory, open]);

  return (
    <Dialog open={open} title={t("nav.settings")} onClose={onClose}>
      <div className="mt-4 grid max-h-[min(38rem,calc(100vh-4rem))] min-h-0 grid-cols-[11rem_minmax(0,1fr)] overflow-hidden rounded-lg border border-line">
        <nav aria-label={t("nav.settings")} className="grid content-start gap-1 border-r border-line p-2">
          <button
            type="button"
            aria-current={category === "general" ? "page" : undefined}
            className="rounded px-3 py-2 text-left text-sm text-ink-muted hover:bg-raised aria-[current=page]:bg-accent-soft aria-[current=page]:text-ink"
            onClick={() => setCategory("general")}
          >
            {t("settings.general")}
          </button>
          <button
            type="button"
            aria-current={category === "modelProviders" ? "page" : undefined}
            className="rounded px-3 py-2 text-left text-sm text-ink-muted hover:bg-raised aria-[current=page]:bg-accent-soft aria-[current=page]:text-ink"
            onClick={() => setCategory("modelProviders")}
          >
            {t("settings.modelProviders.title")}
          </button>
        </nav>
        <section className="min-h-0 overflow-y-auto p-4">
          {category === "general" ? (
            <GeneralSettings open={open} />
          ) : (
            modelProviders
          )}
        </section>
      </div>
      <div className="mt-4 flex justify-end">
        <Button variant="secondary" onClick={onClose}>
          {t("settings.close")}
        </Button>
      </div>
    </Dialog>
  );
}
