import { useEffect, useState } from "react";
import Dropdown from "./Dropdown.jsx";

const ADMIN_EMAIL = "admin@revive.ai";

const PROBLEM_CATEGORIES = [
  "Login / account access",
  "Dashboard data looks wrong",
  "Payment or recovery action issue",
  "Bug / something is broken",
  "Feature request",
  "Other",
];

const PROBLEM_CATEGORY_OPTIONS = PROBLEM_CATEGORIES.map((category) => ({
  value: category,
  label: category,
}));

/**
 * Contact-admin support modal used from the dashboard.
 *
 * Mirrors the "Trouble logging in?" modal on LoginPage.jsx: it
 * collects the reporter's details plus a specific problem category
 * and description, then opens the user's email client with those
 * details prefilled — instead of the dashboard footer's old bare
 * `mailto:` link, which gave the admin zero context.
 */
export default function ContactAdminModal({ open, onClose, user }) {
  const [sent, setSent] = useState(false);

  const [form, setForm] = useState({
    name: "",
    workEmail: "",
    category: PROBLEM_CATEGORIES[0],
    issue: "",
  });

  /*
   * Reset the form (prefilled from the signed-in user) every
   * time the modal is opened.
   */

  useEffect(() => {
    if (!open) return;

    setSent(false);

    setForm({
      name: user?.name || "",
      workEmail: user?.email || "",
      category: PROBLEM_CATEGORIES[0],
      issue: "",
    });
  }, [open, user]);

  function updateField(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function handleClose() {
    setSent(false);
    onClose();
  }

  function handleSubmit(event) {
    event.preventDefault();

    const subject = `Revive support — ${form.category} (${
      form.name || "Revive user"
    })`;

    const body = [
      `Name: ${form.name}`,
      `Work email: ${form.workEmail}`,
      `Role: ${user?.role || "unknown"}`,
      `Problem type: ${form.category}`,
      "",
      "Details:",
      form.issue || "(no additional details provided)",
    ].join("\n");

    const mailtoUrl = `mailto:${ADMIN_EMAIL}?subject=${encodeURIComponent(
      subject,
    )}&body=${encodeURIComponent(body)}`;

    window.location.href = mailtoUrl;
    setSent(true);
  }

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={handleClose}>
      <div
        className="modal auth-support-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <div>
            <div className="section-kicker">SUPPORT</div>

            <h2>Contact admin</h2>

            <p className="modal-subtitle">
              Tell your Revive administrator what's going wrong and they&apos;ll
              follow up.
            </p>
          </div>

          <button className="close-button" onClick={handleClose}>
            ×
          </button>
        </div>

        {sent ? (
          <div className="auth-support-sent">
            <span className="auth-support-sent-icon" aria-hidden="true">
              ✓
            </span>

            <div>
              <strong>Your email client should now be open.</strong>

              <span>
                A message addressed to {ADMIN_EMAIL} was prefilled with your
                details — just hit send. If nothing opened, email {ADMIN_EMAIL}{" "}
                directly.
              </span>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="auth-form auth-support-form">
            <label className="auth-field">
              <span>Full name</span>

              <input
                type="text"
                value={form.name}
                onChange={(event) => updateField("name", event.target.value)}
                placeholder="Your full name"
                required
              />
            </label>

            <label className="auth-field">
              <span>Work email</span>

              <input
                type="email"
                value={form.workEmail}
                onChange={(event) =>
                  updateField("workEmail", event.target.value)
                }
                placeholder="name@company.com"
                required
              />
            </label>

            <label className="auth-field">
              <span>What's the problem about?</span>

              <Dropdown
                value={form.category}
                onChange={(value) => updateField("category", value)}
                options={PROBLEM_CATEGORY_OPTIONS}
                className="auth-field-dropdown"
              />
            </label>

            <label className="auth-field">
              <span>Describe the issue</span>

              <textarea
                className="auth-support-textarea"
                value={form.issue}
                onChange={(event) => updateField("issue", event.target.value)}
                placeholder="What happened, when it started, and anything else that would help your admin."
                rows={4}
                required
              />
            </label>

            <p className="auth-support-note">
              Submitting opens your email client with a message addressed to
              your Revive administrator ({ADMIN_EMAIL}), prefilled with these
              details.
            </p>

            <div className="simulator-actions">
              <button type="submit" className="run-button auth-submit">
                Send to admin <span>→</span>
              </button>

              <button
                type="button"
                className="close-button reset-button"
                onClick={handleClose}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
