/** Returns newly pending approval records after the initial app snapshot. */
export function findNewApprovalRequests(previousIds, items) {
  if (previousIds === null) return [];
  const known = new Set(previousIds);
  return items.filter((item) => item && Number.isInteger(item.id) && !known.has(item.id));
}
