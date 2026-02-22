"""PeakForm session persistence.

Backends
--------
- **Local filesystem** (default): ``~/.peakform/``
  Good for local development.  Ephemeral on Cloud Run.

- **Google Cloud Storage** (recommended for Cloud Run):
  Set ``PEAKFORM_GCS_BUCKET`` env var to a bucket name.
  The Cloud Run service account needs ``roles/storage.objectUser``.

State expires on the Saturday following the analysis week-end date:
  week_end (Sunday) + 6 days → expiry Saturday.
"""

from __future__ import annotations

import abc
import json
import os
import pickle
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Storage keys
# ---------------------------------------------------------------------------

_SESSION  = "session.json"
_RESULT   = "result.pkl"
_REC      = "rec.json"
_MESSAGES = "messages.json"
_MF_UPLOAD     = "uploads/macrofactor.xlsx"
_GARMIN_UPLOAD = "uploads/garmin.csv"

_VERSION = 1


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------

class _Backend(abc.ABC):
    @abc.abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...
    @abc.abstractmethod
    def write_text(self, key: str, text: str) -> None: ...
    @abc.abstractmethod
    def read_bytes(self, key: str) -> Optional[bytes]: ...
    @abc.abstractmethod
    def read_text(self, key: str) -> Optional[str]: ...
    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...
    @abc.abstractmethod
    def delete(self, key: str) -> None: ...


class _LocalBackend(_Backend):
    """Persist to a directory on the local filesystem."""

    def __init__(self, base_dir: Path):
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = self._base / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_bytes(self, key: str, data: bytes) -> None:
        self._path(key).write_bytes(data)

    def write_text(self, key: str, text: str) -> None:
        self._path(key).write_text(text, encoding="utf-8")

    def read_bytes(self, key: str) -> Optional[bytes]:
        p = self._base / key
        return p.read_bytes() if p.exists() else None

    def read_text(self, key: str) -> Optional[str]:
        p = self._base / key
        return p.read_text(encoding="utf-8") if p.exists() else None

    def exists(self, key: str) -> bool:
        return (self._base / key).exists()

    def delete(self, key: str) -> None:
        p = self._base / key
        if p.exists():
            p.unlink(missing_ok=True)


class _GCSBackend(_Backend):
    """Persist to a Google Cloud Storage bucket (cross-device, Cloud Run safe)."""

    def __init__(self, bucket_name: str, prefix: str = "state"):
        from google.cloud import storage as gcs  # lazy — heavy import
        self._bucket = gcs.Client().bucket(bucket_name)
        self._prefix = prefix

    def _blob(self, key: str):
        return self._bucket.blob(f"{self._prefix}/{key}")

    def write_bytes(self, key: str, data: bytes) -> None:
        self._blob(key).upload_from_string(data, content_type="application/octet-stream")

    def write_text(self, key: str, text: str) -> None:
        self._blob(key).upload_from_string(text, content_type="text/plain; charset=utf-8")

    def read_bytes(self, key: str) -> Optional[bytes]:
        blob = self._blob(key)
        return blob.download_as_bytes() if blob.exists() else None

    def read_text(self, key: str) -> Optional[str]:
        blob = self._blob(key)
        return blob.download_as_text(encoding="utf-8") if blob.exists() else None

    def exists(self, key: str) -> bool:
        return self._blob(key).exists()

    def delete(self, key: str) -> None:
        blob = self._blob(key)
        if blob.exists():
            blob.delete()


# ---------------------------------------------------------------------------
# Backend factory (cached)
# ---------------------------------------------------------------------------

_backend_cache: Optional[_Backend] = None


def _get_backend() -> _Backend:
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache

    bucket = os.environ.get("PEAKFORM_GCS_BUCKET")
    if bucket:
        try:
            _backend_cache = _GCSBackend(bucket)
            return _backend_cache
        except Exception:
            pass  # fall through to local

    base = Path(os.environ.get("PEAKFORM_STATE_DIR", str(Path.home() / ".peakform")))
    _backend_cache = _LocalBackend(base)
    return _backend_cache


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------

def _expiry_date(week_end_iso: str) -> date:
    """week_end is Sunday; expiry Saturday is 6 days later."""
    return date.fromisoformat(week_end_iso) + timedelta(days=6)


def is_expired(session: dict) -> bool:
    try:
        return date.today() >= _expiry_date(session["week_end"])
    except (KeyError, ValueError, TypeError):
        return True


def days_until_reset(session: dict) -> Optional[int]:
    try:
        return max(0, (_expiry_date(session["week_end"]) - date.today()).days)
    except Exception:
        return None


def is_saturday() -> bool:
    return date.today().weekday() == 5


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_uploads(mf_path: str, garmin_path: str) -> None:
    be = _get_backend()
    with open(mf_path, "rb") as f:
        be.write_bytes(_MF_UPLOAD, f.read())
    with open(garmin_path, "rb") as f:
        be.write_bytes(_GARMIN_UPLOAD, f.read())


def save_result(result: Any) -> None:
    _get_backend().write_bytes(
        _RESULT, pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
    )


def save_session_meta(week_start: str, week_end: str) -> None:
    _get_backend().write_text(
        _SESSION,
        json.dumps(
            {
                "version": _VERSION,
                "week_start": week_start,
                "week_end": week_end,
                "saved_at": date.today().isoformat(),
            },
            indent=2,
        ),
    )


def save_rec(rec_dict: dict) -> None:
    _get_backend().write_text(_REC, json.dumps(rec_dict, indent=2, default=str))


def save_messages(messages: list) -> None:
    _get_backend().write_text(_MESSAGES, json.dumps(messages, indent=2))


def save_all(result: Any, rec_dict: dict, messages: list) -> None:
    # ── Always save the lightweight files first ───────────────────────────────
    # These are small JSON blobs that never fail. Critically, they must be
    # written even if the pickle below fails, so that load_all() can at
    # minimum fall back to re-running analysis from the saved upload files.
    save_session_meta(
        result.week_start.strftime("%Y-%m-%d"),
        result.week_end.strftime("%Y-%m-%d"),
    )
    save_rec(rec_dict)
    save_messages(messages)

    # ── Optimistically try to pickle the full RunResult ───────────────────────
    # Pickle is large and can fail (e.g. non-picklable objects deep in
    # DataFrames, memory pressure). It is not fatal: load_all() will fall
    # back to re-running analysis from the saved uploads when result.pkl is
    # absent or corrupt.
    try:
        save_result(result)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_session() -> Optional[dict]:
    raw = _get_backend().read_text(_SESSION)
    if raw is None:
        return None
    try:
        s = json.loads(raw)
        return None if is_expired(s) else s
    except Exception:
        return None


def load_result() -> Optional[Any]:
    data = _get_backend().read_bytes(_RESULT)
    if data is None:
        return None
    try:
        return pickle.loads(data)
    except Exception:
        return None


def load_rec_dict() -> dict:
    raw = _get_backend().read_text(_REC)
    if raw is None:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def load_messages() -> list:
    raw = _get_backend().read_text(_MESSAGES)
    if raw is None:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _rerun_from_uploads(week_start_iso: str) -> Optional[Any]:
    """Download the saved upload files and re-run the full analysis.

    Used as a fallback when result.pkl is absent or unpicklable.
    Re-running is slower (~2s) but always works.
    """
    be = _get_backend()
    mf_bytes = be.read_bytes(_MF_UPLOAD)
    garmin_bytes = be.read_bytes(_GARMIN_UPLOAD)
    if mf_bytes is None or garmin_bytes is None:
        return None

    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp()
    try:
        import os
        mf_path = os.path.join(tmpdir, "macrofactor.xlsx")
        garmin_path = os.path.join(tmpdir, "garmin.csv")
        with open(mf_path, "wb") as f:
            f.write(mf_bytes)
        with open(garmin_path, "wb") as f:
            f.write(garmin_bytes)

        from peakform.agent import run_full
        # Pass the original week so we analyse the correct Mon–Sun window,
        # not the current week at load time.
        return run_full(mf_path, garmin_path, week=week_start_iso, verbose=False)
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_all() -> Optional[dict]:
    """
    Load the complete saved session.

    Returns None if no valid (non-expired) state exists.

    Load order
    ----------
    1. Read session.json (expiry + week bounds).
    2. Try result.pkl (fast — full RunResult pickled).
    3. Fall back to re-running analysis from saved upload files (~2 s).

    On success returns::

        { "session": dict, "result": RunResult, "rec": dict, "messages": list }
    """
    session = load_session()
    if session is None:
        return None

    result = load_result()
    if result is None:
        # pickle absent or corrupt — re-derive from the saved source files
        result = _rerun_from_uploads(session.get("week_start", ""))
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
    be = _get_backend()
    for key in [_SESSION, _REC, _MESSAGES, _RESULT]:
        try:
            be.delete(key)
        except Exception:
            pass
