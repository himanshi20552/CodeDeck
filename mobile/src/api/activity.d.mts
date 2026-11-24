export type ActivityItem = {
  key: string;
  kind: string;
  title: string;
  detail: string;
  time: string | null;
  status: string;
};

export function normalizeActivity(monitoring: unknown, approvals?: unknown): ActivityItem[];
export type ActivityItem = { key: string; kind: string; title: string; detail: string; time: string | null; status: string };
export function normalizeActivity(monitoringValue: unknown, approvalsValue?: unknown): ActivityItem[];
