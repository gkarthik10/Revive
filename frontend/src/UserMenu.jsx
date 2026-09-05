import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import axios from "axios";
import { useAuth } from "./AuthContext.jsx";
import Dropdown from "./Dropdown.jsx";

const API_BASE = "http://127.0.0.1:8000/api";

const ROLE_META = {
  admin: {
    label: "Administrator",
    short: "ADMIN",
    description: "Full access to Revive and team management.",
  },

  operator: {
    label: "Operator",
    short: "OPERATOR",
    description: "Can run recovery operations and manage revenue workflows.",
  },

  viewer: {
    label: "Viewer",
    short: "VIEWER",
    description: "Can view dashboards and data without changing anything.",
  },
};

function getInitials(name) {
  if (!name) return "?";

  return name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

function roleInfo(role) {
  return (
    ROLE_META[role] || {
      label: role || "Unknown",
      short: String(role || "UNKNOWN").toUpperCase(),
      description: "",
    }
  );
}

export default function UserMenu() {
  const { user, logout, isAdmin } = useAuth();

  const [menuOpen, setMenuOpen] = useState(false);
  const [teamOpen, setTeamOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);

  const [users, setUsers] = useState([]);
  const [loadingUsers, setLoadingUsers] = useState(false);

  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("operator");

  const [creating, setCreating] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [passwordMessage, setPasswordMessage] = useState("");
  const [changingPassword, setChangingPassword] = useState(false);

  const menuRef = useRef(null);

  useEffect(() => {
    function handlePointerDown(event) {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setMenuOpen(false);
      }
    }

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setMenuOpen(false);

        if (teamOpen) {
          setTeamOpen(false);
        }

        if (passwordOpen) {
          setPasswordOpen(false);
        }
      }
    }

    document.addEventListener("mousedown", handlePointerDown);

    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("mousedown", handlePointerDown);

      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [teamOpen, passwordOpen]);

  if (!user) {
    return null;
  }

  async function loadTeam() {
    setLoadingUsers(true);
    setError("");

    try {
      const response = await axios.get(`${API_BASE}/auth/users`);

      setUsers(response.data?.users || []);
    } catch (err) {
      setError(err?.response?.data?.detail || "Unable to load team members.");
    } finally {
      setLoadingUsers(false);
    }
  }

  function openTeamManagement() {
    setMenuOpen(false);
    setTeamOpen(true);
    setMessage("");
    setError("");
    loadTeam();
  }

  function closeTeamManagement() {
    setTeamOpen(false);
    setMessage("");
    setError("");
  }

  function openChangePassword() {
    setMenuOpen(false);
    setPasswordOpen(true);
    setPasswordError("");
    setPasswordMessage("");
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
  }

  function closeChangePassword() {
    setPasswordOpen(false);
    setPasswordError("");
    setPasswordMessage("");
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
  }

  async function handleChangePassword(event) {
    event.preventDefault();

    setPasswordError("");
    setPasswordMessage("");

    if (!currentPassword) {
      setPasswordError("Enter your current password.");
      return;
    }

    if (newPassword.length < 8) {
      setPasswordError("New password must be at least 8 characters.");
      return;
    }

    if (newPassword !== confirmPassword) {
      setPasswordError("New password and confirmation don't match.");
      return;
    }

    setChangingPassword(true);

    try {
      await axios.post(`${API_BASE}/auth/change-password`, {
        current_password: currentPassword,
        new_password: newPassword,
      });

      setPasswordMessage("Your password has been updated.");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setPasswordError(
        err?.response?.data?.detail || "Unable to change your password.",
      );
    } finally {
      setChangingPassword(false);
    }
  }

  async function handleCreateMember(event) {
    event.preventDefault();

    setError("");
    setMessage("");

    if (!name.trim()) {
      setError("Enter the team member's name.");
      return;
    }

    if (!email.trim()) {
      setError("Enter the team member's work email.");
      return;
    }

    if (password.length < 8) {
      setError("The initial password must contain at least 8 characters.");
      return;
    }

    setCreating(true);

    try {
      await axios.post(`${API_BASE}/auth/users`, {
        name: name.trim(),
        email: email.trim(),
        password,
        role,
      });

      setMessage(`${name.trim()} has been added to the Revive workspace.`);

      setName("");
      setEmail("");
      setPassword("");
      setRole("operator");

      await loadTeam();
    } catch (err) {
      setError(
        err?.response?.data?.detail || "Unable to create this team member.",
      );
    } finally {
      setCreating(false);
    }
  }

  async function handleRoleChange(member, nextRole) {
    setError("");
    setMessage("");

    try {
      await axios.patch(`${API_BASE}/auth/users/${member.id}/role`, {
        role: nextRole,
      });

      setMessage(`${member.name}'s access level was updated.`);

      await loadTeam();
    } catch (err) {
      setError(
        err?.response?.data?.detail || "Unable to update the member's role.",
      );

      await loadTeam();
    }
  }

  async function handleRemove(member) {
    const confirmed = window.confirm(
      `Remove ${member.name} from the Revive workspace?`,
    );

    if (!confirmed) {
      return;
    }

    setError("");
    setMessage("");

    try {
      await axios.delete(`${API_BASE}/auth/users/${member.id}`);

      setMessage(`${member.name} has been removed from the workspace.`);

      await loadTeam();
    } catch (err) {
      setError(
        err?.response?.data?.detail || "Unable to remove this team member.",
      );
    }
  }

  return (
    <>
      {/* =====================================================
          PROFILE BUTTON
      ====================================================== */}

      <div className="user-menu-wrap" ref={menuRef}>
        <button
          type="button"
          className="user-menu-trigger"
          onClick={() => setMenuOpen((value) => !value)}
          aria-label="Open profile menu"
          aria-expanded={menuOpen}
        >
          {getInitials(user.name)}
        </button>

        {menuOpen && (
          <div className="user-menu-panel user-menu-panel-professional">
            <div className="user-menu-identity">
              <div className="user-menu-avatar">{getInitials(user.name)}</div>

              <div className="user-menu-details">
                <strong>{user.name}</strong>
                <span>{user.email}</span>
              </div>
            </div>

            <div className="user-menu-workspace">
              <span>WORKSPACE</span>
              <strong>REVIVE OPERATIONS</strong>
            </div>

            <div className="user-menu-access-row">
              <span>ACCESS LEVEL</span>
              <b className={`user-menu-role-badge user-role-${user.role}`}>
                {roleInfo(user.role).short}
              </b>
            </div>

            {isAdmin && (
              <button
                type="button"
                className="user-menu-team-button"
                onClick={openTeamManagement}
              >
                <span className="user-menu-team-icon">+</span>

                <span className="user-menu-team-copy">
                  <strong>Team management</strong>
                  <small>Members &amp; permissions</small>
                </span>

                <span className="user-menu-team-arrow">→</span>
              </button>
            )}

            <button
              type="button"
              className="user-menu-team-button"
              onClick={openChangePassword}
            >
              <span className="user-menu-team-icon">⚿</span>

              <span className="user-menu-team-copy">
                <strong>Change password</strong>
                <small>Update your account credentials</small>
              </span>

              <span className="user-menu-team-arrow">→</span>
            </button>

            <button
              type="button"
              className="user-menu-signout"
              onClick={() => {
                setMenuOpen(false);
                logout();
              }}
            >
              Sign out
            </button>
          </div>
        )}
      </div>

      {/* =====================================================
          TEAM MANAGEMENT
      ====================================================== */}

      {teamOpen &&
        createPortal(
          <div
            className="tmx-overlay"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) {
                closeTeamManagement();
              }
            }}
          >
            <section
              className="tmx-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="tmx-team-title"
            >
              {/* ==============================================
                HEADER
            =============================================== */}

              <header className="tmx-dialog-header">
                <div className="tmx-dialog-title-area">
                  <div className="tmx-dialog-icon">+</div>

                  <div>
                    <div className="tmx-eyebrow">REVIVE OPERATIONS</div>

                    <h2 id="tmx-team-title">Team management</h2>

                    <p>
                      Add teammates and control what they can access in this
                      workspace.
                    </p>
                  </div>
                </div>

                <button
                  type="button"
                  className="tmx-close"
                  onClick={closeTeamManagement}
                  aria-label="Close team management"
                >
                  ×
                </button>
              </header>

              {/* ==============================================
                FEEDBACK
            =============================================== */}

              {error && (
                <div className="tmx-feedback tmx-feedback-error">
                  <strong>Action couldn't be completed</strong>
                  <span>{error}</span>
                </div>
              )}

              {message && (
                <div className="tmx-feedback tmx-feedback-success">
                  <strong>Done</strong>
                  <span>{message}</span>
                </div>
              )}

              {/* ==============================================
                BODY
            =============================================== */}

              <div className="tmx-dialog-body">
                {/* ============================================
                  LEFT — CREATE ACCOUNT
              ============================================= */}

                <section className="tmx-create-panel">
                  <div className="tmx-panel-heading">
                    <div>
                      <span className="tmx-panel-kicker">ADD MEMBER</span>

                      <h3>Create a team account</h3>
                    </div>
                  </div>

                  <p className="tmx-panel-description">
                    Create individual credentials for a teammate. They can then
                    sign in using their own work email and password.
                  </p>

                  <form className="tmx-form" onSubmit={handleCreateMember}>
                    <label>
                      <span>Full name</span>

                      <input
                        type="text"
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                        placeholder="e.g. Rahul Sharma"
                        autoComplete="name"
                      />
                    </label>

                    <label>
                      <span>Work email</span>

                      <input
                        type="email"
                        value={email}
                        onChange={(event) => setEmail(event.target.value)}
                        placeholder="rahul@company.com"
                        autoComplete="email"
                      />
                    </label>

                    <label>
                      <span>Initial password</span>

                      <input
                        type="password"
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        placeholder="At least 8 characters"
                        autoComplete="new-password"
                        minLength={8}
                      />
                    </label>

                    <div className="tmx-role-section">
                      <div className="tmx-role-heading">
                        <span>Access level</span>

                        <small>Choose what this person can do</small>
                      </div>

                      <div className="tmx-role-options">
                        {Object.entries(ROLE_META).map(([roleKey, meta]) => (
                          <button
                            type="button"
                            key={roleKey}
                            className={`tmx-role-option ${
                              role === roleKey ? "tmx-role-selected" : ""
                            }`}
                            onClick={() => setRole(roleKey)}
                          >
                            <div className="tmx-role-option-top">
                              <span
                                className={`tmx-role-radio ${
                                  role === roleKey ? "tmx-radio-selected" : ""
                                }`}
                              />

                              <strong>{meta.label}</strong>

                              {role === roleKey && (
                                <span className="tmx-role-check">✓</span>
                              )}
                            </div>

                            <span className="tmx-role-option-description">
                              {meta.description}
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>

                    <button
                      type="submit"
                      className="tmx-create-button"
                      disabled={creating}
                    >
                      {creating ? "Creating account..." : "Create account"}

                      {!creating && <span>→</span>}
                    </button>
                  </form>

                  <div className="tmx-security-note">
                    <span>✓</span>

                    <p>
                      Each teammate gets their own credentials. Passwords are
                      never displayed in the team list.
                    </p>
                  </div>
                </section>

                {/* ==========================================
                  RIGHT — CURRENT TEAM
              =========================================== */}

                <section className="tmx-members-panel">
                  <div className="tmx-members-heading">
                    <div>
                      <span className="tmx-panel-kicker">CURRENT TEAM</span>

                      <h3>Workspace members</h3>
                    </div>

                    <span className="tmx-member-count">{users.length}</span>
                  </div>

                  <p className="tmx-panel-description">
                    Everyone currently authorized to access this Revive
                    workspace.
                  </p>

                  {loadingUsers ? (
                    <div className="tmx-state">
                      <span className="tmx-loading-dot" />
                      Loading team members...
                    </div>
                  ) : users.length === 0 ? (
                    <div className="tmx-state">No team members found.</div>
                  ) : (
                    <div className="tmx-members-list">
                      {users.map((member) => {
                        const info = roleInfo(member.role);

                        const isCurrentUser = member.id === user.id;

                        return (
                          <div className="tmx-member" key={member.id}>
                            <div className="tmx-member-avatar">
                              {getInitials(member.name)}
                            </div>

                            <div className="tmx-member-main">
                              <div className="tmx-member-name">
                                <strong>{member.name}</strong>

                                {isCurrentUser && (
                                  <span className="tmx-you-badge">YOU</span>
                                )}
                              </div>

                              <span className="tmx-member-email">
                                {member.email}
                              </span>

                              <span className="tmx-member-role-description">
                                {info.description}
                              </span>
                            </div>

                            <div className="tmx-member-actions">
                              <Dropdown
                                value={member.role}
                                onChange={(nextRole) =>
                                  handleRoleChange(member, nextRole)
                                }
                                disabled={isCurrentUser}
                                className="tmx-role-dropdown"
                                options={[
                                  { value: "admin", label: "ADMIN" },
                                  { value: "operator", label: "OPERATOR" },
                                  { value: "viewer", label: "VIEWER" },
                                ]}
                              />

                              {!isCurrentUser && (
                                <button
                                  type="button"
                                  className="tmx-remove"
                                  onClick={() => handleRemove(member)}
                                  aria-label={`Remove ${member.name}`}
                                >
                                  Remove
                                </button>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* ========================================
                    ROLE EXPLANATION
                ========================================= */}

                  <div className="tmx-role-guide">
                    <div className="tmx-role-guide-title">ACCESS LEVELS</div>

                    <div className="tmx-role-guide-grid">
                      <div>
                        <b>ADMIN</b>
                        <span>Full access + team management</span>
                      </div>

                      <div>
                        <b>OPERATOR</b>
                        <span>Recovery operations</span>
                      </div>

                      <div>
                        <b>VIEWER</b>
                        <span>Read-only access</span>
                      </div>
                    </div>
                  </div>
                </section>
              </div>

              {/* ==============================================
                FOOTER
            =============================================== */}

              <footer className="tmx-dialog-footer">
                <span>
                  Private workspace · Individual accounts · Role-based access
                </span>

                <button type="button" onClick={closeTeamManagement}>
                  Done
                </button>
              </footer>
            </section>
          </div>,
          document.body,
        )}

      {/* =====================================================
          CHANGE PASSWORD
      ====================================================== */}

      {passwordOpen &&
        createPortal(
          <div
            className="tmx-overlay pwx-overlay"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) {
                closeChangePassword();
              }
            }}
          >
            <section
              className="tmx-dialog pwx-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="pwx-title"
            >
              <header className="tmx-dialog-header">
                <div className="tmx-dialog-title-area">
                  <div className="tmx-dialog-icon">⚿</div>

                  <div>
                    <div className="tmx-eyebrow">ACCOUNT SECURITY</div>

                    <h2 id="pwx-title">Change password</h2>

                    <p>
                      Update the password used to sign in to this Revive
                      workspace.
                    </p>
                  </div>
                </div>

                <button
                  type="button"
                  className="tmx-close"
                  onClick={closeChangePassword}
                  aria-label="Close change password"
                >
                  ×
                </button>
              </header>

              {passwordError && (
                <div className="tmx-feedback tmx-feedback-error">
                  <strong>Action couldn't be completed</strong>
                  <span>{passwordError}</span>
                </div>
              )}

              {passwordMessage && (
                <div className="tmx-feedback tmx-feedback-success">
                  <strong>Done</strong>
                  <span>{passwordMessage}</span>
                </div>
              )}

              <div className="pwx-dialog-body">
                <form className="tmx-form" onSubmit={handleChangePassword}>
                  <label>
                    <span>Current password</span>

                    <input
                      type="password"
                      value={currentPassword}
                      onChange={(event) =>
                        setCurrentPassword(event.target.value)
                      }
                      placeholder="Enter your current password"
                      autoComplete="current-password"
                    />
                  </label>

                  <label>
                    <span>New password</span>

                    <input
                      type="password"
                      value={newPassword}
                      onChange={(event) => setNewPassword(event.target.value)}
                      placeholder="At least 8 characters"
                      autoComplete="new-password"
                      minLength={8}
                    />
                  </label>

                  <label>
                    <span>Confirm new password</span>

                    <input
                      type="password"
                      value={confirmPassword}
                      onChange={(event) =>
                        setConfirmPassword(event.target.value)
                      }
                      placeholder="Re-enter your new password"
                      autoComplete="new-password"
                      minLength={8}
                    />
                  </label>

                  <button
                    type="submit"
                    className="tmx-create-button"
                    disabled={changingPassword}
                  >
                    {changingPassword ? "Updating..." : "Update password"}

                    {!changingPassword && <span>→</span>}
                  </button>
                </form>
              </div>

              <footer className="tmx-dialog-footer">
                <span>
                  Your current password is required to confirm this change.
                </span>

                <button type="button" onClick={closeChangePassword}>
                  Done
                </button>
              </footer>
            </section>
          </div>,
          document.body,
        )}
    </>
  );
}
