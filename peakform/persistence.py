"""PeakForm session persistence.

Saves all generated state to ~/.peakform/ (configurable via PEAKFORM_STATE_DIR).

State expires on the Saturday that follows the analysis week-end date:
  - week_end is Sunday of the analysis week
  - expiry = week_end + 6 days  (the following Saturday)
  - On expiry Saturday the saved state is ignored; a fresh upload is expected

Directory layout
----------------
~/.peakform/
  session.json      metadata  (version, week_start, week_end, saved_at)
  result.pkl        pickled RunResult  (mf_data, garmin_data, report_md, …)
  rec.json          InterviewState as plain dict
  messages.json     AI Coach chat history
  uploads/
    macrofactor.xlsx
    garmin.csv
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Storage locations
# ---------------------------------------------------------------------------

_DEFAULT_DIR = Path.home() / ".peakform"
STATE_DIR     = Path(os.environ.get("PEAKFORM_STATE_DIR", str(_DEFAULT_DIR)))
UPLOADS_DIR   = STATE_DIR / "uploads"
SESSION_JSON  = STATE_DIR / "session.json"
REC_JSON      = STATE_DIR / "rec.json"
MESSAGES_JSON = STATE_DIR / "messages.json"
RESULT_PKL    = STATE_DIR / "result.pkl"
MF_UPLOAD     = UPLOADS_DIR / "macrofactor.xlsx"
GARMIN_UPLOAD = UPLOADS_DIR / "garmin.csv"

_VERSION = 1


def _ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------

def _expiry_date(week_end_iso: str) -> date:
    """week_end is Sunday; the expiry Saturday is 6 days later."""
    return date.fromisoformat(week_end_iso) + timedelta(days=6)


def is_expired(session: dict) -> bool:
    """True if the saved session is at or past its expiry Saturday."""
    try:
        return date.today() >= _expiry_date(session["week_end"])
    except (KeyError, ValueError, TypeError):
        return True


def days_until_reset(session: dict) -> Optional[int]:
    """Calendar days until the Saturday reset, or None if expired."""
    try:
        delta = (_expiry_date(session["week_end"]) - date.today()).days
        return max(0, delta)
    except Exception:
        return None


def is_saturday() -> bool:
    return date.today().weekday() == 5


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_uploads(mf_path: str, garmin_path: str) -> None:
    """Copy the two uploaded files to persistent storage."""
    _ensure_dirs()
    shutil.copy2(mf_path, MF_UPLOAD)
    shutil.copy2(garmin_path, GARMIN_UPLOAD)


def save_result(result: Any) -> None:
    """Pickle the full RunResult (DataFrames included)."""
    _ensure_dirs()
    with open(RESULT_PKL, "wb") as fh:
        pickle.dump(result, fh, protocol=pickle.HIGHEST_PROTOCOL)


def save_session_meta(week_start: str, week_end: str) -> None:
    _ensure_dirs()
    SESSION_JSON.write_text(
        json.dumps(
            {
                "version": _VERSION,
                "week_start": week_start,
                "week_end": week_end,
                "saved_at": date.today().isoformat(),
            },
            indent=2,
        )
    )


def save_rec(rec_dict: dict) -> None:
    """Persist InterviewState (as a plain dict) to JSON."""
    _ensure_dirs()
    REC_JSON.write_text(json.dumps(rec_dict, indent=2, default=str))


def save_messages(messages: list) -> None:
    """Persist AI Coach chat history to JSON."""
    _ensure_dirs()
    MESSAGES_JSON.write_text(json.dumps(messages, indent=2))


def save_all(result: Any, rec_dict: dict, messages: list) -> None:
    """Save result + metadata + rec + messages in a single call."""
    save_result(result)
    save_session_meta(
        result.week_start.strftime("%Y-%m-%d"),
        result.week_end.strftime("%Y-%m-%d"),
    )
    save_rec(rec_dict)
    save_messages(messages)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_session() -> Optional[dict]:
    """Return raw session metadata if it exists and is not expired."""
    if not SESSION_JSON.exists():
        return None
    try:
        s = json.loads(SESSION_JSON.read_text())
        return None if is_expired(s) else s
    except Exception:
        return None


def load_result() -> Optional[Any]:
    """Unpickle the saved RunResult, or None on any error."""
    if not RESULT_PKL.exists():
        return None
    try:
        with open(RESULT_PKL, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def load_rec_dict() -> dict:
    if not REC_JSON.exists():
        return {}
    try:
        return json.loads(REC_JSON.read_text())
    except Exception:
        return {}


def load_messages() -> list:
    if not MESSAGES_JSON.exists():
        return []
    try:
        return json.loads(MESSAGES_JSON.read_text())
    except Exception:
        return []


def load_all() -> Optional[dict]:
    """
    Load the complete saved session.

    Returns None if no valid (non-expired) state exists.
    On success returns:
        {
            "session":  dict,
            "result":   RunResult,
            "rec":      dict,
            "messages": list,
        }
    """
    session = load_session()
    if session is None:
        return None
    result = load_result()
    if result is None:
        return None
    return {
        "session": session,
        "result": result,
        "rec": load_rec_dict(),
        "messages": load_messages(),
    }


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

def clear_all() -> None:
    """Delete every persisted file (called on explicit reset)."""
    for p in [SESSION_JSON, REC_JSON, MESSAGES_JSON, RESULT_PKL]:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
