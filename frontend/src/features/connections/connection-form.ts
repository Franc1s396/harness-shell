export type ConnectionFormTab = "basic" | "authentication" | "advanced";
export type ConnectionSubmitIntent = "save" | "save-and-connect";

export type ConnectionFormValues = {
  displayName: string;
  groupName: string;
  host: string;
  port: string;
  username: string;
  authKind: "password" | "private_key";
  proxyJumpId: string;
  favorite: boolean;
};

export const emptyConnectionForm: ConnectionFormValues = {
  displayName: "",
  groupName: "",
  host: "",
  port: "22",
  username: "",
  authKind: "password",
  proxyJumpId: "",
  favorite: false,
};

export type ConnectionFormField =
  | "displayName"
  | "host"
  | "port"
  | "username"
  | "password"
  | "privateKey";

export type ConnectionFormErrors = {
  fields: Partial<Record<ConnectionFormField, "required" | "invalid">>;
  firstTab: ConnectionFormTab | null;
  firstField: ConnectionFormField | null;
};

export function validateConnectionForm(
  values: ConnectionFormValues,
  context: {
    existingAuthKind: "password" | "private_key" | null;
    hasImportedKey: boolean;
    passwordPresent: boolean;
  },
): ConnectionFormErrors {
  const fields: ConnectionFormErrors["fields"] = {};
  if (!values.displayName.trim()) fields.displayName = "required";
  if (!values.host.trim()) fields.host = "required";
  const port = Number(values.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    fields.port = "invalid";
  }
  if (!values.username.trim()) fields.username = "required";

  const keepsExistingPassword = context.existingAuthKind === "password";
  const keepsExistingKey = context.existingAuthKind === "private_key";
  if (
    values.authKind === "password" &&
    !keepsExistingPassword &&
    !context.passwordPresent
  ) {
    fields.password = "required";
  }
  if (
    values.authKind === "private_key" &&
    !keepsExistingKey &&
    !context.hasImportedKey
  ) {
    fields.privateKey = "required";
  }

  const firstTab =
    fields.displayName || fields.host || fields.port
      ? "basic"
      : fields.username || fields.password || fields.privateKey
        ? "authentication"
        : null;
  const fieldOrder: ConnectionFormField[] = [
    "displayName",
    "host",
    "port",
    "username",
    "password",
    "privateKey",
  ];
  const firstField = fieldOrder.find((field) => fields[field]) ?? null;
  return { fields, firstTab, firstField };
}
