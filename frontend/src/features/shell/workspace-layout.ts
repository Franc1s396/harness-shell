export const AGENT_COLLAPSE_WIDTH = 1280;
export const SIDEBAR_COLLAPSE_WIDTH = 1040;

export const resolveResponsiveWorkspace = (
  viewportWidth: number,
  requestedSidebarVisible: boolean,
  requestedAgentExpanded: boolean,
) => ({
  sidebarVisible:
    viewportWidth < SIDEBAR_COLLAPSE_WIDTH
      ? false
      : requestedSidebarVisible,
  agentRailExpanded:
    viewportWidth < AGENT_COLLAPSE_WIDTH ? false : requestedAgentExpanded,
});
