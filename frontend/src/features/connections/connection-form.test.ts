import { describe, expect, it } from "vitest";

import { emptyConnectionForm, validateConnectionForm } from "./connection-form";

describe("connection form", () => {
  it("locates the first error on the correct tab", () => {
    const result = validateConnectionForm(emptyConnectionForm, {
      existingAuthKind: null,
      hasImportedKey: false,
      passwordPresent: false,
    });

    expect(result.firstTab).toBe("basic");
    expect(result.fields).toMatchObject({
      displayName: "required",
      host: "required",
      username: "required",
      password: "required",
    });
  });

  it("allows an unchanged secret while editing", () => {
    const result = validateConnectionForm(
      {
        ...emptyConnectionForm,
        displayName: "Prod",
        host: "prod.example",
        username: "root",
      },
      {
        existingAuthKind: "password",
        hasImportedKey: false,
        passwordPresent: false,
      },
    );

    expect(result.fields.password).toBeUndefined();
  });

  it("requires a new secret when editing changes authentication method", () => {
    const values = {
      ...emptyConnectionForm,
      displayName: "Prod",
      host: "prod.example",
      username: "root",
      authKind: "private_key" as const,
    };

    expect(
      validateConnectionForm(values, {
        existingAuthKind: "password",
        hasImportedKey: false,
        passwordPresent: false,
      }).fields.privateKey,
    ).toBe("required");
  });
});
