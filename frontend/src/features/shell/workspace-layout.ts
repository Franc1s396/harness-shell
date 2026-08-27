export const MIN_SUPPORTED_VIEWPORT = 960;
export const INLINE_SIDEBAR_VIEWPORT = 1440;
export const ACTIVITY_RAIL_WIDTH = 44;
export const SEPARATOR_WIDTH = 4;
export const MIN_TERMINAL_WIDTH = 560;
export const DEFAULT_SIDEBAR_WIDTH = 280;
export const MIN_SIDEBAR_WIDTH = 240;
export const MAX_SIDEBAR_WIDTH = 380;
export const DEFAULT_AGENT_WIDTH = 480;
export const MIN_AGENT_WIDTH = 320;
export const MAX_AGENT_WIDTH = 640;

export type ResponsiveWorkspace = {
  sidebarInline: boolean;
  sidebarDrawerAvailable: boolean;
  agentVisible: boolean;
};

export type WidthBounds = {
  min: number;
  max: number;
};

type AgentWidthBudget = {
  viewportWidth: number;
  sidebarInline: boolean;
  sidebarWidth: number;
};

type TerminalWidthBudget = AgentWidthBudget & {
  agentVisible: boolean;
  agentWidth: number;
};

export const resolveResponsiveWorkspace = (
  viewportWidth: number,
  requestedSidebarVisible: boolean,
  requestedAgentVisible: boolean,
): ResponsiveWorkspace => {
  if (viewportWidth < MIN_SUPPORTED_VIEWPORT) {
    throw new Error(`Unsupported workspace width: ${viewportWidth}`);
  }
  const wide = viewportWidth >= INLINE_SIDEBAR_VIEWPORT;
  return {
    sidebarInline: wide && requestedSidebarVisible,
    sidebarDrawerAvailable: !wide,
    agentVisible: requestedAgentVisible,
  };
};

export const agentWidthBounds = ({
  viewportWidth,
  sidebarInline,
  sidebarWidth,
}: AgentWidthBudget): WidthBounds => {
  const sidebarBudget = sidebarInline
    ? sidebarWidth + SEPARATOR_WIDTH
    : 0;
  const available =
    viewportWidth -
    ACTIVITY_RAIL_WIDTH -
    sidebarBudget -
    SEPARATOR_WIDTH -
    MIN_TERMINAL_WIDTH;
  const max = Math.min(MAX_AGENT_WIDTH, Math.floor(available));
  if (max < MIN_AGENT_WIDTH) {
    throw new Error(
      `Workspace width ${viewportWidth} cannot preserve the terminal and Agent minimum widths.`,
    );
  }
  return { min: MIN_AGENT_WIDTH, max };
};

export const resolveEffectiveAgentWidth = (
  requestedWidth: number,
  bounds: WidthBounds,
): number => Math.min(bounds.max, Math.max(bounds.min, Math.round(requestedWidth)));

export const resolveTerminalWidth = ({
  viewportWidth,
  sidebarInline,
  sidebarWidth,
  agentVisible,
  agentWidth,
}: TerminalWidthBudget): number => {
  const sidebarBudget = sidebarInline
    ? sidebarWidth + SEPARATOR_WIDTH
    : 0;
  const agentBudget = agentVisible ? agentWidth + SEPARATOR_WIDTH : 0;
  return (
    viewportWidth - ACTIVITY_RAIL_WIDTH - sidebarBudget - agentBudget
  );
};
