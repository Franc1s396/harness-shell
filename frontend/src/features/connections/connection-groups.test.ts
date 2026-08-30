import { describe, expect, it } from "vitest";

import type { ConnectionProfile } from "../../api/ssh";
import { groupVisibleConnections } from "./connection-groups";

const profile = (
  id: string,
  name: string,
  group: string | null,
  favorite = false,
): ConnectionProfile => ({
  connection_id: id,
  version: 1,
  display_name: name,
  group_name: group,
  host: `${id}.example`,
  port: 22,
  username: "root",
  auth_kind: "password",
  credential_id: `cred-${id}`,
  passphrase_credential_id: null,
  proxy_jump_id: null,
  favorite,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
});

describe("connection grouping", () => {
  it("filters then orders favorites before names inside each group", () => {
    const groups = groupVisibleConnections(
      [
        profile("b", "Beta", "Prod"),
        profile("a", "Alpha", "Prod", true),
        profile("c", "Cache", null),
      ],
      "prod",
    );

    expect(groups).toEqual([
      {
        name: "Prod",
        connections: [
          expect.objectContaining({ connection_id: "a" }),
          expect.objectContaining({ connection_id: "b" }),
        ],
      },
    ]);
  });
});
