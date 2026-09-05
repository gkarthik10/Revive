import { useEffect, useRef, useState } from "react";

/**
 * Dropdown
 *
 * A generic themed stand-in for a native <select>, built the same way as
 * VoiceScriptPanel's ChannelDropdown: a native select's closed state can be
 * styled with CSS, but the open option list is rendered by the OS/browser
 * and ignores app CSS entirely — on a dark UI that shows up as a jarring
 * white system menu. This renders both the closed trigger and the open
 * list ourselves, so the dark theme holds all the way through.
 *
 * Usage:
 *   <Dropdown
 *     value={decisionFilter}
 *     onChange={setDecisionFilter}
 *     options={[
 *       { value: "ALL", label: "All decisions" },
 *       { value: "PURSUE", label: "Pursue" },
 *       { value: "STOP", label: "Stop" },
 *     ]}
 *   />
 */
export default function Dropdown({
  value,
  onChange,
  options,
  disabled = false,
  className = "",
  placeholder = "Select...",
}) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(() =>
    options.findIndex((option) => option.value === value),
  );
  const rootRef = useRef(null);

  const selected = options.find((option) => option.value === value);

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
      setActiveIndex(options.findIndex((option) => option.value === value));
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  function handleListKeyDown(event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => Math.min(index + 1, options.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (options[activeIndex]) {
        onChange(options[activeIndex].value);
      }
      setOpen(false);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div
      className={"app-dropdown" + (className ? ` ${className}` : "")}
      ref={rootRef}
    >
      <button
        type="button"
        className="app-dropdown-trigger"
        onClick={() => !disabled && setOpen((isOpen) => !isOpen)}
        onKeyDown={handleTriggerKeyDown}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span>{selected ? selected.label : placeholder}</span>

        <svg
          className="app-dropdown-caret"
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
          className="app-dropdown-list"
          role="listbox"
          tabIndex={-1}
          onKeyDown={handleListKeyDown}
          ref={(node) => node?.focus()}
        >
          {options.map((option, index) => (
            <li
              key={option.value}
              role="option"
              aria-selected={option.value === value}
              className={
                "app-dropdown-option" +
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
