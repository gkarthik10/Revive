import { useEffect, useRef, useState } from "react";
import axios from "axios";

const API_BASE = "http://127.0.0.1:8000/api";

/**
 * Ops Copilot — floating chat widget for the internal dashboard.
 *
 * Talks to POST /api/copilot/chat and POST /api/copilot/confirm.
 * Any action the assistant proposes (creating a promise, generating a
 * payment link, marking paid, retrying a payment, settling A2A, ...)
 * arrives as `pending_action` and is rendered as an explicit
 * confirm/reject card — nothing happens until an operator clicks
 * Confirm.
 */
export default function CopilotWidget() {
  const [open, setOpen] = useState(false);
  const [showPromo, setShowPromo] = useState(false);
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      text:
        "Hi — I'm the Revive Ops Copilot. Ask me about cases, promises, " +
        "customers, or the ledger, or ask me to take an action (I'll always " +
        "check with you before anything actually happens).",
    },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [conversationId, setConversationId] = useState(null);
  const [pendingAction, setPendingAction] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const scrollRef = useRef(null);
  const promoTimerRef = useRef(null);
  const promoHideRef = useRef(null);

  useEffect(() => {
    const show = () => {
      if (open) return;
      setShowPromo(true);
      if (promoHideRef.current) window.clearTimeout(promoHideRef.current);
      promoHideRef.current = window.setTimeout(() => setShowPromo(false), 6500);
    };

    const firstTimer = window.setTimeout(show, 2500);
    promoTimerRef.current = window.setInterval(show, 20000);

    return () => {
      window.clearTimeout(firstTimer);
      if (promoTimerRef.current) window.clearInterval(promoTimerRef.current);
      if (promoHideRef.current) window.clearTimeout(promoHideRef.current);
    };
  }, [open]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, pendingAction, open]);

  async function sendMessage(text) {
    const trimmed = text.trim();
    if (!trimmed || sending) return;

    setMessages((m) => [...m, { role: "user", text: trimmed }]);
    setInput("");
    setSending(true);
    setError("");

    try {
      const { data } = await axios.post(`${API_BASE}/copilot/chat`, {
        message: trimmed,
        conversation_id: conversationId,
      });

      setConversationId(data.conversation_id);
      setMessages((m) => [...m, { role: "assistant", text: data.reply }]);
      setPendingAction(data.pending_action || null);
    } catch (err) {
      setError(
        err?.response?.data?.detail || err.message || "Something went wrong.",
      );
    } finally {
      setSending(false);
    }
  }

  async function respondToAction(approved) {
    if (!pendingAction || confirming) return;
    setConfirming(true);
    setError("");

    try {
      const { data } = await axios.post(`${API_BASE}/copilot/confirm`, {
        action_id: pendingAction.action_id,
        approved,
      });

      setMessages((m) => [
        ...m,
        {
          role: "system",
          text: approved
            ? `✓ Confirmed: ${pendingAction.summary}`
            : `✕ Declined: ${pendingAction.summary}`,
        },
        { role: "assistant", text: data.reply },
      ]);
      setPendingAction(data.pending_action || null);
    } catch (err) {
      setError(
        err?.response?.data?.detail || err.message || "Something went wrong.",
      );
    } finally {
      setConfirming(false);
    }
  }

  return (
    <div className="copilot-widget">
      {showPromo && !open && (
        <div className="copilot-promo" role="status">
          <button
            type="button"
            className="copilot-promo-close"
            onClick={() => setShowPromo(false)}
            aria-label="Dismiss Copilot tip"
          >
            ×
          </button>
          <strong>Want to finish tasks faster?</strong>
          <span>
            Try the automated bot here <b>→</b>
          </span>
          <i aria-hidden="true" />
        </div>
      )}

      {open && (
        <div className="copilot-panel">
          <div className="copilot-panel-header">
            <div>
              <div className="copilot-panel-title">Ops Copilot</div>
              <div className="copilot-panel-subtitle">
                Internal assistant · actions require confirmation
              </div>
            </div>
            <button
              type="button"
              className="copilot-close"
              onClick={() => setOpen(false)}
              aria-label="Close copilot"
            >
              ×
            </button>
          </div>

          <div className="copilot-messages" ref={scrollRef}>
            {messages.map((msg, idx) => (
              <div
                key={idx}
                className={`copilot-message copilot-message-${msg.role}`}
              >
                {msg.text}
              </div>
            ))}

            {pendingAction && (
              <div className="copilot-action-card">
                <div className="copilot-action-label">
                  Awaiting confirmation
                </div>
                <div className="copilot-action-summary">
                  {pendingAction.summary}
                </div>
                <div className="copilot-action-buttons">
                  <button
                    type="button"
                    className="copilot-action-confirm"
                    disabled={confirming}
                    onClick={() => respondToAction(true)}
                  >
                    {confirming ? "Working…" : "Confirm"}
                  </button>
                  <button
                    type="button"
                    className="copilot-action-reject"
                    disabled={confirming}
                    onClick={() => respondToAction(false)}
                  >
                    Reject
                  </button>
                </div>
              </div>
            )}

            {sending && (
              <div className="copilot-message copilot-message-assistant copilot-typing">
                …
              </div>
            )}
          </div>

          {error && <div className="copilot-error">{error}</div>}

          <form
            className="copilot-input-row"
            onSubmit={(e) => {
              e.preventDefault();
              sendMessage(input);
            }}
          >
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask about a case, promise, or customer…"
              disabled={sending}
            />
            <button type="submit" disabled={sending || !input.trim()}>
              Send
            </button>
          </form>
        </div>
      )}

      <button
        type="button"
        className="copilot-launcher"
        onClick={() => {
          setOpen((v) => !v);
          setShowPromo(false);
        }}
        aria-label="Open Ops Copilot"
      >
        {open ? "×" : "💬"}
      </button>
    </div>
  );
}
