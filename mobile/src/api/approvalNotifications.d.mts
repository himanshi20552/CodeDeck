export function findNewApprovalRequests<T extends { id: number }>(previousIds: number[] | null, items: T[]): T[];
