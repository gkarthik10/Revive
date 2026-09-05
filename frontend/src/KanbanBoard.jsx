import { useEffect, useMemo, useState } from "react";
import { formatCurrency, formatDate } from "./formatters.js";

/* ============================================================
   Ops Kanban Board
   ------------------------------------------------------------
   Pulls the operator-facing "still needs a human" items that
   already exist across the app (pending live payments, active
   promises, unconfirmed A2A agreements, PSR alerts, and
   high-value pursue cases not yet recovered) into a single
   drag-and-drop board with three lanes: TO DO / IN PROGRESS /
   DONE. Operators can also add ad-hoc manual tasks.

   Only the *lane placement* is persisted (in localStorage) —
   the card content itself is always re-derived live from
   current props, so amounts/statuses never go stale. Once the
   underlying item resolves (payment recovered, promise paid,
   etc.) its card simply disappears from the board on the next
   render.
============================================================ */

const STORAGE_KEY = "revive_kanban_v1";

const COLUMNS = [
  { key: "todo", label: "TO DO" },
  { key: "inprogress", label: "IN PROGRESS" },
  { key: "done", label: "DONE" },
];

const FILTERS = [
  { key: "ALL", label: "All" },
  { key: "case", label: "Cases" },
  { key: "payment", label: "Payments" },
  { key: "promise", label: "Promises" },
  { key: "a2a", label: "A2A" },
  { key: "psr", label: "PSR" },
  { key: "manual", label: "Manual" },
];

function loadStore() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { columns: {}, manual: [] };

    const parsed = JSON.parse(raw);

    return {
      columns:
        parsed?.columns && typeof parsed.columns === "object"
          ? parsed.columns
          : {},
      manual: Array.isArray(parsed?.manual) ? parsed.manual : [],
    };
  } catch {
    return { columns: {}, manual: [] };
  }
}

function saveStore(store) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // Best-effort only — a full/unavailable localStorage should
    // never break the board itself.
  }
}

export default function KanbanBoard({
  cases = [],
  livePayments = [],
  promises = [],
  liveA2aSettlements = [],
  psrAlerts = [],
}) {
  const [store, setStore] = useState(loadStore);
  const [filter, setFilter] = useState("ALL");
  const [addingTask, setAddingTask] = useState(false);
  const [taskTitle, setTaskTitle] = useState("");
  const [taskNote, setTaskNote] = useState("");
  const [dragOverColumn, setDragOverColumn] = useState(null);

  useEffect(() => {
    saveStore(store);
  }, [store]);

  /* ----------------------------------------------------------
     Auto-generated cards — always derived fresh from live data.
  ---------------------------------------------------------- */

  const autoCards = useMemo(() => {
    const cards = [];

    (Array.isArray(livePayments) ? livePayments : [])
      .filter(
        (item) =>
          String(item.recovery_status || item.outcome || "").toUpperCase() ===
          "PENDING_RECOVERY",
      )
      .forEach((item) => {
        cards.push({
          id: `payment-${item.case_id}`,
          type: "payment",
          title: `Retry live payment · ${item.case_id}`,
          sub: item.customer_name || "Customer unavailable",
          meta: item.root_cause_label || item.payment_method || "",
          amount: item.amount,
        });
      });

    (Array.isArray(promises) ? promises : [])
      .filter((item) => String(item.status || "").toUpperCase() === "PROMISED")
      .forEach((item) => {
        cards.push({
          id: `promise-${item.promise_id || item.case_id}`,
          type: "promise",
          title: `Follow up promise · ${item.case_id}`,
          sub: item.customer_name || item.customer_id || "Customer unavailable",
          meta: item.promise_date ? `Due ${formatDate(item.promise_date)}` : "",
          amount: item.promised_amount,
        });
      });

    (Array.isArray(liveA2aSettlements) ? liveA2aSettlements : [])
      .filter(
        (item) =>
          !item.recovery_confirmed &&
          String(item.payment_status || "").toUpperCase() === "PENDING",
      )
      .forEach((item) => {
        cards.push({
          id: `a2a-${item.case_id || item.agreement_id}`,
          type: "a2a",
          title: `Confirm A2A settlement · ${item.case_id}`,
          sub: item.invoice_id || "Invoice unavailable",
          meta: "Awaiting verified payment capture",
          amount: item.agreed_amount,
        });
      });

    (Array.isArray(psrAlerts) ? psrAlerts : []).forEach((alert, index) => {
      cards.push({
        id: `psr-${alert.bank || "bank"}-${alert.card_network || "network"}-${index}`,
        type: "psr",
        title: `Investigate PSR alert · ${alert.bank || "Unknown"} / ${
          alert.card_network || "Unknown"
        }`,
        sub: `${alert.concentrated_cases ?? 0} of ${alert.group_size ?? 0} cases concentrated`,
        meta: alert.recommendation || "",
        amount: null,
      });
    });

    (Array.isArray(cases) ? cases : [])
      .filter(
        (item) =>
          String(item.roi_decision || "").toUpperCase() === "PURSUE" &&
          String(item.outcome || "").toUpperCase() !== "RECOVERED",
      )
      .sort((a, b) => Number(b.amount || 0) - Number(a.amount || 0))
      .slice(0, 15)
      .forEach((item) => {
        cards.push({
          id: `case-${item.case_id}`,
          type: "case",
          title: `Pursue case · ${item.case_id}`,
          sub: item.root_cause || "Root cause unavailable",
          meta: item.channel ? `Channel: ${item.channel}` : "",
          amount: item.amount,
        });
      });

    return cards;
  }, [cases, livePayments, promises, liveA2aSettlements, psrAlerts]);

  const manualCards = Array.isArray(store.manual) ? store.manual : [];

  const allCards = useMemo(() => {
    const auto = autoCards.map((card) => ({
      ...card,
      column: store.columns[card.id] || "todo",
      manual: false,
    }));

    const manual = manualCards.map((card) => ({
      ...card,
      type: "manual",
      manual: true,
      column: card.column || "todo",
    }));

    return [...auto, ...manual];
  }, [autoCards, manualCards, store.columns]);

  const visibleCards =
    filter === "ALL"
      ? allCards
      : allCards.filter((card) => card.type === filter);

  const cardsByColumn = COLUMNS.reduce((acc, col) => {
    acc[col.key] = visibleCards.filter((card) => card.column === col.key);
    return acc;
  }, {});

  /* ---------------------------------------------------------- */

  function moveCard(card, targetColumn) {
    if (card.column === targetColumn) return;

    if (card.manual) {
      setStore((prev) => ({
        ...prev,
        manual: prev.manual.map((item) =>
          item.id === card.id ? { ...item, column: targetColumn } : item,
        ),
      }));
    } else {
      setStore((prev) => ({
        ...prev,
        columns: { ...prev.columns, [card.id]: targetColumn },
      }));
    }
  }

  function deleteManualCard(id) {
    setStore((prev) => ({
      ...prev,
      manual: prev.manual.filter((item) => item.id !== id),
    }));
  }

  function submitTask(event) {
    event.preventDefault();

    const title = taskTitle.trim();
    if (!title) return;

    const newCard = {
      id: `manual-${Date.now()}-${Math.round(Math.random() * 10000)}`,
      title,
      sub: taskNote.trim(),
      meta: "",
      amount: null,
      column: "todo",
      createdAt: new Date().toISOString(),
    };

    setStore((prev) => ({
      ...prev,
      manual: [newCard, ...prev.manual],
    }));

    setTaskTitle("");
    setTaskNote("");
    setAddingTask(false);
  }

  function handleDragStart(event, card) {
    event.dataTransfer.setData("text/plain", card.id);
    event.dataTransfer.effectAllowed = "move";
  }

  function handleDrop(event, columnKey) {
    event.preventDefault();
    setDragOverColumn(null);

    const id = event.dataTransfer.getData("text/plain");
    const card = allCards.find((item) => item.id === id);

    if (card) moveCard(card, columnKey);
  }

  const columnOrder = COLUMNS.map((c) => c.key);

  return (
    <div className="kanban-board">
      <div className="kanban-toolbar">
        <div
          className="pulse-filters kanban-filters"
          role="tablist"
          aria-label="Kanban card filters"
        >
          {FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={filter === item.key ? "active" : ""}
              onClick={() => setFilter(item.key)}
              role="tab"
              aria-selected={filter === item.key}
            >
              {item.label}
            </button>
          ))}
        </div>

        <button
          type="button"
          className="run-button kanban-add-button"
          onClick={() => setAddingTask((value) => !value)}
        >
          {addingTask ? "Cancel" : "+ Add task"}
        </button>
      </div>

      {addingTask && (
        <form className="kanban-add-form" onSubmit={submitTask}>
          <input
            type="text"
            placeholder="Task title (e.g. Call customer about dispute)"
            value={taskTitle}
            onChange={(event) => setTaskTitle(event.target.value)}
            autoFocus
            required
          />

          <input
            type="text"
            placeholder="Optional note"
            value={taskNote}
            onChange={(event) => setTaskNote(event.target.value)}
          />

          <button type="submit" className="run-button">
            Add to TO DO
          </button>
        </form>
      )}

      <div className="kanban-columns">
        {COLUMNS.map((col) => (
          <div
            key={col.key}
            className={`kanban-column${dragOverColumn === col.key ? " drag-over" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              setDragOverColumn(col.key);
            }}
            onDragLeave={() =>
              setDragOverColumn((v) => (v === col.key ? null : v))
            }
            onDrop={(event) => handleDrop(event, col.key)}
          >
            <div className="kanban-column-header">
              <span>{col.label}</span>
              <span className="kanban-column-count">
                {cardsByColumn[col.key].length}
              </span>
            </div>

            <div className="kanban-column-body">
              {cardsByColumn[col.key].length === 0 ? (
                <div className="kanban-empty">Nothing here.</div>
              ) : (
                cardsByColumn[col.key].map((card) => {
                  const currentIndex = columnOrder.indexOf(card.column);

                  return (
                    <div
                      key={card.id}
                      className={`kanban-card kanban-card-${card.type}`}
                      draggable
                      onDragStart={(event) => handleDragStart(event, card)}
                    >
                      <div className="kanban-card-top">
                        <span
                          className={`kanban-card-tag kanban-tag-${card.type}`}
                        >
                          {card.type.toUpperCase()}
                        </span>

                        {card.manual && (
                          <button
                            type="button"
                            className="kanban-card-delete"
                            onClick={() => deleteManualCard(card.id)}
                            aria-label="Delete task"
                          >
                            ×
                          </button>
                        )}
                      </div>

                      <strong className="kanban-card-title">
                        {card.title}
                      </strong>

                      {card.sub && (
                        <div className="kanban-card-sub">{card.sub}</div>
                      )}

                      {card.meta && (
                        <div className="kanban-card-meta">{card.meta}</div>
                      )}

                      {card.amount != null && (
                        <div className="kanban-card-amount">
                          {formatCurrency(card.amount)}
                        </div>
                      )}

                      <div className="kanban-card-actions">
                        <button
                          type="button"
                          disabled={currentIndex <= 0}
                          onClick={() =>
                            moveCard(card, columnOrder[currentIndex - 1])
                          }
                        >
                          ← Back
                        </button>

                        <button
                          type="button"
                          disabled={currentIndex >= columnOrder.length - 1}
                          onClick={() =>
                            moveCard(card, columnOrder[currentIndex + 1])
                          }
                        >
                          Next →
                        </button>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
