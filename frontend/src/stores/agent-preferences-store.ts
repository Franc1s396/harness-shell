import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export const AGENT_PREFERENCES_STORAGE_KEY =
  "harness-shell.agent-preferences";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type PersistedAgentPreferences = {
  preferredApiConfigId: string | null;
};

type AgentPreferencesState = PersistedAgentPreferences & {
  setPreferredApiConfigId: (value: string | null) => void;
  reset: () => void;
};

const defaults: PersistedAgentPreferences = {
  preferredApiConfigId: null,
};

export const sanitizeAgentPreferences = (
  value: unknown,
): PersistedAgentPreferences => {
  const candidate =
    typeof value === "object" && value !== null
      ? (value as { preferredApiConfigId?: unknown })
      : {};
  return {
    preferredApiConfigId:
      typeof candidate.preferredApiConfigId === "string" &&
      UUID.test(candidate.preferredApiConfigId)
        ? candidate.preferredApiConfigId
        : null,
  };
};

export const migrateAgentPreferences = (value: unknown, version: number) =>
  version === 1 ? sanitizeAgentPreferences(value) : { ...defaults };

export const useAgentPreferencesStore = create<AgentPreferencesState>()(
  persist(
    (set) => ({
      ...defaults,
      setPreferredApiConfigId: (preferredApiConfigId) =>
        set(sanitizeAgentPreferences({ preferredApiConfigId })),
      reset: () => set(defaults),
    }),
    {
      name: AGENT_PREFERENCES_STORAGE_KEY,
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: ({ preferredApiConfigId }) => ({ preferredApiConfigId }),
      migrate: migrateAgentPreferences,
      merge: (persisted, current) => ({
        ...current,
        ...sanitizeAgentPreferences(persisted),
      }),
      onRehydrateStorage: () => (_state, error) => {
        if (error) {
          localStorage.removeItem(AGENT_PREFERENCES_STORAGE_KEY);
        }
      },
    },
  ),
);
