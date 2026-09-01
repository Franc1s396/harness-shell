import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import type { AgentCommandError, ModelApiConfig } from "../../api/agent";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { FormField } from "../../components/ui/fields";
import type { ProviderDraft } from "./provider-config-actions";

type AgentProviderDialogBaseProps = {
  open: boolean;
  busy: boolean;
  error: AgentCommandError | null;
  onClose: () => void;
  onSubmit: (draft: ProviderDraft, apiKey: string) => Promise<void>;
};

export type AgentProviderDialogProps = AgentProviderDialogBaseProps &
  (
    | { mode: "create"; config: null }
    | { mode: "edit"; config: ModelApiConfig }
  );

export type ProviderFormErrors = Partial<
  Record<"displayName" | "apiType" | "baseUrl" | "model" | "apiKey", string>
>;

export const validateProviderDraft = (
  draft: ProviderDraft,
  apiKey: string,
  mode: "create" | "edit",
): ProviderFormErrors => {
  const errors: ProviderFormErrors = {};
  const displayName = draft.displayName.trim();
  const baseUrl = draft.baseUrl.trim();
  const model = draft.model.trim();
  if ([...displayName].length < 1 || [...displayName].length > 80) {
    errors.displayName = "INVALID";
  }
  if ([...model].length < 1 || [...model].length > 255) {
    errors.model = "INVALID";
  }
  if ([...baseUrl].length < 1 || [...baseUrl].length > 2048) {
    errors.baseUrl = "INVALID";
  }
  try {
    const parsed = new URL(baseUrl);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      errors.baseUrl = "INVALID";
    }
  } catch {
    errors.baseUrl = "INVALID";
  }
  if (mode === "create" && apiKey.length === 0) {
    errors.apiKey = "REQUIRED";
  }
  return errors;
};

const createDraft = (config: ModelApiConfig | null): ProviderDraft =>
  config
    ? {
        displayName: config.display_name,
        apiType: config.api_type,
        baseUrl: config.base_url,
        model: config.model,
        enabled: config.enabled,
      }
    : {
        displayName: "",
        apiType: "RESPONSES",
        baseUrl: "",
        model: "",
        enabled: true,
      };

export function AgentProviderDialog(props: AgentProviderDialogProps) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<ProviderDraft>(() =>
    createDraft(props.config),
  );
  const [apiKey, setApiKey] = useState("");
  const [errors, setErrors] = useState<ProviderFormErrors>({});

  useEffect(() => {
    if (!props.open) {
      setApiKey("");
      setErrors({});
      return;
    }
    setDraft(createDraft(props.config));
    setApiKey("");
    setErrors({});
  }, [props.open, props.mode, props.config]);

  const close = () => {
    setApiKey("");
    setErrors({});
    props.onClose();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const validation = validateProviderDraft(draft, apiKey, props.mode);
    setErrors(validation);
    if (Object.keys(validation).length > 0) {
      setApiKey("");
      return;
    }

    const normalizedDraft: ProviderDraft = {
      ...draft,
      displayName: draft.displayName.trim(),
      baseUrl: draft.baseUrl.trim(),
      model: draft.model.trim(),
    };
    try {
      await props.onSubmit(normalizedDraft, apiKey);
    } catch {
      // The controlled parent exposes the structured mutation failure.
    } finally {
      setApiKey("");
    }
  };

  const validationMessage = (value: string | undefined) =>
    value === "REQUIRED"
      ? t("settings.modelProviders.required")
      : value
        ? t("settings.modelProviders.invalid")
        : undefined;

  return (
    <Dialog
      open={props.open}
      busy={props.busy}
      title={t(
        props.mode === "create"
          ? "settings.modelProviders.createTitle"
          : "settings.modelProviders.editTitle",
      )}
      onClose={close}
    >
      <form className="mt-4 grid gap-4" onSubmit={(event) => void submit(event)}>
        <FormField
          id="provider-display-name"
          label={t("settings.modelProviders.displayName")}
          error={validationMessage(errors.displayName)}
        >
          <input
            className="rounded border border-line bg-input px-3 py-2 text-ink"
            value={draft.displayName}
            disabled={props.busy}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                displayName: event.target.value,
              }))
            }
          />
        </FormField>
        <FormField id="provider-api-type" label={t("settings.modelProviders.apiType")}>
          <select
            className="rounded border border-line bg-input px-3 py-2 text-ink"
            value={draft.apiType}
            disabled={props.busy}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                apiType: event.target.value as ProviderDraft["apiType"],
              }))
            }
          >
            <option value="CHAT_COMPLETIONS">CHAT_COMPLETIONS</option>
            <option value="RESPONSES">RESPONSES</option>
          </select>
        </FormField>
        <FormField
          id="provider-base-url"
          label={t("settings.modelProviders.baseUrl")}
          error={validationMessage(errors.baseUrl)}
        >
          <input
            className="rounded border border-line bg-input px-3 py-2 text-ink"
            value={draft.baseUrl}
            disabled={props.busy}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                baseUrl: event.target.value,
              }))
            }
          />
        </FormField>
        <FormField
          id="provider-model"
          label={t("settings.modelProviders.model")}
          error={validationMessage(errors.model)}
        >
          <input
            className="rounded border border-line bg-input px-3 py-2 text-ink"
            value={draft.model}
            disabled={props.busy}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                model: event.target.value,
              }))
            }
          />
        </FormField>
        {props.mode === "edit" ? (
          <p className="text-xs text-success">
            {t("settings.modelProviders.storedKey")}
          </p>
        ) : null}
        <FormField
          id="provider-api-key"
          label={t("settings.modelProviders.apiKey")}
          hint={
            props.mode === "edit"
              ? t("settings.modelProviders.keepCurrentKey")
              : undefined
          }
          error={validationMessage(errors.apiKey)}
        >
          <input
            type="password"
            autoComplete="new-password"
            className="rounded border border-line bg-input px-3 py-2 text-ink"
            value={apiKey}
            disabled={props.busy}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </FormField>
        <label className="flex items-center gap-2 text-sm text-ink-muted">
          <input
            type="checkbox"
            checked={draft.enabled}
            disabled={props.busy}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                enabled: event.target.checked,
              }))
            }
          />
          {t("settings.modelProviders.enabled")}
        </label>
        {props.error ? (
          <p role="alert" className="text-sm text-danger">
            <strong>{props.error.code}</strong>: {props.error.message}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" disabled={props.busy} onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={props.busy}>
            {t("settings.modelProviders.save")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
