import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type WorkspaceActivity = "connections" | "terminal";
export type ConnectionDialogState =
  | { kind: "closed" }
  | { kind: "create" }
  | { kind: "edit"; connectionId: string };

const STORAGE_KEY = "harness-shell.workspace-ui";
const MIN_SIDEBAR = 240;
const MAX_SIDEBAR = 420;
const FIXED_CHROME = 48 + 44;
const MIN_TERMINAL = 560;

export const sidebarWidthBounds = (viewportWidth: number) => ({
  min: MIN_SIDEBAR,
  max: Math.max(
    MIN_SIDEBAR,
    Math.min(MAX_SIDEBAR, viewportWidth - FIXED_CHROME - MIN_TERMINAL),
  ),
});

export const clampSidebarWidth = (width: number, viewportWidth: number) => {
  const { min, max } = sidebarWidthBounds(viewportWidth);
  return Math.min(max, Math.max(min, Math.round(width)));
};

export type PersistedWorkspaceState = {
  sidebarVisible: boolean;
  sidebarWidth: number;
  activeActivity: WorkspaceActivity;
};

type WorkspaceUiState = PersistedWorkspaceState & {
  agentRailExpanded: boolean;
  connectionDialog: ConnectionDialogState;
  layoutRevision: number;
  setSidebarVisible: (visible: boolean) => void;
  setSidebarWidth: (width: number, viewportWidth?: number) => void;
  setActiveActivity: (activity: WorkspaceActivity) => void;
  setAgentRailExpanded: (expanded: boolean) => void;
  openCreateConnection: () => void;
  openEditConnection: (connectionId: string) => void;
  closeConnectionDialog: () => void;
  bumpLayoutRevision: () => void;
  reset: () => void;
};

const defaults: PersistedWorkspaceState = {
  sidebarVisible: true,
  sidebarWidth: 280,
  activeActivity: "connections",
};

const transient = {
  agentRailExpanded: false,
  connectionDialog: { kind: "closed" } as ConnectionDialogState,
  layoutRevision: 0,
};

const sanitize = (value: unknown): PersistedWorkspaceState => {
  const candidate =
    typeof value === "object" && value !== null
      ? (value as Partial<PersistedWorkspaceState>)
      : {};
  return {
    sidebarVisible:
      typeof candidate.sidebarVisible === "boolean"
        ? candidate.sidebarVisible
        : defaults.sidebarVisible,
    sidebarWidth: clampSidebarWidth(
      typeof candidate.sidebarWidth === "number"
        ? candidate.sidebarWidth
        : defaults.sidebarWidth,
      window.innerWidth,
    ),
    activeActivity:
      candidate.activeActivity === "terminal" ? "terminal" : "connections",
  };
};

export const persistedWorkspaceState = (
  state: Pick<WorkspaceUiState, keyof PersistedWorkspaceState> | Record<string, unknown>,
): PersistedWorkspaceState => sanitize(state);

export const migrateWorkspaceState = (
  persisted: unknown,
  version: number,
): PersistedWorkspaceState => (version === 1 ? sanitize(persisted) : defaults);

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
      setSidebarWidth: (width, viewportWidth = window.innerWidth) =>
        set({ sidebarWidth: clampSidebarWidth(width, viewportWidth) }),
      setActiveActivity: (activeActivity) => set({ activeActivity }),
      setAgentRailExpanded: (agentRailExpanded) => set({ agentRailExpanded }),
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
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (state: WorkspaceUiState) => persistedWorkspaceState(state),
      migrate: migrateWorkspaceState,
      merge: (persisted, current) => ({ ...current, ...sanitize(persisted) }),
      onRehydrateStorage: () => (_state, error) => {
        if (error) localStorage.removeItem(STORAGE_KEY);
      },
    },
  ),
);
