import assert from "node:assert/strict";
import test from "node:test";
import { normalizeActivity } from "./activity.mjs";
import { activityViewState } from "./activityState.mjs";
import { findNewApprovalRequests } from "./approvalNotifications.mjs";

test("empty response renders as an empty activity list", () => {
  assert.deepEqual(normalizeActivity({ alerts: [], deployment_events: [], file_activity: [] }, { items: [] }), []);
  assert.deepEqual(normalizeActivity(null, null), []);
});

test("normalizes multiple event types and sorts by persisted timestamp", () => {
  const result = normalizeActivity({
    alerts: [{ id: 7, title: "Rollout failed", message: "Pods not ready", severity: "critical", last_seen: "2026-11-24T10:00:00Z" }],
    deployment_events: [{ id: 12, environment: "staging", version: "blue", status: "FAILED", deployment_type: "remote_staging", created_at: "2026-11-24T11:00:00Z", logs: [{ message: "Smoke check failed" }] }],
    file_activity: [{ id: 1, event_type: "modified", path: "src/api.py", occurred_at: "2026-11-24T09:00:00Z", workflow_context: "analysis" }],
  }, { items: [{ id: 3, project_id: 2, project: { name: "payments" }, release_version: "v2", events: [{ id: 4, to_status: "Approved", occurred_at: "2026-11-24T12:00:00Z" }] }] });
  assert.deepEqual(result.map((item) => item.kind), ["Release", "Deployment", "Alert", "Repository"]);
  assert.equal(result[1].detail, "Smoke check failed");
  assert.equal(result[3].title, "modified · src/api.py");
});

test("missing optional properties and null array entries are safe", () => {
  const result = normalizeActivity({
    alerts: [null, {}],
    deployment_events: [null, { id: 2, logs: null }],
    file_activity: [null, { id: 3 }],
  }, { items: [null, { id: 4, events: [null, {}] }] });
  assert.equal(result.length, 4);
  assert.ok(result.every((item) => typeof item.title === "string" && typeof item.detail === "string"));
  assert.ok(result.every((item) => item.time === null));
});

test("API error state remains a screen concern instead of crashing event conversion", () => {
  assert.deepEqual(normalizeActivity(undefined, { items: null }), []);
  assert.equal(normalizeActivity({ alerts: "unexpected", deployment_events: {}, file_activity: null }).length, 0);
});

test("activity screen distinguishes loading, API failure, empty and populated responses", () => {
  assert.equal(activityViewState({ loading: true, error: "offline", entries: [] }), "loading");
  assert.equal(activityViewState({ loading: false, error: "server error", entries: [] }), "error");
  assert.equal(activityViewState({ loading: false, entries: [] }), "empty");
  assert.equal(activityViewState({ loading: false, entries: normalizeActivity({ alerts: [{ id: 1, title: "Alert" }] }) }), "content");
});

test("approval notifications identify new pending requests and ignore initial history", () => {
  const previous = [{ id: 2 }];
  assert.deepEqual(findNewApprovalRequests(null, previous), []);
  assert.deepEqual(findNewApprovalRequests([2], [{ id: 2 }, { id: 3 }]), [{ id: 3 }]);
  assert.deepEqual(findNewApprovalRequests([2, 3], [{ id: 2 }, { id: 3 }]), []);
});
