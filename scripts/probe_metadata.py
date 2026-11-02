"""Authorization probe: GET one real root zarr.json via the ingest GCS client.

Uses the same obstore/ADC path as point_reader (including WIF), not gcloud.
Prints only a fixed category; no forecast values, tokens or identifiers.
"""
from __future__ import annotations

import argparse
import sys

from point_reader import probe_root_metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-prefix", required=True)
    args = parser.parse_args(argv)
    try:
        probe_root_metadata(args.run_prefix)
    except ValueError as exc:
        print(f"WN3_READER_PROBE_BLOCKED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Google/obstore messages can carry account or project identifiers.
        print(f"WN3_READER_PROBE_FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("WN3_READER_ZARR_V3_METADATA_OK (no forecast values read)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
