import { expect, test, type Page } from "@playwright/test";
import {
  NA,
  NB,
  NC,
  L1,
  ROOT_A,
  ROOT_B,
  ROOT_BAD,
  ROOT_FUTURE,
  V,
  event,
  eventsBody,
  held,
  listBody,
  listItem,
  mockContract,
  planA,
  planB,
  planBad,
  type Reply
} from "./managed-helpers";

function activePlan() {
  const plan = JSON.parse(planA());
  Object.assign(
    plan.nodes.find((n: { id: string }) => n.id === NB),
    { work: "in_progress", attemptId: "b1", attemptEpoch: 1 }
  );
  Object.assign(
    plan.readiness.leaves.find((n: { nodeId: string }) => n.nodeId === NB),
    { work: "in_progress", ready: false, attemptId: "b1", attemptEpoch: 1 }
  );
  return JSON.stringify(plan);
}
const noEvents = eventsBody([], null);
const trace = (attemptId = "a1") =>
  JSON.stringify({
    contractVersion: V,
    nodeId: NA,
    attemptId,
    attemptEpoch: 1,
    claimKey: attemptId,
    status: "exited",
    reason: null,
    integrity: "verified",
    executionKind: "codex-cli",
    exit: { code: 0, killReason: null },
    prompt: { text: "Task context", bytes: 12 },
    records: [],
    nextAfterSeq: null,
    capped: false
  });
const routes = (extra: Record<string, Reply> = {}) => ({
  "/plans?": { body: listBody([listItem(ROOT_A, "Plan A"), listItem(ROOT_B, "Plan B")]) },
  [`/plans/${ROOT_A}/events`]: {
    body: eventsBody([event(1, NA), { ...event(2, NA, "attempt_finished"), workTo: "done" }], null)
  },
  [`/plans/${ROOT_A}`]: { body: activePlan() },
  [`/plans/${ROOT_B}/events`]: { body: noEvents },
  [`/plans/${ROOT_B}`]: { body: planB() },
  [`/nodes/${NA}/events`]: { body: eventsBody([event(1, NA)], null) },
  [`/nodes/${NA}/attempts/a1/trace`]: { body: trace() },
  ...extra
});
let guard: Awaited<ReturnType<typeof mockContract>> | null = null;
test.afterEach(() => {
  expect(guard?.nonGet ?? []).toEqual([]);
  guard = null;
});
async function open(page: Page, replies = routes()) {
  guard = await mockContract(page, replies);
  await page.goto("/");
  await page.getByRole("button", { name: "Tasks", exact: true }).click();
  await expect(page.getByTestId("tasks-coverage")).toContainText("plans loaded");
  return guard;
}

test("saved catalog spans plans; active excludes completed and unstarted work; board keeps the same filters", async ({
  page
}) => {
  const m = await open(page);
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(7);
  expect(m.requests.map((r) => r.url)).toContain("/api/plan-contract/v1/plans?limit=20");
  await page.getByTestId("tasks-scope-active").click();
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(1);
  await expect(page.getByTestId(`task-item-${NB}`)).toContainText("In progress");
  await page.getByLabel("Task view", { exact: true }).selectOption("board");
  await expect(
    page.getByTestId("tasks-column-in_progress").getByTestId(`task-item-${NB}`)
  ).toBeVisible();
  await page.getByTestId("tasks-scope-saved").click();
  await page.getByLabel("Filter tasks by review").selectOption("stale");
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(1);
  await expect(page.getByTestId(`task-item-${NC}`)).toBeVisible();
});

test("plan, readiness, status and search filters combine and can be cleared", async ({ page }) => {
  await open(page);
  await page.getByLabel("Filter tasks by plan").selectOption(ROOT_A);
  await page.getByLabel("Filter tasks by readiness").selectOption("blocked");
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(1);
  await expect(page.getByTestId(`task-item-${L1}`)).toContainText("Blocked");
  await page.getByLabel("Filter tasks by status").selectOption("done");
  await expect(page.getByTestId("tasks-empty")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters", exact: true }).click();
  await page.getByLabel("Search tasks").fill("Only leaf B");
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(1);
});

test("task selection opens its details and trace; returning preserves the query and layout", async ({
  page
}) => {
  await open(page);
  await page.getByLabel("Search tasks").fill("Leaf A");
  await page.getByLabel("Task view", { exact: true }).selectOption("board");
  await page.getByTestId(`task-item-${NA}`).click();
  await expect(page.getByTestId("node-detail")).toHaveAttribute("data-node-id", NA);
  await expect(page.getByTestId("trace-prompt")).toContainText("Task context");
  await expect(page.getByTestId("trace-workspace")).toBeVisible();
  await expect(page.getByTestId("tasks-board")).toHaveCount(0);
  await page.getByTestId("tasks-back").click();
  await expect(page.getByLabel("Search tasks")).toHaveValue("Leaf A");
  await expect(page.getByLabel("Task view", { exact: true })).toHaveValue("board");
  await expect(page.getByTestId("node-detail")).toHaveCount(0);
});

test("history is lazy, sorted by timestamp, paged per plan, and opens the historical attempt", async ({
  page
}) => {
  const m = await open(
    page,
    routes({
      [`/plans/${ROOT_A}/events?limit=100&afterSeq=2`]: {
        body: eventsBody(
          [
            {
              ...event(3, NA, "decision_recorded"),
              attemptId: "old-attempt",
              recordedAt: "2026-10-07T12:00:00Z",
              decision: "rejected"
            }
          ],
          null,
          2
        )
      },
      [`/plans/${ROOT_A}/events`]: {
        body: eventsBody([event(2, NA, "attempt_finished")], "2", 2)
      },
      [`/nodes/${NA}/attempts/old-attempt/trace`]: { body: trace("old-attempt") }
    })
  );
  expect(
    m.requests.some((r) => r.url.startsWith("/api/plan-contract/") && r.url.includes("/events"))
  ).toBe(false);
  await page.getByTestId("tasks-scope-history").click();
  await page.getByRole("button", { name: "Load more history · Plan A", exact: true }).click();
  await expect(page.locator('[data-testid^="task-event-"]').first()).toHaveAttribute(
    "data-testid",
    `task-event-${ROOT_A}-3`
  );
  await expect(page.getByTestId("tasks-history")).toContainText("earlier events are unavailable");
  await page.getByLabel("Filter history by event").selectOption("decision_recorded");
  await expect(page.locator('[data-testid^="task-event-"]')).toHaveCount(1);
  await page.getByTestId(`task-event-${ROOT_A}-3`).click();
  await expect(page.getByTestId("attempt-trace")).toHaveAttribute("data-attempt-id", "old-attempt");
  await expect(page.getByTestId("managed-tab-history")).toBeVisible();
});

test("loading another plan page extends task discovery without changing the filters", async ({
  page
}) => {
  await open(
    page,
    routes({
      "/plans?limit=20&afterRootId=": { body: listBody([listItem(ROOT_B, "Plan B")]) },
      "/plans?limit=20": { body: listBody([listItem(ROOT_A, "Plan A")], ROOT_A) }
    })
  );
  await page.getByLabel("Search tasks").fill("Only leaf B");
  await expect(page.getByTestId("tasks-empty")).toBeVisible();
  await page.getByTestId("tasks-more").click();
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(1);
  await expect(page.getByLabel("Search tasks")).toHaveValue("Only leaf B");
});

test("invalid and unsupported plans stay explicit and do not contribute fabricated tasks", async ({
  page
}) => {
  const m = await open(
    page,
    routes({
      "/plans?": {
        body: listBody([
          listItem(ROOT_BAD, "Broken"),
          listItem(ROOT_FUTURE, "Future", { supported: false, contractVersion: "plan-contract/v9" })
        ])
      },
      [`/plans/${ROOT_BAD}`]: { body: planBad() }
    })
  );
  await expect(page.getByRole("alert")).toHaveCount(2);
  await expect(page.getByTestId("tasks-coverage")).toContainText("0 matching tasks");
  expect(m.requests.some((r) => r.url.includes(`/plans/${ROOT_FUTURE}`))).toBe(false);
});

test("refresh prevents an old catalog response from restoring stale tasks", async ({ page }) => {
  const old = held({ status: 500, body: '{"code":"stale_error","message":"old request failed"}' });
  let calls = 0;
  await open(
    page,
    routes({
      [`/plans/${ROOT_A}`]: () =>
        ++calls === 2 ? old.reply() : Promise.resolve({ body: activePlan() })
    })
  );
  await page.getByTestId("tasks-refresh").click();
  // Leave the view while the second catalog load is held, then start a new view instance.
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await page.getByRole("button", { name: "Tasks", exact: true }).click();
  old.release();
  await expect(page.locator('[data-testid^="task-item-"]')).toHaveCount(7);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("history request failures are visible and retryable", async ({ page }) => {
  let fail = true;
  await open(
    page,
    routes({
      [`/plans/${ROOT_A}/events`]: async () =>
        fail
          ? { status: 500, body: '{"code":"history_error","message":"history unavailable"}' }
          : { body: eventsBody([event(1, NA)], null) }
    })
  );
  await page.getByTestId("tasks-scope-history").click();
  await expect(page.getByRole("alert")).toContainText("history unavailable");
  fail = false;
  await page.getByRole("button", { name: "Retry history", exact: true }).click();
  await expect(page.getByTestId(`task-event-${ROOT_A}-1`)).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
