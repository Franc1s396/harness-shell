import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import {
  DEFAULT_AGENT_WIDTH,
  DEFAULT_SIDEBAR_WIDTH,
  MAX_AGENT_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_AGENT_WIDTH,
  MIN_SIDEBAR_WIDTH,
  type WidthBounds,
} from "../features/shell/workspace-layout";

export type WorkspaceActivity = "connections" | "sftp" | "approval" | "settings";
export type ConnectionDialogState =
  | { kind: "closed" }
  | { kind: "create" }
  | { kind: "edit"; connectionId: string };

const STORAGE_KEY = "harness-shell.workspace-ui";

const clampToBounds = (width: number, bounds: WidthBounds) =>
  Math.min(bounds.max, Math.max(bounds.min, Math.round(width)));

export const sidebarWidthBounds = (): WidthBounds => ({
  min: MIN_SIDEBAR_WIDTH,
  max: MAX_SIDEBAR_WIDTH,
});

export const clampSidebarWidth = (width: number) =>
  clampToBounds(width, sidebarWidthBounds());

export const requestedAgentWidthBounds = (): WidthBounds => ({
  min: MIN_AGENT_WIDTH,
  max: MAX_AGENT_WIDTH,
});

export const clampAgentWidth = (width: number) =>
  clampToBounds(width, requestedAgentWidthBounds());

export type PersistedWorkspaceState = {
  sidebarVisible: boolean;
  sidebarWidth: number;
  agentVisible: boolean;
  agentWidth: number;
  activeActivity: WorkspaceActivity;
};

type WorkspaceUiState = PersistedWorkspaceState & {
  mediumViewportDrawerOpen: boolean;
  connectionDialog: ConnectionDialogState;
  layoutRevision: number;
  setSidebarVisible: (visible: boolean) => void;
  setSidebarWidth: (width: number, viewportWidth?: number) => void;
  setAgentVisible: (visible: boolean) => void;
  setAgentWidth: (width: number) => void;
  setActiveActivity: (activity: WorkspaceActivity) => void;
  setMediumViewportDrawerOpen: (open: boolean) => void;
  openCreateConnection: () => void;
  openEditConnection: (connectionId: string) => void;
  closeConnectionDialog: () => void;
  bumpLayoutRevision: () => void;
  reset: () => void;
};

const defaults: PersistedWorkspaceState = {
  sidebarVisible: true,
  sidebarWidth: DEFAULT_SIDEBAR_WIDTH,
  agentVisible: false,
  agentWidth: DEFAULT_AGENT_WIDTH,
  activeActivity: "connections",
};

const transient = {
  mediumViewportDrawerOpen: false,
  connectionDialog: { kind: "closed" } as ConnectionDialogState,
  layoutRevision: 0,
};

const validWidthOrDefault = (
  value: unknown,
  bounds: WidthBounds,
  defaultValue: number,
) =>
  typeof value === "number" &&
  Number.isFinite(value) &&
  value >= bounds.min &&
  value <= bounds.max
    ? Math.round(value)
    : defaultValue;

const validActivity = (value: unknown): WorkspaceActivity => {
  if (value === "sftp" || value === "approval" || value === "settings") {
    return value;
  }
  return "connections";
};

const sanitizeV2 = (value: unknown): PersistedWorkspaceState => {
  const candidate =
    typeof value === "object" && value !== null
      ? (value as Partial<PersistedWorkspaceState>)
      : {};
  return {
    sidebarVisible:
      typeof candidate.sidebarVisible === "boolean"
        ? candidate.sidebarVisible
        : defaults.sidebarVisible,
    sidebarWidth: validWidthOrDefault(
      candidate.sidebarWidth,
      sidebarWidthBounds(),
      defaults.sidebarWidth,
    ),
    agentVisible:
      typeof candidate.agentVisible === "boolean"
        ? candidate.agentVisible
        : defaults.agentVisible,
    agentWidth: validWidthOrDefault(
      candidate.agentWidth,
      requestedAgentWidthBounds(),
      defaults.agentWidth,
    ),
    activeActivity: validActivity(candidate.activeActivity),
  };
};

const migrateV1 = (value: unknown): PersistedWorkspaceState => {
  const candidate =
    typeof value === "object" && value !== null
      ? (value as { sidebarVisible?: unknown; sidebarWidth?: unknown })
      : {};
  return {
    ...defaults,
    sidebarVisible:
      typeof candidate.sidebarVisible === "boolean"
        ? candidate.sidebarVisible
        : defaults.sidebarVisible,
    sidebarWidth: validWidthOrDefault(
      candidate.sidebarWidth,
      sidebarWidthBounds(),
      defaults.sidebarWidth,
    ),
  };
};

export const persistedWorkspaceState = (
  state:
    | Pick<WorkspaceUiState, keyof PersistedWorkspaceState>
    | Record<string, unknown>,
): PersistedWorkspaceState => sanitizeV2(state);

export const migrateWorkspaceState = (
  persisted: unknown,
  version: number,
): PersistedWorkspaceState => {
  if (version === 3) return sanitizeV2(persisted);
  if (version === 2) return sanitizeV2(persisted);
  if (version === 1) return migrateV1(persisted);
  return { ...defaults };
};

export const useWorkspaceUiStore = create<WorkspaceUiState>()(
  persist(
    (set) => ({
      ...defaults,
      ...transient,
      setSidebarVisible: (sidebarVisible) =>
        set((state) => ({
          sidebarVisible,
          layoutRevision: state.layoutRevision + 1,
        })),
      setSidebarWidth: (width) =>
        set({ sidebarWidth: clampSidebarWidth(width) }),
      setAgentVisible: (agentVisible) =>
        set((state) => ({
          agentVisible,
          layoutRevision: state.layoutRevision + 1,
        })),
      setAgentWidth: (width) => set({ agentWidth: clampAgentWidth(width) }),
      setActiveActivity: (activeActivity) => set({ activeActivity }),
      setMediumViewportDrawerOpen: (mediumViewportDrawerOpen) =>
        set({ mediumViewportDrawerOpen }),
      openCreateConnection: () =>
        set({ connectionDialog: { kind: "create" } }),
      openEditConnection: (connectionId) =>
        set({ connectionDialog: { kind: "edit", connectionId } }),
      closeConnectionDialog: () =>
        set((state) => ({
          connectionDialog: { kind: "closed" },
          layoutRevision: state.layoutRevision + 1,
        })),
      bumpLayoutRevision: () =>
        set((state) => ({ layoutRevision: state.layoutRevision + 1 })),
      reset: () => set({ ...defaults, ...transient }),
    }),
    {
      name: STORAGE_KEY,
      version: 3,
      storage: createJSONStorage(() => localStorage),
      partialize: (state: WorkspaceUiState) => ({
        sidebarVisible: state.sidebarVisible,
        sidebarWidth: state.sidebarWidth,
        agentVisible: state.agentVisible,
        agentWidth: state.agentWidth,
        activeActivity: state.activeActivity,
      }),
      migrate: migrateWorkspaceState,
      merge: (persisted, current) => ({
        ...current,
        ...sanitizeV2(persisted),
      }),
      onRehydrateStorage: () => (_state, error) => {
        if (error) localStorage.removeItem(STORAGE_KEY);
      },
    },
  ),
);
