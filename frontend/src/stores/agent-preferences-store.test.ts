// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import {
  AGENT_PREFERENCES_STORAGE_KEY,
  migrateAgentPreferences,
  useAgentPreferencesStore,
} from "./agent-preferences-store";

describe("agent preferences", () => {
  beforeEach(() => {
    localStorage.clear();
    useAgentPreferencesStore.getState().reset();
  });

  it("persists only one valid UUID preference", () => {
    useAgentPreferencesStore
      .getState()
      .setPreferredApiConfigId("10000000-0000-4000-8000-000000000001");

    const persisted = JSON.parse(
      localStorage.getItem(AGENT_PREFERENCES_STORAGE_KEY) ?? "{}",
    );
    expect(persisted.state).toEqual({
      preferredApiConfigId: "10000000-0000-4000-8000-000000000001",
    });
  });

  it("fails closed for unknown versions and invalid IDs", () => {
    expect(
      migrateAgentPreferences({ preferredApiConfigId: "not-a-uuid" }, 1),
    ).toEqual({ preferredApiConfigId: null });
    expect(
      migrateAgentPreferences(
        {
          preferredApiConfigId:
            "10000000-0000-4000-8000-000000000001",
        },
        99,
      ),
    ).toEqual({ preferredApiConfigId: null });
  });

  it("drops unknown persisted keys during migration", () => {
    expect(
      migrateAgentPreferences(
        {
          preferredApiConfigId:
            "10000000-0000-4000-8000-000000000001",
          conversationId: "must-not-persist",
          apiKey: "must-not-persist",
        },
        1,
      ),
    ).toEqual({
      preferredApiConfigId: "10000000-0000-4000-8000-000000000001",
    });
  });
});
