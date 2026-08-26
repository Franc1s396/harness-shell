import type { ConnectionProfile } from "../../api/ssh";

export type ConnectionGroup = {
  name: string | null;
  connections: ConnectionProfile[];
};

export const groupVisibleConnections = (
  connections: readonly ConnectionProfile[],
  query: string,
): ConnectionGroup[] => {
  const normalized = query.trim().toLocaleLowerCase();
  const grouped = new Map<string | null, ConnectionProfile[]>();

  for (const connection of connections) {
    const haystack = `${connection.display_name} ${connection.host} ${connection.group_name ?? ""}`.toLocaleLowerCase();
    if (normalized && !haystack.includes(normalized)) continue;

    const group = connection.group_name?.trim() || null;
    grouped.set(group, [...(grouped.get(group) ?? []), connection]);
  }

  return [...grouped.entries()]
    .sort(([left], [right]) =>
      (left ?? "\uffff").localeCompare(right ?? "\uffff"),
    )
    .map(([name, items]) => ({
      name,
      connections: items.sort(
        (left, right) =>
          Number(right.favorite) - Number(left.favorite) ||
          left.display_name.localeCompare(right.display_name),
      ),
    }));
};
