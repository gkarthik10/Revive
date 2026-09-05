"""
Revive - Hinglish Voice Recovery

Generates spoken-style Hinglish (Hindi + English, Latin script)
recovery scripts for outbound IVR calls and WhatsApp voice notes,
grounded directly in an already-diagnosed Revive case.

Design constraints (deliberate, matching the rest of the codebase):

    - This module never invents business logic. It reads a case's
      existing root_cause_label (from app.diagnosis.taxonomy, via
      the pipeline) and an optional promise-to-pay record (from
      app.promise_tracker.tracker) and turns them into a script -
      it does not decide whether to pursue the case, that is the
      policy engine and ROI engine's job.

    - Text-to-speech is real, not mocked, when configured: if
      ELEVENLABS_API_KEY is set, this module makes a real REST
      call to ElevenLabs and saves a real mp3. If it is not set,
      every function still works end-to-end and returns a script
      with audio_provider="not_configured" - the pipeline never
      blocks on missing credentials, same pattern as the LLM
      fallback in app/diagnosis/.

    - Every generated script is appended to a JSON-persisted store
      (mirroring app/customer_alerts/alerts.py) so "what did we
      tell this customer" is always answerable - an audit trail,
      not a one-off print statement.

Supported root causes: every label in app.diagnosis.taxonomy.
Unknown labels fall back to a generic reminder script rather than
raising, since a voice script should never crash a recovery run.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ============================================================
# Storage
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = BASE_DIR / "data"

VOICE_SCRIPTS_FILE = Path(
    os.getenv(
        "VOICE_SCRIPTS_FILE",
        str(DATA_DIR / "voice_scripts.json"),
    )
)

AUDIO_DIR = Path(
    os.getenv(
        "VOICE_AUDIO_DIR",
        str(DATA_DIR / "voice_audio"),
    )
)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv(
    "ELEVENLABS_VOICE_ID",
    "21m00Tcm4TlvDq8ikWAM",  # ElevenLabs' default public demo voice
)
ELEVENLABS_TIMEOUT = float(os.getenv("ELEVENLABS_HTTP_TIMEOUT_SECONDS", "30"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# Helpers (same pattern as app/notifications/notifications.py)
# ============================================================

def _value(item: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return default


def _money(value: Any) -> str:
    try:
        return f"₹{float(value):,.0f}"
    except (TypeError, ValueError):
        return "₹0"


def _first_name(full_name: Any) -> str:
    text = str(full_name or "").strip()
    if not text:
        return "aap"  # polite fallback: "you" in Hindi
    return text.split()[0]


def _friendly_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.strftime("%d %B")


# ============================================================
# Templates
# ============================================================
#
# Each template is a callable(ctx: dict) -> str so wording can
# react to amount/date/name without a second templating layer.
# ctx always contains: name, amount, due_date (may be None),
# case_id, is_repeat (bool - True if a promise was already broken
# once for this case).

def _tpl_insufficient_funds(ctx: dict[str, Any]) -> str:
    return (
        f"Namaste {ctx['name']}, main Revive se bol raha hoon. Aapka "
        f"{ctx['amount']} ka payment is baar account mein balance kam "
        f"hone ki wajah se process nahi ho paaya. Koi baat nahi - jab "
        f"funds available hon, aap seedha payment link se retry kar "
        f"sakte hain. Kya main aapko ek convenient reminder date "
        f"bhej doon?"
    )


def _tpl_otp_timeout(ctx: dict[str, Any]) -> str:
    return (
        f"Hello {ctx['name']}, Revive se baat kar raha hoon. Aapka "
        f"{ctx['amount']} ka transaction OTP verify hone se pehle "
        f"expire ho gaya tha - bas itni si baat thi. Aap dobara try "
        f"karenge to is baar OTP time pe enter karte hi payment "
        f"confirm ho jaayega. Main aapko fresh payment link bhej "
        f"raha hoon."
    )


def _tpl_issuer_declined(ctx: dict[str, Any]) -> str:
    return (
        f"Namaste {ctx['name']}, main Revive ki taraf se call kar "
        f"raha hoon. Aapke bank ne {ctx['amount']} ka payment decline "
        f"kar diya tha - aksar yeh temporary hota hai, bank ki taraf "
        f"se koi permanent issue nahi. Aap ek baar retry kar sakte "
        f"hain, ya chaahen to hum kisi doosre card/UPI se try karne "
        f"ka option bhi bhej sakte hain."
    )


def _tpl_card_expired(ctx: dict[str, Any]) -> str:
    return (
        f"Hi {ctx['name']}, Revive se. Aapka {ctx['amount']} ka "
        f"payment is liye nahi ho paaya kyunki card expire ho chuka "
        f"hai. Bas naya card details update karne se kaam ho "
        f"jaayega - main aapko ek secure update link bhej raha hoon, "
        f"do minute ka kaam hai."
    )


def _tpl_mandate_expired_or_revoked(ctx: dict[str, Any]) -> str:
    return (
        f"Namaste {ctx['name']}, main Revive se baat kar raha hoon. "
        f"Aapka autopay mandate expire ho gaya hai ya cancel ho gaya "
        f"hai, is liye {ctx['amount']} ka payment nahi kat paaya. "
        f"Isse dobara chalu rakhne ke liye ek chhoti si "
        f"re-authorization karni hogi - main link bhej raha hoon, "
        f"aap apni bank app se ek baar approve kar dijiye."
    )


def _tpl_mandate_debit_failed(ctx: dict[str, Any]) -> str:
    return (
        f"Hello {ctx['name']}, Revive se. Aapka autopay mandate "
        f"active hai, bas is cycle mein {ctx['amount']} ka debit "
        f"attempt fail ho gaya - shayad balance kam tha us din. "
        f"Hum NPCI ke rules ke hisaab se ek notice ke saath dobara "
        f"try karenge, aapko koi extra kadam nahi uthana."
    )


def _tpl_network_error(ctx: dict[str, Any]) -> str:
    return (
        f"Namaste {ctx['name']}, Revive se call hai. Aapka "
        f"{ctx['amount']} ka payment ek network glitch ki wajah se "
        f"beech mein reh gaya - na paisa kata hai, na order confirm "
        f"hua hai. Aap seedha dobara try kar sakte hain, ya main "
        f"aapko fresh link bhej doon?"
    )


def _tpl_checkout_abandonment(ctx: dict[str, Any]) -> str:
    return (
        f"Hi {ctx['name']}, main Revive se. Aapne checkout shuru "
        f"kiya tha, {ctx['amount']} ka order, lekin poora nahi kar "
        f"paaye. Aapke liye cart abhi bhi saved hai - main ek "
        f"direct link bhej raha hoon, ek click mein payment complete "
        f"ho jaayegi, koi extra step nahi."
    )


def _tpl_b2b_cashflow_delay(ctx: dict[str, Any]) -> str:
    due = f" {ctx['due_date']} tak" if ctx.get("due_date") else ""
    return (
        f"Namaste {ctx['name']}, main Revive se aapke accounts team "
        f"se baat kar raha hoon. Invoice ka {ctx['amount']} abhi "
        f"outstanding hai. Hum samajhte hain cashflow timing kabhi "
        f"kabhi tight hoti hai - kya hum ek promise-to-pay date"
        f"{due} fix kar sakte hain, taaki dono taraf se clarity "
        f"rahe?"
    )


def _tpl_invoice_dispute(ctx: dict[str, Any]) -> str:
    return (
        f"Namaste {ctx['name']}, Revive se baat kar raha hoon. "
        f"Humein pata hai ki {ctx['amount']} ke invoice par kuch "
        f"dispute flag hua hai. Main is call ko yahin rok raha hoon "
        f"aur humari finance support team seedha aapse contact "
        f"karegi resolve karne ke liye - koi payment pressure abhi "
        f"nahi."
    )


def _tpl_payment_approval_delay(ctx: dict[str, Any]) -> str:
    return (
        f"Hello {ctx['name']}, main Revive se. {ctx['amount']} ka "
        f"payment abhi aapki approval process mein hai, aisa lag "
        f"raha hai. Bas ek gentle follow-up ke liye call kiya tha - "
        f"koi expected approval date bata sakte hain, taaki hum "
        f"us hisaab se follow up karein?"
    )


def _tpl_administrative_delay(ctx: dict[str, Any]) -> str:
    due = f", expected settlement date {ctx['due_date']}" if ctx.get("due_date") else ""
    return (
        f"Namaste {ctx['name']}, Revive se call hai. {ctx['amount']} "
        f"ka payment thoda administrative delay mein hai, jaisa "
        f"humein samajh aaya{due}. Kya aap confirm kar sakte hain ki "
        f"yeh timeline sahi hai, ya kisi aur cheez ki zaroorat hai "
        f"process aage badhaane ke liye?"
    )


def _tpl_generic(ctx: dict[str, Any]) -> str:
    return (
        f"Namaste {ctx['name']}, main Revive se baat kar raha hoon "
        f"aapke {ctx['amount']} ke pending payment ke baare mein. "
        f"Main aapko ek convenient payment link bhej raha hoon - "
        f"jab bhi suitable ho, complete kar dijiye."
    )


_TEMPLATES: dict[str, Any] = {
    "insufficient_funds": _tpl_insufficient_funds,
    "otp_timeout": _tpl_otp_timeout,
    "issuer_declined": _tpl_issuer_declined,
    "card_expired": _tpl_card_expired,
    "mandate_expired_or_revoked": _tpl_mandate_expired_or_revoked,
    "mandate_debit_failed": _tpl_mandate_debit_failed,
    "network_error": _tpl_network_error,
    "checkout_abandonment": _tpl_checkout_abandonment,
    "b2b_cashflow_delay": _tpl_b2b_cashflow_delay,
    "invoice_dispute": _tpl_invoice_dispute,
    "payment_approval_delay": _tpl_payment_approval_delay,
    "administrative_delay": _tpl_administrative_delay,
}

_REPEAT_SUFFIX = (
    " Waise, main yeh bhi bata doon - pichli baar jo date decide "
    "hui thi woh nikal chuki hai, is liye agar koi naya timeline "
    "chahiye to abhi bata dijiye, hum usi hisaab se update kar denge."
)


# ============================================================
# Script model
# ============================================================

@dataclass(frozen=True)
class VoiceScript:
    """
    One generated Hinglish recovery script for one case.
    """

    script_id: str
    case_id: str
    root_cause: str
    channel: str  # "ivr_call" | "whatsapp_voice_note"
    language: str
    script_text: str
    generated_at: str

    audio_provider: str  # "elevenlabs" | "not_configured" | "error"
    audio_path: str | None = None
    audio_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "script_id": self.script_id,
            "case_id": self.case_id,
            "root_cause": self.root_cause,
            "channel": self.channel,
            "language": self.language,
            "script_text": self.script_text,
            "generated_at": self.generated_at,
            "audio_provider": self.audio_provider,
            "audio_path": self.audio_path,
            "audio_error": self.audio_error,
        }


# ============================================================
# Script generation
# ============================================================

def generate_hinglish_script(
    case: dict[str, Any],
    promise: dict[str, Any] | None = None,
    channel: str = "ivr_call",
) -> str:
    """
    Build a Hinglish script string from an already-diagnosed case.

    `case` is a plain dict as produced by the pipeline / cases.json
    (must contain at least root_cause_label and amount).
    `promise` is an optional promise-to-pay dict (as returned by
    PromiseTracker) - if its status is BROKEN, the script adds a
    short re-commitment ask.
    """

    root_cause = str(
        _value(case, "root_cause_label", "root_cause", default="")
    )

    name = _first_name(
        _value(case, "customer_name", "name")
    )

    amount = _money(
        _value(
            promise or {},
            "promised_amount",
            "outstanding_amount",
            default=_value(case, "amount"),
        )
    )

    due_date = None
    if promise:
        due_date = _friendly_date(
            _value(promise, "promise_date", "due_date")
        )

    is_repeat = bool(
        promise and str(_value(promise, "status", default="")).upper() == "BROKEN"
    )

    ctx = {
        "name": name,
        "amount": amount,
        "due_date": due_date,
        "case_id": _value(case, "case_id"),
    }

    template = _TEMPLATES.get(root_cause, _tpl_generic)
    text = template(ctx)

    if is_repeat and root_cause == "b2b_cashflow_delay":
        text += _REPEAT_SUFFIX

    return text


def _synthesize_audio(text: str, case_id: str) -> tuple[str, str | None, str | None]:
    """
    Attempt real ElevenLabs text-to-speech.

    Returns (provider, path_or_none, error_or_none). Never raises -
    a TTS failure degrades to a text-only script, it never blocks
    the recovery workflow.
    """

    if not ELEVENLABS_API_KEY:
        return "not_configured", None, None

    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.4,
                    "similarity_boost": 0.75,
                },
            },
            timeout=ELEVENLABS_TIMEOUT,
        )
    except requests.RequestException as exc:
        return "error", None, f"ElevenLabs request failed: {exc}"

    if response.status_code != 200:
        detail = response.text[:200]
        if response.status_code == 402 and "paid_plan_required" in response.text:
            detail = (
                "This ElevenLabs voice requires a paid plan for API access. "
                "Free-tier fix: clone your own voice in the ElevenLabs "
                "dashboard (up to 3 free Instant Voice Clones) and set "
                "ELEVENLABS_VOICE_ID to that voice's ID — a self-owned "
                "voice isn't subject to this restriction. Or upgrade to "
                "the ElevenLabs Starter plan to use the default demo voice."
            )
        return (
            "error",
            None,
            f"ElevenLabs returned HTTP {response.status_code}: {detail}",
        )

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = AUDIO_DIR / f"{case_id}-{uuid.uuid4().hex[:8]}.mp3"
    audio_path.write_bytes(response.content)

    return "elevenlabs", str(audio_path), None


# ============================================================
# Persisted store (mirrors app/customer_alerts/alerts.py)
# ============================================================

class VoiceScriptStore:
    """
    Thread-safe, JSON-persisted history of generated voice scripts.
    Gives the dashboard an audit trail of what was scripted/spoken
    for every case, the same way the recovery ledger tracks
    decisions and the customer alert service tracks emails sent.
    """

    def __init__(self, path: Path = VOICE_SCRIPTS_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(self._records, fh, indent=2, ensure_ascii=False)

    def generate(
        self,
        case: dict[str, Any],
        promise: dict[str, Any] | None = None,
        channel: str = "ivr_call",
        synthesize_audio: bool = False,
    ) -> VoiceScript:
        text = generate_hinglish_script(case, promise=promise, channel=channel)

        case_id = str(_value(case, "case_id", default="unknown"))
        root_cause = str(
            _value(case, "root_cause_label", "root_cause", default="unknown")
        )

        if synthesize_audio:
            provider, audio_path, audio_error = _synthesize_audio(text, case_id)
        else:
            provider, audio_path, audio_error = "not_configured", None, None

        script = VoiceScript(
            script_id=f"VS-{uuid.uuid4().hex[:10]}",
            case_id=case_id,
            root_cause=root_cause,
            channel=channel,
            language="hinglish",
            script_text=text,
            generated_at=_now(),
            audio_provider=provider,
            audio_path=audio_path,
            audio_error=audio_error,
        )

        with self._lock:
            self._records.append(script.to_dict())
            self._save()

        return script

    def list_for_case(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self._records if r.get("case_id") == case_id]

    def get_by_id(self, script_id: str) -> dict[str, Any] | None:
        with self._lock:
            for record in self._records:
                if record.get("script_id") == script_id:
                    return record
        return None

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)
        return {
            "total_scripts": len(records),
            "with_real_audio": sum(
                1 for r in records if r.get("audio_provider") == "elevenlabs"
            ),
            "audio_configured": bool(ELEVENLABS_API_KEY),
        }


voice_script_store = VoiceScriptStore()


# ============================================================
# Self-test
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("REVIVE — HINGLISH VOICE RECOVERY")
    print("Self-Test")
    print("=" * 60)

    from app.diagnosis.taxonomy import all_root_causes

    sample_case = {
        "case_id": "RV-VOICE-TEST",
        "customer_name": "Priya Sharma",
        "amount": 4599,
    }

    print("\nGenerated scripts, one per known root cause:\n")

    for cause in all_root_causes():
        case = dict(sample_case, root_cause_label=cause.label)
        text = generate_hinglish_script(case)
        assert text, f"Empty script for {cause.label}"
        assert "Priya" in text or "priya" in text.lower()
        print(f"• {cause.label}")
        print(f"  {text}\n")

    # Unknown root cause falls back gracefully instead of raising.
    fallback_case = dict(sample_case, root_cause_label="totally_unseen_label")
    fallback_text = generate_hinglish_script(fallback_case)
    assert fallback_text
    print(f"• (unknown label fallback)\n  {fallback_text}\n")

    # B2B repeat-broken-promise gets the extra re-commitment ask.
    b2b_case = dict(sample_case, root_cause_label="b2b_cashflow_delay")
    broken_promise = {
        "promised_amount": 120000,
        "promise_date": "2026-09-20T00:00:00",
        "status": "BROKEN",
    }
    repeat_text = generate_hinglish_script(b2b_case, promise=broken_promise)
    assert "naya timeline" in repeat_text
    print(f"• b2b_cashflow_delay (repeat/broken promise)\n  {repeat_text}\n")

    # Store round-trip, without attempting real network audio.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_store = VoiceScriptStore(path=Path(tmp) / "voice_scripts.json")
        result = test_store.generate(sample_case, channel="whatsapp_voice_note")
        assert result.audio_provider == "not_configured"
        assert result.script_id.startswith("VS-")
        assert len(test_store.list_for_case("RV-VOICE-TEST")) == 1
        assert (Path(tmp) / "voice_scripts.json").exists()

    print("✓ All 12 taxonomy root causes produce a valid script.")
    print("✓ Unknown root cause label falls back instead of raising.")
    print("✓ Broken-promise repeat case adds a re-commitment ask.")
    print("✓ Store persists and round-trips generated scripts.")
    print(
        f"✓ ElevenLabs audio: "
        f"{'configured' if ELEVENLABS_API_KEY else 'not configured (script-only mode)'}."
    )

    print()
    print("=" * 60)
    print("HINGLISH VOICE RECOVERY SELF-TEST: PASSED")
    print("=" * 60)