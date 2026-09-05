import { useState } from "react";
import { useAuth } from "./AuthContext.jsx";

const ADMIN_EMAIL = "admin@revive.ai";

export default function LoginPage() {
  const { login } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const [showSupportModal, setShowSupportModal] = useState(false);
  const [supportSent, setSupportSent] = useState(false);
  const [supportForm, setSupportForm] = useState({
    name: "",
    workEmail: "",
    workId: "",
    issue: "",
  });

  function updateSupportField(field, value) {
    setSupportForm((current) => ({ ...current, [field]: value }));
  }

  function openSupportModal() {
    setSupportSent(false);
    setSupportForm({
      name: "",
      workEmail: email || "",
      workId: "",
      issue: "",
    });
    setShowSupportModal(true);
  }

  function closeSupportModal() {
    setShowSupportModal(false);
  }

  function handleSupportSubmit(event) {
    event.preventDefault();

    const subject = `Login trouble — ${supportForm.name || "Revive user"} (Work ID: ${
      supportForm.workId || "not provided"
    })`;

    const body = [
      `Name: ${supportForm.name}`,
      `Work email: ${supportForm.workEmail}`,
      `Work ID: ${supportForm.workId}`,
      "",
      "Issue:",
      supportForm.issue || "(no additional details provided)",
    ].join("\n");

    const mailtoUrl = `mailto:${ADMIN_EMAIL}?subject=${encodeURIComponent(
      subject,
    )}&body=${encodeURIComponent(body)}`;

    window.location.href = mailtoUrl;
    setSupportSent(true);
  }

  async function handleSubmit(event) {
    event.preventDefault();

    setError(null);
    setLoading(true);

    try {
      await login(email.trim(), password);
    } catch (err) {
      setError(
        err?.response?.data?.detail ||
          err?.message ||
          "Unable to sign in. Please check your credentials.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-screen">
      <div className="auth-glow auth-glow-one" aria-hidden="true" />

      <div className="auth-glow auth-glow-two" aria-hidden="true" />

      <div className="auth-layout">
        {/* ==================================================
            LEFT — REVIVE BRAND
        ================================================== */}

        <section className="auth-showcase">
          <div className="auth-showcase-badge">
            <span className="auth-showcase-dot" />
            <span>AI REVENUE RECOVERY</span>
          </div>

          <div className="auth-showcase-mark" aria-hidden="true">
            R
          </div>

          <h2>
            Recover revenue.
            <br />
            <span>Prove every decision.</span>
          </h2>

          <p>
            Revive gives revenue teams one command center for intelligent
            recovery, customer promises, payment actions, and auditable
            outcomes.
          </p>

          {/* ==================================================
              THINK → DECIDE → ACT → PROVE
          ================================================== */}

          <div className="auth-showcase-points">
            <div className="auth-capability-card">
              <strong>THINK</strong>

              <span>Understand why payments fail.</span>
            </div>

            <div className="auth-capability-card">
              <strong>DECIDE</strong>

              <span>Choose the safest recovery action.</span>
            </div>

            <div className="auth-capability-card">
              <strong>ACT</strong>

              <span>Execute only when policy allows.</span>
            </div>

            <div className="auth-capability-card">
              <strong>PROVE</strong>

              <span>Track every action and outcome.</span>
            </div>
          </div>
        </section>

        {/* ==================================================
            RIGHT — LOGIN
        ================================================== */}

        <section className="auth-card">
          <div className="auth-brand">
            <span className="auth-brand-mark" aria-hidden="true">
              ◆
            </span>

            <span>REVIVE</span>
          </div>

          <h1>Sign in</h1>

          <p className="auth-subtitle">
            Sign in with your team account to open the dashboard.
          </p>

          <form onSubmit={handleSubmit} className="auth-form">
            <label className="auth-field">
              <span>Work email</span>

              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="name@company.com"
                autoComplete="username"
                autoFocus
                required
              />
            </label>

            <label className="auth-field">
              <span>Password</span>

              <div className="auth-password-field">
                <input
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="Enter your password"
                  autoComplete="current-password"
                  required
                />

                <button
                  type="button"
                  className="auth-password-toggle"
                  onClick={() => setShowPassword((value) => !value)}
                >
                  {showPassword ? "Hide" : "Show"}
                </button>
              </div>
            </label>

            {error && (
              <div className="policy-warning auth-error" role="alert">
                {error}
              </div>
            )}

            <button
              type="submit"
              className="run-button auth-submit"
              disabled={loading || !email.trim() || !password}
            >
              {loading ? "Authenticating..." : "Sign in"}

              {!loading && <span>→</span>}
            </button>
          </form>

          <button
            type="button"
            className="auth-trouble-link"
            onClick={openSupportModal}
          >
            Trouble logging in? Contact admin
          </button>

          <div className="auth-team-note">
            <span className="auth-team-note-icon">◆</span>

            <div>
              <strong>Team access only</strong>

              <span>
                New accounts are created by your Revive administrator.
              </span>
            </div>
          </div>

          <div className="auth-card-footer">
            <span>REVIVE</span>

            <span>SECURE OPS WORKSPACE</span>
          </div>
        </section>
      </div>

      {/* ==================================================
          TROUBLE LOGGING IN — CONTACT ADMIN MODAL
      ================================================== */}

      {showSupportModal && (
        <div className="modal-backdrop" onClick={closeSupportModal}>
          <div
            className="modal auth-support-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <div className="section-kicker">ACCOUNT ACCESS</div>

                <h2>Trouble logging in?</h2>

                <p className="modal-subtitle">
                  Send your Revive administrator the details below and
                  they&apos;ll help restore access.
                </p>
              </div>

              <button className="close-button" onClick={closeSupportModal}>
                ×
              </button>
            </div>

            {supportSent ? (
              <div className="auth-support-sent">
                <span className="auth-support-sent-icon" aria-hidden="true">
                  ✓
                </span>

                <div>
                  <strong>Your email client should now be open.</strong>

                  <span>
                    A message addressed to {ADMIN_EMAIL} was prefilled with your
                    details — just hit send. If nothing opened, email{" "}
                    {ADMIN_EMAIL} directly.
                  </span>
                </div>
              </div>
            ) : (
              <form
                onSubmit={handleSupportSubmit}
                className="auth-form auth-support-form"
              >
                <label className="auth-field">
                  <span>Full name</span>

                  <input
                    type="text"
                    value={supportForm.name}
                    onChange={(event) =>
                      updateSupportField("name", event.target.value)
                    }
                    placeholder="Your full name"
                    required
                  />
                </label>

                <label className="auth-field">
                  <span>Work email</span>

                  <input
                    type="email"
                    value={supportForm.workEmail}
                    onChange={(event) =>
                      updateSupportField("workEmail", event.target.value)
                    }
                    placeholder="name@company.com"
                    required
                  />
                </label>

                <label className="auth-field">
                  <span>Work ID / Employee ID</span>

                  <input
                    type="text"
                    value={supportForm.workId}
                    onChange={(event) =>
                      updateSupportField("workId", event.target.value)
                    }
                    placeholder="e.g. REV-1042"
                    required
                  />
                </label>

                <label className="auth-field">
                  <span>What's happening?</span>

                  <textarea
                    className="auth-support-textarea"
                    value={supportForm.issue}
                    onChange={(event) =>
                      updateSupportField("issue", event.target.value)
                    }
                    placeholder="Describe the error, when it started, and anything else that would help your admin."
                    rows={4}
                    required
                  />
                </label>

                <p className="auth-support-note">
                  Submitting opens your email client with a message addressed to
                  your Revive administrator ({ADMIN_EMAIL}), prefilled with
                  these details.
                </p>

                <div className="simulator-actions">
                  <button type="submit" className="run-button auth-submit">
                    Send to admin <span>→</span>
                  </button>

                  <button
                    type="button"
                    className="close-button reset-button"
                    onClick={closeSupportModal}
                  >
                    Cancel
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
