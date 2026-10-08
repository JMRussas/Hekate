// Task navigation over existing plan-contract reads. Catalog pages load 20 plans,
// at most four plan reads concurrently. Counts and filters cover loaded plans only.
// History is fetched on demand and keeps each plan's original pagination cursor.
// No writes, execution controls, or local copies of authoritative task state.
import { useCallback, useEffect, useState } from "react";
import { getPlan, getPlanEvents, listPlans, type CheckedEventPage } from "../../planContract/api";
import { isStale, useRequestSlot } from "../../planContract/requestSlot";
import type { PlanEventView, PlanListItem, PlanNodeView, PlanView } from "../../planContract/types";

type Entry = { item: PlanListItem; plan: PlanView | null; error: string | null };
type Task = { entry: Entry; node: PlanNodeView; ready: boolean; blocked: boolean };
type Scope = "active" | "saved" | "history";
type Layout = "list" | "board";
type History = {
  items: PlanEventView[];
  next: string | null;
  starts: number | null;
  error: string | null;
};

async function batches<T, R>(items: T[], read: (item: T) => Promise<R>): Promise<R[]> {
  const result: R[] = [];
  for (let i = 0; i < items.length; i += 4)
    result.push(...(await Promise.all(items.slice(i, i + 4).map(read))));
  return result;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
const name = (task: Task) => task.node.name ?? task.node.id;
const workLabel = (value: string) =>
  ({ todo: "To do", in_progress: "In progress", done: "Done", cancelled: "Cancelled" })[value] ??
  value;
const planName = (entry: Entry) => entry.item.name ?? entry.item.rootId;
const stamp = (value: string) => new Date(value).toLocaleString();
const pageState = ({ page, nextCursorText }: CheckedEventPage): History => ({
  items: page.events,
  next: nextCursorText,
  starts: page.historyStartsAtSeq,
  error: null
});

export default function TaskNavigator({
  onSelect,
  onReset,
  focused
}: {
  focused: boolean;
  onSelect: (
    item: PlanListItem,
    nodeId: string,
    history: boolean,
    attemptId?: string | null
  ) => void;
  onReset: () => void;
}) {
  const catalogSlot = useRequestSlot();
  const historySlot = useRequestSlot();
  const [catalog, setCatalog] = useState<{
    entries: Entry[];
    next: string | null;
    loading: boolean;
    error: string | null;
  }>({ entries: [], next: null, loading: true, error: null });
  const [scope, setScope] = useState<Scope>("saved");
  const [layout, setLayout] = useState<Layout>("list");
  const [query, setQuery] = useState("");
  const [planFilter, setPlanFilter] = useState("all");
  const [workFilter, setWorkFilter] = useState("all");
  const [acceptanceFilter, setAcceptanceFilter] = useState("all");
  const [readinessFilter, setReadinessFilter] = useState("all");
  const [eventFilter, setEventFilter] = useState("all");
  const [history, setHistory] = useState<Record<string, History>>({});
  const [historyBusy, setHistoryBusy] = useState(false);

  const loadCatalog = useCallback(
    async (cursor: string | null) => {
      const g = catalogSlot.begin();
      historySlot.cancel();
      setHistory({});
      setHistoryBusy(false);
      setCatalog((s) => ({
        entries: cursor === null ? [] : s.entries,
        next: null,
        loading: true,
        error: null
      }));
      try {
        const page = await listPlans(cursor, g, 20);
        const entries = await batches(page.plans, async (item): Promise<Entry> => {
          if (!g.isCurrent()) return { item, plan: null, error: "Request cancelled." };
          if (!item.supported)
            return { item, plan: null, error: `Unsupported contract: ${item.contractVersion}` };
          try {
            const plan = await getPlan(item.rootId, g);
            return {
              item,
              plan: plan.readiness.errors.length ? null : plan,
              error: plan.readiness.errors.length ? "Stored plan is invalid." : null
            };
          } catch (e) {
            return { item, plan: null, error: message(e) };
          }
        });
        if (!g.isCurrent()) return;
        setCatalog((s) =>
          g.isCurrent()
            ? {
                entries: cursor === null ? entries : [...s.entries, ...entries],
                next: page.nextAfterRootId,
                loading: false,
                error: null
              }
            : s
        );
      } catch (e) {
        if (isStale(e, g)) return;
        setCatalog((s) =>
          g.isCurrent() ? { ...s, next: cursor, loading: false, error: message(e) } : s
        );
      }
    },
    [catalogSlot, historySlot]
  );

  useEffect(() => {
    const timer = setTimeout(() => void loadCatalog(null), 0);
    return () => clearTimeout(timer);
  }, [loadCatalog]);

  const loadHistory = useCallback(async () => {
    const g = historySlot.begin();
    setHistoryBusy(true);
    const rows = await batches(
      catalog.entries.filter((e) => e.plan !== null),
      async (entry) => {
        try {
          if (!g.isCurrent())
            return [
              entry.item.rootId,
              { items: [], next: null, starts: null, error: "Request cancelled." }
            ] as const;
          return [
            entry.item.rootId,
            pageState(await getPlanEvents(entry.item.rootId, null, g))
          ] as const;
        } catch (e) {
          return [
            entry.item.rootId,
            { items: [], next: null, starts: null, error: message(e) }
          ] as const;
        }
      }
    );
    if (!g.isCurrent()) return;
    setHistory(Object.fromEntries(rows));
    setHistoryBusy(false);
  }, [catalog.entries, historySlot]);

  useEffect(() => {
    if (scope !== "history" || catalog.loading) return;
    const timer = setTimeout(() => void loadHistory(), 0);
    return () => clearTimeout(timer);
  }, [scope, catalog.loading, loadHistory]);

  const moreHistory = async (entry: Entry) => {
    const current = history[entry.item.rootId];
    if (!current?.next || historyBusy) return;
    const g = historySlot.begin();
    setHistoryBusy(true);
    try {
      const next = pageState(await getPlanEvents(entry.item.rootId, current.next, g));
      if (!g.isCurrent()) return;
      setHistory((s) =>
        g.isCurrent()
          ? { ...s, [entry.item.rootId]: { ...next, items: [...current.items, ...next.items] } }
          : s
      );
    } catch (e) {
      if (isStale(e, g)) return;
      setHistory((s) =>
        g.isCurrent() ? { ...s, [entry.item.rootId]: { ...current, error: message(e) } } : s
      );
    } finally {
      if (g.isCurrent()) setHistoryBusy(false);
    }
  };

  const tasks: Task[] = catalog.entries.flatMap(
    (entry) =>
      entry.plan?.readiness.leaves.flatMap((leaf) => {
        const node = entry.plan!.nodes.find((n) => n.id === leaf.nodeId);
        return node ? [{ entry, node, ready: leaf.ready, blocked: leaf.blockers.length > 0 }] : [];
      }) ?? []
  );
  const visible = tasks.filter(
    (task) =>
      (scope !== "active" || task.node.work === "in_progress") &&
      (planFilter === "all" || task.entry.item.rootId === planFilter) &&
      (workFilter === "all" || task.node.work === workFilter) &&
      (acceptanceFilter === "all" || task.node.effectiveAcceptance === acceptanceFilter) &&
      (readinessFilter === "all" || (readinessFilter === "ready" ? task.ready : task.blocked)) &&
      `${name(task)} ${planName(task.entry)} ${task.node.id} ${task.node.attemptId ?? ""}`
        .toLowerCase()
        .includes(query.trim().toLowerCase())
  );
  const byId = new Map(visible.map((t) => [t.node.id, t]));
  const timeline = catalog.entries
    .flatMap((entry) =>
      (history[entry.item.rootId]?.items ?? []).flatMap((event) => {
        const task = byId.get(event.nodeId);
        return task && (eventFilter === "all" || event.kind === eventFilter)
          ? [{ task, event }]
          : [];
      })
    )
    .sort(
      (a, b) => b.event.recordedAt.localeCompare(a.event.recordedAt) || b.event.seq - a.event.seq
    );
  const workStates = [
    ...new Set(["todo", "in_progress", "done", "cancelled", ...tasks.map((t) => t.node.work)])
  ];
  const acceptances = [...new Set(tasks.map((t) => t.node.effectiveAcceptance))].sort();
  const eventKinds = [
    ...new Set(Object.values(history).flatMap((h) => h.items.map((e) => e.kind)))
  ].sort();
  const controlClass =
    "rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-200";

  const taskCard = (task: Task) => (
    <button
      key={task.node.id}
      data-testid={`task-item-${task.node.id}`}
      onClick={() => onSelect(task.entry.item, task.node.id, false, task.node.attemptId)}
      className="w-full rounded border border-slate-700 bg-slate-800/70 p-3 text-left hover:border-blue-400 focus-visible:outline-blue-400"
    >
      <div className="text-sm font-medium break-words">{name(task)}</div>
      <div className="mt-1 text-xs text-slate-400">{planName(task.entry)}</div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs">
        <span>{workLabel(task.node.work)}</span>
        <span>{task.node.effectiveAcceptance}</span>
        {task.ready && <span className="text-emerald-300">Ready</span>}
        {task.blocked && <span className="text-amber-300">Blocked</span>}
      </div>
      {task.node.attemptId && (
        <div className="mt-1 truncate text-xs text-slate-500">
          Attempt {task.node.attemptId} · {task.node.attemptEpoch}
        </div>
      )}
    </button>
  );

  if (focused)
    return (
      <section
        data-testid="task-navigator"
        className="mb-4 flex items-center justify-between border-b border-slate-700 pb-3"
      >
        <button data-testid="tasks-back" className={controlClass} onClick={onReset}>
          ← Back to filtered tasks
        </button>
        <span className="text-xs text-slate-400">
          {scope === "history" ? "History" : scope === "active" ? "Active tasks" : "Saved tasks"} ·{" "}
          {layout} view
        </span>
      </section>
    );

  return (
    <section data-testid="task-navigator" className="mb-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Tasks</h2>
          <p className="text-xs text-slate-400">
            Find work, inspect attempts and revisit decisions.
          </p>
        </div>
        <button
          className={controlClass}
          data-testid="tasks-refresh"
          disabled={catalog.loading}
          onClick={() => {
            onReset();
            void loadCatalog(null);
          }}
        >
          Refresh tasks
        </button>
      </div>
      <nav aria-label="Task collections" className="mb-3 flex gap-2">
        {(["active", "saved", "history"] as const).map((value) => (
          <button
            key={value}
            data-testid={`tasks-scope-${value}`}
            aria-pressed={scope === value}
            onClick={() => setScope(value)}
            className={`rounded px-3 py-2 text-sm ${scope === value ? "bg-blue-600 text-white" : "bg-slate-800 text-slate-300"}`}
          >
            {value === "active" ? "Active" : value === "saved" ? "Saved" : "History"}
            {value !== "history" && (
              <span className="ml-2 text-xs">
                {value === "active"
                  ? tasks.filter((t) => t.node.work === "in_progress").length
                  : tasks.length}
              </span>
            )}
          </button>
        ))}
      </nav>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs">
          Search
          <input
            aria-label="Search tasks"
            className={controlClass}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Task, plan or attempt"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Plan
          <select
            aria-label="Filter tasks by plan"
            className={controlClass}
            value={planFilter}
            onChange={(e) => setPlanFilter(e.target.value)}
          >
            <option value="all">All loaded plans</option>
            {catalog.entries.map((e) => (
              <option key={e.item.rootId} value={e.item.rootId}>
                {planName(e)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Status
          <select
            aria-label="Filter tasks by status"
            className={controlClass}
            value={workFilter}
            onChange={(e) => setWorkFilter(e.target.value)}
          >
            <option value="all">All statuses</option>
            {workStates.map((s) => (
              <option key={s} value={s}>
                {workLabel(s)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Review
          <select
            aria-label="Filter tasks by review"
            className={controlClass}
            value={acceptanceFilter}
            onChange={(e) => setAcceptanceFilter(e.target.value)}
          >
            <option value="all">All decisions</option>
            {acceptances.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Readiness
          <select
            aria-label="Filter tasks by readiness"
            className={controlClass}
            value={readinessFilter}
            onChange={(e) => setReadinessFilter(e.target.value)}
          >
            <option value="all">All readiness</option>
            <option value="ready">Ready</option>
            <option value="blocked">Blocked</option>
          </select>
        </label>
        {scope !== "history" ? (
          <label className="flex flex-col gap-1 text-xs">
            View
            <select
              aria-label="Task view"
              className={controlClass}
              value={layout}
              onChange={(e) => setLayout(e.target.value as Layout)}
            >
              <option value="list">List</option>
              <option value="board">Board</option>
            </select>
          </label>
        ) : (
          <label className="flex flex-col gap-1 text-xs">
            Event
            <select
              aria-label="Filter history by event"
              className={controlClass}
              value={eventFilter}
              onChange={(e) => setEventFilter(e.target.value)}
            >
              <option value="all">All events</option>
              {eventKinds.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
        )}
        <button
          className={controlClass}
          onClick={() => {
            setQuery("");
            setPlanFilter("all");
            setWorkFilter("all");
            setAcceptanceFilter("all");
            setReadinessFilter("all");
            setEventFilter("all");
          }}
        >
          Clear filters
        </button>
      </div>
      <p className="mb-3 text-xs text-slate-400">
        {scope === "active"
          ? "Active means in_progress in Hekate; it does not assert that a worker process is alive."
          : scope === "saved"
            ? "Saved includes every stored task, including completed and cancelled work."
            : "Recorded task events, newest first within the loaded history pages. Task-state filters use current state."}
      </p>
      <p data-testid="tasks-coverage" className="mb-3 text-xs text-slate-500">
        {catalog.entries.length} plans loaded ·{" "}
        {scope === "history"
          ? timeline.length + " matching events"
          : visible.length + " matching tasks"}
        {catalog.next ? " · More plans available" : ""}
      </p>
      {catalog.loading && (
        <p role="status" className="text-sm text-slate-400">
          Loading tasks…
        </p>
      )}
      {catalog.error && (
        <p role="alert" className="text-sm text-red-400">
          Tasks could not be loaded: {catalog.error}
        </p>
      )}
      {catalog.entries
        .filter((e) => e.error)
        .map((e) => (
          <p role="alert" key={e.item.rootId} className="mb-2 text-xs text-amber-300">
            {planName(e)}: {e.error} Tasks from this plan are unavailable.
          </p>
        ))}
      {scope !== "history" && !catalog.loading && !catalog.error && visible.length === 0 && (
        <p data-testid="tasks-empty" className="py-6 text-sm text-slate-400">
          No tasks match this view. Try Saved or clear the filters.
        </p>
      )}
      {scope !== "history" && layout === "list" && (
        <div data-testid="tasks-list" className="grid gap-2">
          {visible.map(taskCard)}
        </div>
      )}
      {scope !== "history" && layout === "board" && (
        <div data-testid="tasks-board" className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {workStates.map((work) => (
            <div
              key={work}
              data-testid={`tasks-column-${work}`}
              className="rounded bg-slate-900/70 p-3"
            >
              <h3 className="mb-3 text-sm font-semibold">
                {workLabel(work)}{" "}
                <span className="text-slate-500">
                  {visible.filter((t) => t.node.work === work).length}
                </span>
              </h3>
              <div className="grid gap-2">
                {visible.filter((t) => t.node.work === work).map(taskCard)}
              </div>
            </div>
          ))}
        </div>
      )}
      {scope === "history" && (
        <div data-testid="tasks-history">
          {historyBusy && (
            <p role="status" className="text-sm text-slate-400">
              Loading history…
            </p>
          )}
          {!historyBusy && !catalog.loading && timeline.length === 0 && (
            <p className="py-6 text-sm text-slate-400">
              No recorded task events match these filters.
            </p>
          )}
          <ol className="grid gap-2">
            {timeline.map(({ task, event }) => (
              <li key={`${task.entry.item.rootId}:${event.seq}`}>
                <button
                  data-testid={`task-event-${task.entry.item.rootId}-${event.seq}`}
                  className="w-full rounded border border-slate-700 p-3 text-left hover:border-blue-400"
                  onClick={() => onSelect(task.entry.item, task.node.id, true, event.attemptId)}
                >
                  <div className="flex flex-wrap justify-between gap-2 text-xs text-slate-400">
                    <span>
                      {event.kind} · {workLabel(event.workFrom)} → {workLabel(event.workTo)}
                      {event.decision ? " · " + event.decision : ""}
                    </span>
                    <time dateTime={event.recordedAt}>{stamp(event.recordedAt)}</time>
                  </div>
                  <div className="mt-1 text-sm">
                    {name(task)}{" "}
                    <span className="text-xs text-slate-500">· {planName(task.entry)}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {event.actor}
                    {event.attemptId ? " · Attempt " + event.attemptId : ""} · Event {event.seq}
                  </div>
                </button>
              </li>
            ))}
          </ol>
          {catalog.entries
            .filter((e) => planFilter === "all" || e.item.rootId === planFilter)
            .map((entry) => {
              const h = history[entry.item.rootId];
              return (
                h && (
                  <div key={entry.item.rootId} className="mt-2 text-xs text-slate-500">
                    {h.error && (
                      <p role="alert">
                        {planName(entry)}: history unavailable: {h.error}
                      </p>
                    )}
                    {h.starts !== null && h.starts > 1 && (
                      <p>
                        {planName(entry)}: retained history starts at event {h.starts}; earlier
                        events are unavailable.
                      </p>
                    )}
                    {h.next && (
                      <button
                        disabled={historyBusy}
                        className={controlClass}
                        onClick={() => void moreHistory(entry)}
                      >
                        Load more history · {planName(entry)}
                      </button>
                    )}
                  </div>
                )
              );
            })}
          {!historyBusy && Object.values(history).some((h) => h.error) && (
            <button className={controlClass} onClick={() => void loadHistory()}>
              Retry history
            </button>
          )}
        </div>
      )}
      {catalog.next && !catalog.loading && (
        <button
          data-testid="tasks-more"
          className={controlClass + " mt-3"}
          onClick={() => void loadCatalog(catalog.next)}
        >
          Load more plans
        </button>
      )}
      {catalog.error && !catalog.loading && (
        <button className={controlClass} onClick={() => void loadCatalog(catalog.next)}>
          Retry tasks
        </button>
      )}
    </section>
  );
}
