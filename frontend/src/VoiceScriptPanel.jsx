import { useEffect, useRef, useState } from "react";
import axios from "axios";

const CHANNEL_OPTIONS = [
  { value: "ivr_call", label: "IVR Call" },
  { value: "whatsapp_voice_note", label: "WhatsApp Voice Note" },
];

/**
 * ChannelDropdown
 *
 * A small custom listbox standing in for a native <select>. A native
 * select's closed state can be themed with CSS, but its open option
 * list is rendered by the OS/browser chrome and ignores app CSS —
 * on a dark UI that shows up as a jarring white flash. This renders
 * both the closed control and the open list ourselves so the dark
 * theme holds throughout.
 */
function ChannelDropdown({ value, onChange, disabled }) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(() =>
    CHANNEL_OPTIONS.findIndex((option) => option.value === value),
  );
  const rootRef = useRef(null);

  const selected =
    CHANNEL_OPTIONS.find((option) => option.value === value) ??
    CHANNEL_OPTIONS[0];

  useEffect(() => {
    if (!open) return undefined;

    function handlePointerDown(event) {
      if (rootRef.current && !rootRef.current.contains(event.target)) {
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [open]);

  function handleTriggerKeyDown(event) {
    if (disabled) return;

    if (
      event.key === "ArrowDown" ||
      event.key === "Enter" ||
      event.key === " "
    ) {
      event.preventDefault();
      setOpen(true);
      setActiveIndex(
        CHANNEL_OPTIONS.findIndex((option) => option.value === value),
      );
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  function handleListKeyDown(event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) =>
        Math.min(index + 1, CHANNEL_OPTIONS.length - 1),
      );
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onChange(CHANNEL_OPTIONS[activeIndex].value);
      setOpen(false);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="channel-dropdown" ref={rootRef}>
      <button
        type="button"
        className="channel-dropdown-trigger explanation-input"
        onClick={() => !disabled && setOpen((isOpen) => !isOpen)}
        onKeyDown={handleTriggerKeyDown}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span>{selected.label}</span>
        <svg
          className="channel-dropdown-caret"
          width="12"
          height="12"
          viewBox="0 0 12 12"
          aria-hidden="true"
        >
          <path
            d="M2.5 4.5L6 8l3.5-3.5"
            stroke="currentColor"
            strokeWidth="1.4"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <ul
          className="channel-dropdown-list"
          role="listbox"
          tabIndex={-1}
          onKeyDown={handleListKeyDown}
          ref={(node) => node?.focus()}
        >
          {CHANNEL_OPTIONS.map((option, index) => (
            <li
              key={option.value}
              role="option"
              aria-selected={option.value === value}
              className={
                "channel-dropdown-option" +
                (option.value === value ? " is-selected" : "") +
                (index === activeIndex ? " is-active" : "")
              }
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              {option.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * VoiceScriptPanel
 *
 * Renders inside the CASE DETAIL MODAL in App.jsx, right after the
 * Decision Explainer box:
 *
 *   <VoiceScriptPanel caseId={selectedCase.case_id} apiBase={API_BASE} />
 *
 * Fetches nothing on mount — only when the operator clicks
 * "Generate script" — so it adds no load to the dashboard's
 * existing polling.
 */
export default function VoiceScriptPanel({ caseId, apiBase }) {
  const [channel, setChannel] = useState("ivr_call");
  const [synthesizeAudio, setSynthesizeAudio] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [script, setScript] = useState(null);

  async function handleGenerate() {
    setLoading(true);
    setError(null);
    setScript(null);
    try {
      const response = await axios.post(
        `${apiBase}/cases/${caseId}/voice-script`,
        { channel, synthesize_audio: synthesizeAudio },
      );
      setScript(response.data.script);
    } catch (err) {
      setError(
        err.response?.data?.detail ||
          err.message ||
          "Failed to generate script.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="settlement-reason decision-explainer-box">
      <span>HINGLISH VOICE RECOVERY SCRIPT</span>

      <p>
        Generate a spoken-style Hinglish recovery script for this case, ready
        for an IVR call or a WhatsApp voice note.
      </p>

      <div className="voice-script-controls">
        <ChannelDropdown
          value={channel}
          onChange={setChannel}
          disabled={loading}
        />

        <label className="voice-script-audio-toggle">
          <input
            type="checkbox"
            checked={synthesizeAudio}
            onChange={(event) => setSynthesizeAudio(event.target.checked)}
            disabled={loading}
          />
          Generate real voice audio
        </label>

        <button
          className="run-button"
          onClick={handleGenerate}
          disabled={loading}
        >
          {loading ? "Generating..." : "✦ Generate Script"}
        </button>
      </div>

      {error && <div className="policy-warning">{error}</div>}

      {script && (
        <div className="roi-summary-line">
          <p>{script.script_text}</p>
          <small>
            {script.channel} · {script.root_cause} ·{" "}
            {script.audio_provider === "elevenlabs"
              ? "real audio generated"
              : script.audio_provider === "error"
                ? `audio failed: ${script.audio_error}`
                : "script only — TTS not configured"}
          </small>

          {script.audio_provider === "elevenlabs" && (
            <audio
              controls
              style={{ marginTop: "12px", width: "100%" }}
              src={`${apiBase}/voice-audio/${script.script_id}`}
            >
              Your browser does not support audio playback.
            </audio>
          )}

          {script.audio_provider === "not_configured" && synthesizeAudio && (
            <div className="policy-warning" style={{ marginTop: "8px" }}>
              ELEVENLABS_API_KEY isn't being picked up by the backend. Confirm
              it's in <code>revive/backend/.env</code> or{" "}
              <code>revive/.env</code>, then restart <code>uvicorn</code>.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
