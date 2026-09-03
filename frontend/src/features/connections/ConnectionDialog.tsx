import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { useTranslation } from "react-i18next";

import type {
  ConnectionProfile,
  ConnectionProfileInput,
} from "../../api/ssh";
import { Button } from "../../components/ui/controls";
import { Dialog } from "../../components/ui/Dialog";
import { FormField, SelectField } from "../../components/ui/fields";
import {
  emptyConnectionForm,
  validateConnectionForm,
  type ConnectionFormErrors,
  type ConnectionFormField,
  type ConnectionFormTab,
  type ConnectionFormValues,
  type ConnectionSubmitIntent,
} from "./connection-form";
import { selectPrivateKeyText } from "./private-key-file";

export type ConnectionDialogProps = {
  open: boolean;
  connection: ConnectionProfile | null;
  connections: ConnectionProfile[];
  onClose: () => void;
  onSubmit: (
    input: ConnectionProfileInput,
    intent: ConnectionSubmitIntent,
  ) => Promise<void>;
  onDelete: (connectionId: string) => Promise<void>;
};

const tabs: ConnectionFormTab[] = ["basic", "authentication", "advanced"];
const inputClass =
  "w-full rounded border border-line bg-input px-2 py-1.5 text-ink disabled:opacity-50";

const valuesFromConnection = (
  connection: ConnectionProfile | null,
): ConnectionFormValues =>
  connection
    ? {
        displayName: connection.display_name,
        groupName: connection.group_name ?? "",
        host: connection.host,
        port: String(connection.port),
        username: connection.username,
        authKind: connection.auth_kind,
        proxyJumpId: connection.proxy_jump_id ?? "",
        favorite: connection.favorite,
      }
    : { ...emptyConnectionForm };

export function ConnectionDialog({
  open,
  connection,
  connections,
  onClose,
  onSubmit,
  onDelete,
}: ConnectionDialogProps) {
  const { t } = useTranslation();
  const [values, setValues] = useState(() => valuesFromConnection(connection));
  const [activeTab, setActiveTab] = useState<ConnectionFormTab>("basic");
  const [errors, setErrors] = useState<ConnectionFormErrors["fields"]>({});
  const [hasImportedKey, setHasImportedKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  const passphraseRef = useRef<HTMLInputElement>(null);
  const importedKeyRef = useRef<string | null>(null);
  const dialogGenerationRef = useRef(0);
  const fieldRefs = useRef<Partial<Record<ConnectionFormField, HTMLElement>>>({});
  const tabRefs = useRef<Partial<Record<ConnectionFormTab, HTMLButtonElement>>>({});

  const clearSecrets = () => {
    if (passwordRef.current) passwordRef.current.value = "";
    if (passphraseRef.current) passphraseRef.current.value = "";
  };

  useEffect(() => {
    dialogGenerationRef.current += 1;
    setBusy(false);
    if (open) {
      setValues(valuesFromConnection(connection));
      setActiveTab("basic");
      setErrors({});
      setOperationError(null);
      setDeleteConfirmOpen(false);
    }
    clearSecrets();
    importedKeyRef.current = null;
    setHasImportedKey(false);
  }, [open, connection]);

  const updateValue = <K extends keyof ConnectionFormValues>(
    key: K,
    value: ConnectionFormValues[K],
  ) => setValues((current) => ({ ...current, [key]: value }));

  const onTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    tab: ConnectionFormTab,
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const offset = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(tabs.indexOf(tab) + offset + tabs.length) % tabs.length];
    setActiveTab(next);
    tabRefs.current[next]?.focus();
  };

  const errorText = (field: ConnectionFormField) => {
    const error = errors[field];
    return error ? t(`connections.${error}`) : undefined;
  };

  const focusFirstError = (
    firstTab: ConnectionFormTab,
    firstField: ConnectionFormField,
  ) => {
    setActiveTab(firstTab);
    window.setTimeout(() => fieldRefs.current[firstField]?.focus(), 0);
  };

  const submit = async (intent: ConnectionSubmitIntent) => {
    const validation = validateConnectionForm(values, {
      existingAuthKind: connection?.auth_kind ?? null,
      hasImportedKey,
      passwordPresent: Boolean(passwordRef.current?.value),
    });
    setErrors(validation.fields);
    if (validation.firstTab && validation.firstField) {
      focusFirstError(validation.firstTab, validation.firstField);
      return;
    }

    setBusy(true);
    setOperationError(null);
    const generation = dialogGenerationRef.current;
    try {
      const credentialSecret = values.authKind === "password"
        ? passwordRef.current?.value || null
        : importedKeyRef.current;
      const passphraseSecret = values.authKind === "private_key"
        ? passphraseRef.current?.value || null
        : null;

      await onSubmit(
        {
          display_name: values.displayName.trim(),
          group_name: values.groupName.trim() || null,
          host: values.host.trim(),
          port: Number(values.port),
          username: values.username.trim(),
          auth_kind: values.authKind,
          credential_secret: credentialSecret,
          passphrase_secret: passphraseSecret,
          proxy_jump_id: values.proxyJumpId || null,
          favorite: values.favorite,
        },
        intent,
      );
    } catch (error) {
      if (dialogGenerationRef.current === generation) {
        setOperationError(
          error instanceof Error ? error.message : "Connection operation failed.",
        );
      }
    } finally {
      if (dialogGenerationRef.current === generation) {
        clearSecrets();
        importedKeyRef.current = null;
        setHasImportedKey(false);
        setBusy(false);
      }
    }
  };

  const close = () => {
    clearSecrets();
    importedKeyRef.current = null;
    setHasImportedKey(false);
    setOperationError(null);
    onClose();
  };

  return (
    <>
      <Dialog
        open={open}
        busy={busy || deleteConfirmOpen}
        title={connection ? t("connections.edit") : t("connections.new")}
        onClose={close}
      >
        <form
          className="mt-4 grid gap-4"
          onSubmit={(event) => event.preventDefault()}
        >
          <div role="tablist" className="flex gap-1 border-b border-line">
            {tabs.map((tab) => (
              <button
                key={tab}
                ref={(element) => {
                  if (element) tabRefs.current[tab] = element;
                }}
                type="button"
                role="tab"
                aria-selected={activeTab === tab}
                aria-controls={`connection-panel-${tab}`}
                className="border-b-2 border-transparent px-3 py-2 text-sm text-ink-muted aria-selected:border-accent aria-selected:text-ink"
                onClick={() => setActiveTab(tab)}
                onKeyDown={(event) => onTabKeyDown(event, tab)}
              >
                {t(`connections.${tab}`)}
              </button>
            ))}
          </div>

          <div
            id="connection-panel-basic"
            role="tabpanel"
            hidden={activeTab !== "basic"}
            className="grid grid-cols-2 gap-4"
          >
              <FormField
                id="connection-display-name"
                label={t("connections.displayName")}
                error={errorText("displayName")}
              >
                <input
                  ref={(element) => {
                    if (element) fieldRefs.current.displayName = element;
                  }}
                  className={inputClass}
                  value={values.displayName}
                  onChange={(event) =>
                    updateValue("displayName", event.target.value)
                  }
                />
              </FormField>
              <FormField
                id="connection-group"
                label={t("connections.group")}
              >
                <input
                  className={inputClass}
                  value={values.groupName}
                  onChange={(event) => updateValue("groupName", event.target.value)}
                />
              </FormField>
              <FormField
                id="connection-host"
                label={t("connections.host")}
                error={errorText("host")}
              >
                <input
                  ref={(element) => {
                    if (element) fieldRefs.current.host = element;
                  }}
                  className={inputClass}
                  value={values.host}
                  onChange={(event) => updateValue("host", event.target.value)}
                />
              </FormField>
              <FormField
                id="connection-port"
                label={t("connections.port")}
                error={errorText("port")}
              >
                <input
                  ref={(element) => {
                    if (element) fieldRefs.current.port = element;
                  }}
                  className={inputClass}
                  inputMode="numeric"
                  value={values.port}
                  onChange={(event) => updateValue("port", event.target.value)}
                />
              </FormField>
          </div>

          <div
            id="connection-panel-authentication"
            role="tabpanel"
            hidden={activeTab !== "authentication"}
            className="grid gap-4"
          >
              <FormField
                id="connection-username"
                label={t("connections.username")}
                error={errorText("username")}
              >
                <input
                  ref={(element) => {
                    if (element) fieldRefs.current.username = element;
                  }}
                  className={inputClass}
                  value={values.username}
                  onChange={(event) => updateValue("username", event.target.value)}
                />
              </FormField>
              <SelectField
                id="connection-auth-kind"
                label={t("connections.authMethod")}
              >
                <select
                  className={inputClass}
                  value={values.authKind}
                  onChange={(event) =>
                    updateValue(
                      "authKind",
                      event.target.value as ConnectionFormValues["authKind"],
                    )
                  }
                >
                  <option value="password">{t("connections.password")}</option>
                  <option value="private_key">{t("connections.privateKey")}</option>
                </select>
              </SelectField>
              {values.authKind === "password" ? (
                <FormField
                  id="connection-password"
                  label={t("connections.password")}
                  error={errorText("password")}
                  hint={connection ? t("connections.keepCurrent") : undefined}
                >
                  <input
                    ref={(element) => {
                      passwordRef.current = element;
                      if (element) fieldRefs.current.password = element;
                    }}
                    type="password"
                    autoComplete="new-password"
                    className={inputClass}
                  />
                </FormField>
              ) : (
                <div className="grid gap-4">
                  <FormField
                    id="connection-private-key"
                    label={t("connections.privateKey")}
                    error={errorText("privateKey")}
                    hint={
                      hasImportedKey
                        ? t("connections.keySelected")
                        : connection?.auth_kind === "private_key"
                          ? t("connections.keepCurrent")
                          : undefined
                    }
                  >
                    <button
                      ref={(element) => {
                        if (element) fieldRefs.current.privateKey = element;
                      }}
                      type="button"
                      className={`${inputClass} text-left`}
                      disabled={busy}
                      onClick={async () => {
                        try {
                          const privateKey = await selectPrivateKeyText();
                          if (privateKey !== null) {
                            importedKeyRef.current = privateKey;
                            setHasImportedKey(true);
                          }
                        } catch (error) {
                          setOperationError(
                            error instanceof Error
                              ? error.message
                              : "Private key selection failed.",
                          );
                        }
                      }}
                    >
                      {t("connections.importKey")}
                    </button>
                  </FormField>
                  <FormField
                    id="connection-passphrase"
                    label={t("connections.passphrase")}
                    hint={connection ? t("connections.keepCurrent") : undefined}
                  >
                    <input
                      ref={passphraseRef}
                      type="password"
                      autoComplete="new-password"
                      className={inputClass}
                    />
                  </FormField>
                </div>
              )}
          </div>

          <div
            id="connection-panel-advanced"
            role="tabpanel"
            hidden={activeTab !== "advanced"}
            className="grid gap-4"
          >
              <SelectField
                id="connection-proxy-jump"
                label={t("connections.proxyJump")}
              >
                <select
                  className={inputClass}
                  value={values.proxyJumpId}
                  onChange={(event) =>
                    updateValue("proxyJumpId", event.target.value)
                  }
                >
                  <option value="">{t("connections.direct")}</option>
                  {connections
                    .filter(
                      (candidate) =>
                        candidate.connection_id !== connection?.connection_id,
                    )
                    .map((candidate) => (
                      <option
                        key={candidate.connection_id}
                        value={candidate.connection_id}
                      >
                        {candidate.display_name}
                      </option>
                    ))}
                </select>
              </SelectField>
              <label className="flex items-center gap-2 text-sm text-ink-muted">
                <input
                  type="checkbox"
                  checked={values.favorite}
                  onChange={(event) =>
                    updateValue("favorite", event.target.checked)
                  }
                />
                {t("connections.favorite")}
              </label>
          </div>

          {operationError ? (
            <p role="alert" className="m-0 text-sm text-danger">
              {operationError}
            </p>
          ) : null}

          <footer className="flex items-center justify-between gap-3 border-t border-line pt-4">
            <div>
              {connection ? (
                <Button
                  variant="danger"
                  disabled={busy}
                  onClick={() => setDeleteConfirmOpen(true)}
                >
                  {t("common.delete")}
                </Button>
              ) : null}
            </div>
            <div className="flex gap-2">
              <Button variant="ghost" disabled={busy} onClick={close}>
                {t("common.cancel")}
              </Button>
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => void submit("save")}
              >
                {t("common.save")}
              </Button>
              <Button
                disabled={busy}
                onClick={() => void submit("save-and-connect")}
              >
                {t("common.saveAndConnect")}
              </Button>
            </div>
          </footer>
        </form>
      </Dialog>

      <Dialog
        open={deleteConfirmOpen}
        busy={busy}
        title={t("connections.deleteConfirmTitle")}
        onClose={() => setDeleteConfirmOpen(false)}
      >
        <p className="text-sm text-ink-muted">
          {t("connections.deleteConfirmBody")}
        </p>
        <div className="flex justify-end gap-2">
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => setDeleteConfirmOpen(false)}
          >
            {t("common.cancel")}
          </Button>
          <Button
            variant="danger"
            disabled={busy}
            onClick={async () => {
              if (!connection) return;
              setBusy(true);
              try {
                await onDelete(connection.connection_id);
                setDeleteConfirmOpen(false);
              } finally {
                setBusy(false);
              }
            }}
          >
            {t("connections.confirmDelete")}
          </Button>
        </div>
      </Dialog>
    </>
  );
}
