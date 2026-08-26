import { create } from "zustand";

type TerminalUiState = {
  activeTabId: string | null;
  tabOrder: string[];
  focusRevision: number;
  setActiveTab: (tabId: string) => void;
  reconcileTabs: (tabIds: readonly string[]) => void;
  requestFocus: () => void;
  reset: () => void;
};

const defaults = {
  activeTabId: null as string | null,
  tabOrder: [] as string[],
  focusRevision: 0,
};

export const useTerminalUiStore = create<TerminalUiState>((set) => ({
  ...defaults,
  setActiveTab: (activeTabId) =>
    set((state) =>
      state.tabOrder.includes(activeTabId) ? { activeTabId } : {},
    ),
  reconcileTabs: (tabIds) =>
    set((state) => {
      const tabOrder = [...tabIds];
      const activeTabId =
        state.activeTabId && tabOrder.includes(state.activeTabId)
          ? state.activeTabId
          : (tabOrder[tabOrder.length - 1] ?? null);
      return { tabOrder, activeTabId };
    }),
  requestFocus: () =>
    set((state) => ({ focusRevision: state.focusRevision + 1 })),
  reset: () => set({ ...defaults, tabOrder: [] }),
}));
