import type { ActivityItem } from "./activity.mjs";
export function activityViewState(input: { loading: boolean; error?: unknown; entries: ActivityItem[] }): "loading" | "error" | "content" | "empty";
