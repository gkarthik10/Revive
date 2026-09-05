/* ============================================================
   Shared formatting helpers.
   Split out from App.jsx so other components (e.g. KanbanBoard)
   can use the same currency/date formatting without creating a
   circular import back into App.jsx.
============================================================ */

export function formatCurrency(value) {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "₹0";
  }

  return `₹${number.toLocaleString("en-IN", {
    maximumFractionDigits: 0,
  })}`;
}

export function formatDate(value) {
  if (!value) return "—";

  try {
    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
      return String(value);
    }

    return date.toLocaleString("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return String(value);
  }
}
