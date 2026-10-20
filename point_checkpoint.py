"""Private, resumable point I/O; no forecast physics or database writes.

Checkpoint files are staging, NOT transport-v2 forecasts. Only the canonical
point_reader.select_point builds a complete, validated export. No Dask arrays
are accepted by this loader. Xarray must open the store with chunks=None.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
import stat
import tempfile


# The underlying object-store client already retries transient requests.
# Retry at the point level at most once more, then preserve and report the
# original error. Never retry NotFound/auth errors or mask missing values.
EXTRA_POINT_ATTEMPTS = 1
EXTRA_POINT_RETRY_DELAY_SECONDS = 0.5


def classify_point_read_error(exc):
    """Return (safe category, retryable) without leaking remote exceptions.

    GenericError is a documented obstore fallback, not an error diagnosis.
    Never log its unfiltered message: it can include URLs and identifiers.
    """
    name = type(exc).__name__
    if isinstance(exc, (FileNotFoundError,)) or name == "NotFoundError":
        return "OBJECT_NOT_FOUND", False
    if name in ("PermissionDeniedError", "UnauthenticatedError"):
        return "ACCESS_DENIED", False
    if isinstance(exc, TimeoutError):
        return "REQUEST_TIMEOUT", True
    if isinstance(exc, (ValueError, TypeError, IndexError)):
        return "INVALID_DATA_OR_REQUEST", False

    # HTTP codes, when present inside opaque obstore errors, can be
    # classified safely without printing raw storage paths or credentials.
    seen = set()
    pending = [exc]
    code = None
    for _ in range(5):
        if not pending:
            break
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current)
        status = re.search(
            r"(?:http(?:/[12](?:\.[01])?)?|status(?:\s+code)?)"
            r"\s*[:=]?\s*(401|403|404|408|429|500|502|503|504)\b",
            message, re.IGNORECASE,
        )
        if status:
            code = int(status.group(1))
            break
        pending.extend(
            error for error in (current.__cause__, current.__context__)
            if isinstance(error, Exception)
        )
    if code in (401, 403):
        return f"HTTP_{code}_ACCESS_DENIED", False
    if code == 404:
        return "HTTP_404_NOT_FOUND", False
    if code in (408, 429, 500, 502, 503, 504):
        return f"HTTP_{code}_RETRYABLE", True
    if name == "GenericError":
        # A single additional probe, never an unbounded retry loop.
        return "GENERIC_UNCLASSIFIED", True
    if isinstance(exc, (ConnectionError, OSError)):
        return "CONNECTION_OR_IO_ERROR", True
    return "UNCLASSIFIED", False

CHECKPOINT_SCHEMA = "weathernext3-point-checkpoint-v1"
MAX_CHECKPOINT_BYTES = 1024 * 1024
WORKERS = 4


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def atomic_json(path, value):
    """Commit private JSON atomically; interruption leaves the previous file."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".wn3-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(_canonical(value))
            out.write(b"\n")
            out.flush()
            os.fsync(out.fileno())
        # mkstemp already creates mode 0600. Replace, never follow, a symlink.
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CHECKPOINT_INVALID: duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError("CHECKPOINT_INVALID: non-standard numeric literal")


class PointCheckpoint(AbstractContextManager):
    """One writer per request; validate identity and content before reuse."""
    def __init__(self, directory, identity, required_fields):
        self.identity = identity
        self.required = set(required_fields)
        self.values = {}
        self.path = None if directory is None else (
            Path(directory).expanduser() / (_digest(identity) + ".checkpoint.json")
        )
        self._lock = None

    def __enter__(self):
        if self.path is None:
            return self
        import fcntl  # Cloud Shell / Linux; do not silently omit locking.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path.with_suffix(".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._lock = os.fdopen(fd, "a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._read()
        except BaseException:
            self._lock.close()
            self._lock = None
            raise
        return self

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        return False

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as src:
            info = os.fstat(src.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CHECKPOINT_BYTES
                    or info.st_mode & 0o077):
                raise ValueError("CHECKPOINT_INVALID: size, type or private permissions")
            obj = json.load(src, object_pairs_hook=_unique_pairs,
                            parse_constant=_bad_constant)
        if not isinstance(obj, dict) or set(obj) != {"body", "sha256"}:
            raise ValueError("CHECKPOINT_INVALID: invalid envelope")
        body = obj["body"]
        if not isinstance(body, dict) or set(body) != {"schema", "identity", "values"}:
            raise ValueError("CHECKPOINT_INVALID: invalid body")
        if obj["sha256"] != _digest(body):
            raise ValueError("CHECKPOINT_INVALID: checksum mismatch")
        if body["schema"] != CHECKPOINT_SCHEMA or body["identity"] != self.identity:
            raise ValueError("CHECKPOINT_INVALID: different run, site, fields or metadata")
        values = body["values"]
        if not isinstance(values, dict):
            raise ValueError("CHECKPOINT_INVALID: values must be an object")
        valid_leads = {str(i) for i in range(1, self.identity["hours"] + 1)}
        for lead, fields in values.items():
            if lead not in valid_leads or not isinstance(fields, dict):
                raise ValueError("CHECKPOINT_INVALID: invalid lead")
            for field, value in fields.items():
                self._validate(field, value)
        self.values = values

    def _validate(self, field, value):
        if field not in self.identity["available_fields"]:
            raise ValueError("CHECKPOINT_INVALID: unexpected field")
        if value is None:
            if field in self.required:
                raise ValueError("INCOMPLETE_HOURLY_VALUES: required P50 cannot be cached null")
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("CHECKPOINT_INVALID: non-finite native value")

    def put(self, lead, field, value):
        if type(lead) is not int or not 1 <= lead <= self.identity["hours"]:
            raise ValueError("CHECKPOINT_INVALID: invalid lead")
        self._validate(field, value)
        self.values.setdefault(str(lead), {})[field] = value
        if self.path is not None:
            body = {"schema": CHECKPOINT_SCHEMA, "identity": self.identity,
                    "values": self.values}
            atomic_json(self.path, {"body": body, "sha256": _digest(body)})

    def completed_hours(self):
        return sum(self.required.issubset(row) for row in self.values.values())


def _identity(subset, request):
    return {
        "run_prefix": request.prefix,
        "lat": float(request.lat), "lon": float(request.lon),
        "hours": request.hours, "requested_fields": list(request.fields),
        "available_fields": list(subset.data_vars),
        "selected_lat": float(subset["lat_0p1"].values),
        "selected_lon_360": float(subset["lon_0p1"].values),
        "init_time": str(subset["init_time"].values),
        "raw_units": {f: str(subset[f].attrs.get("units", "unspecified"))
                      for f in subset.data_vars},
        "array_layout": {f: {"dims": list(subset[f].dims),
                             "shape": list(subset[f].shape),
                             "dtype": str(subset[f].dtype)}
                         for f in subset.data_vars},
    }



def _seed_matching_p50_checkpoint(target, required_fields, emit):
    """Reuse a *verified* median-only checkpoint for a superset field request.

    Only fields change. Run, exact site, horizon, grid, initialization,
    units and each shared variable's array shape/dtype must match exactly.
    The old checkpoint remains unchanged and is read under its own lock.
    """
    if target.path is None:
        return 0
    wanted = list(required_fields)
    identity = target.identity
    if identity["requested_fields"] == wanted:
        return 0  # already in P50-only mode
    if not set(wanted).issubset(identity["available_fields"]):
        return 0

    source_identity = {
        **identity,
        "requested_fields": wanted,
        "available_fields": wanted,
        "raw_units": {field: identity["raw_units"][field]
                      for field in wanted},
        "array_layout": {field: identity["array_layout"][field]
                         for field in wanted},
    }
    source = PointCheckpoint(target.path.parent, source_identity, required_fields)
    if not os.path.lexists(source.path):
        return 0

    with source:  # validates SHA256, exact identity, every lead and value
        snapshot = {lead: dict(row) for lead, row in source.values.items()}

    # Fail closed BEFORE importing anything if two validated checkpoints
    # claim different values for the same initialized run and grid cell.
    for lead, fields in snapshot.items():
        current = target.values.get(lead, {})
        for field, value in fields.items():
            if field in current and current[field] != value:
                raise ValueError(
                    "CHECKPOINT_CONFLICT: different P50 values for the same "
                    "run/site/hour; refusing to combine snapshots"
                )

    imported = 0
    for lead in sorted(snapshot, key=int):
        for field, value in snapshot[lead].items():
            if field not in target.values.get(lead, {}):
                target.put(int(lead), field, value)
                imported += 1
    emit(f"REUSE_P50: {imported} validated median values copied from "
         f"compatible private checkpoint")
    return imported

def load_direct(subset, request, emit, *, checkpoint_dir=None):
    """Materialize only selected point scalars, at most four reads in flight.

    Coordinates/time selection and final JSON validation remain in the
    canonical reader. Completed scalars persist even if a later read fails.
    All coordinates/metadata are part of checkpoint identity; never mix runs.
    """
    import numpy as np
    import xarray as xr
    from point_reader import P50_FIELDS

    fields = tuple(subset.data_vars)
    for field in fields:
        array = subset[field]
        if array.chunks is not None:
            raise ValueError("DIRECT_REQUIRES_NO_DASK: open the store with chunks=None")
        if "lead_time" not in array.dims or array.size != request.hours:
            raise ValueError("unexpected extra data dimensions in direct point read")

    def read_scalar(field, index):
        # This indexes the lazy backend BEFORE materializing a numpy scalar.
        data = np.asarray(subset[field].isel(lead_time=index).values)
        if data.size != 1:
            raise ValueError("unexpected extra data dimensions in direct point read")
        value = float(data.item())
        if not math.isfinite(value):
            if field in P50_FIELDS:
                raise ValueError(
                    f"INCOMPLETE_HOURLY_VALUES: {field} at lead {index + 1}"
                )
            value = None
        return value

    with PointCheckpoint(checkpoint_dir, _identity(subset, request), P50_FIELDS) as cp:
        emit(f"DIRECT: no Dask; at most {WORKERS} point reads in flight")
        _seed_matching_p50_checkpoint(cp, P50_FIELDS, emit)
        saved = sum(len(row) for row in cp.values.values())
        emit(f"RESUME: {cp.completed_hours()}/{request.hours} P50 hours cached; "
             f"{saved}/{request.hours * len(fields)} source scalars retained")
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for index in range(request.hours):
                lead = index + 1
                saved = cp.values.get(str(lead), {})
                missing = [field for field in fields if field not in saved]
                if not missing:
                    emit(f"CACHE_HOUR_OK: {lead}/{request.hours}")
                    continue
                emit(f"DIRECT_HOUR: {lead}/{request.hours}; {len(missing)} values pending download")
                futures = {executor.submit(read_scalar, f, index): f for f in missing}
                errors = []
                # Do not abort as_completed on the first exception: other
                # in-flight fields may already be correct and must be saved.
                for future in as_completed(futures):
                    field = futures[future]
                    try:
                        value = future.result()
                    except Exception as exc:
                        category, can_retry = classify_point_read_error(exc)
                        emit(
                            f"DIRECT_VALUE_ERROR: lead={lead} field={field} "
                            f"category={category} attempt=1/"
                            f"{1 + (EXTRA_POINT_ATTEMPTS if can_retry else 0)}"
                        )
                        errors.append((field, exc, can_retry))
                    else:
                        cp.put(lead, field, value)

                # Retry only failed fields, sequentially. Authentication,
                # missing chunk objects, or bad numerical data are not
                # repaired or replaced by extrapolated values.
                for field, original, can_retry in errors:
                    if not can_retry:
                        raise original
                    for attempt in range(1, EXTRA_POINT_ATTEMPTS + 1):
                        emit(
                            f"DIRECT_RETRY: lead={lead} field={field} "
                            f"attempt={attempt + 1}/{EXTRA_POINT_ATTEMPTS + 1}"
                        )
                        time.sleep(EXTRA_POINT_RETRY_DELAY_SECONDS * attempt)
                        try:
                            value = read_scalar(field, index)
                        except Exception as exc:
                            category, again = classify_point_read_error(exc)
                            emit(
                                f"DIRECT_VALUE_ERROR: lead={lead} field={field} "
                                f"category={category} attempt={attempt + 1}/"
                                f"{EXTRA_POINT_ATTEMPTS + 1}"
                            )
                            if not again or attempt == EXTRA_POINT_ATTEMPTS:
                                raise
                        else:
                            cp.put(lead, field, value)
                            emit(f"DIRECT_RETRY_OK: lead={lead} field={field}")
                            break

                # Only emit OK after every required field is saved.
                emit(f"DIRECT_HOUR_OK: {lead}/{request.hours}")
        # Replace only selected small arrays. Do not recompute/copy global data.
        result = xr.Dataset(coords=subset.coords, attrs=subset.attrs)
        for field in fields:
            source = subset[field]
            data = np.array([
                np.nan if cp.values[str(lead)][field] is None else cp.values[str(lead)][field]
                for lead in range(1, request.hours + 1)
            ], dtype=float).reshape(source.shape)
            result[field] = xr.Variable(source.dims, data, attrs=source.attrs)
        return result
