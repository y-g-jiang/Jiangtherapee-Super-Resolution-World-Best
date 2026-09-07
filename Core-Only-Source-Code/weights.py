"""Load the Controller, RefineNet and normalization weights for a frame count."""

from __future__ import annotations

import hashlib
import json
import operator
from pathlib import Path

import numpy as np


WEIGHT_DIR = Path(__file__).resolve().parent / "weights"


def weight_paths(k: int) -> dict[str, Path]:
    if isinstance(k, (bool, np.bool_)):
        raise ValueError("k must be an integer from 1 to 14")
    k = operator.index(k)
    if not 1 <= k <= 14:
        raise ValueError("k must be an integer from 1 to 14")
    manifest = json.loads((WEIGHT_DIR / "manifest.json").read_text(encoding="utf-8"))
    names = dict(manifest["routes"][str(k)], static_affine=manifest["static_affine"])
    paths = {}
    for role, name in names.items():
        if Path(name).name != name or not name.endswith(".npz"):
            raise ValueError("invalid weight filename")
        path = WEIGHT_DIR / name
        entry = manifest["files"][name]
        if path.stat().st_size != entry["bytes"]:
            raise ValueError(f"weight size mismatch: {name}")
        with path.open("rb") as stream:
            sha256 = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha256.update(chunk)
        if sha256.hexdigest() != entry["sha256"]:
            raise ValueError(f"weight checksum mismatch: {name}")
        paths[role] = path
    return paths


def load_npz(path: str | Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as archive:
        result = {}
        for key in archive.files:
            value = archive[key]
            if value.dtype.kind not in "fi" or not np.isfinite(value).all():
                raise ValueError(f"invalid weight tensor: {key}")
            name = key[4:] if key.startswith("net.") else key
            if name in result:
                raise ValueError(f"duplicate weight tensor: {name}")
            result[name] = np.ascontiguousarray(value, dtype=np.float32)
    return result
