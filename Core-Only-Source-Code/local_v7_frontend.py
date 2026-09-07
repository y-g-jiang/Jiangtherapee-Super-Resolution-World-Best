"""Local intensity normalization of already-aligned reconstruction features."""

from __future__ import annotations


from pathlib import Path


import cv2


import numpy as np


FEATURE_DIM = 151


def _gaussian_kernel(radius: int, sigma: float) -> np.ndarray:
    coordinate = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(np.float32(-0.5) * (coordinate / np.float32(sigma)) ** 2).astype(np.float32)
    return np.ascontiguousarray(kernel / kernel.sum(dtype=np.float32), np.float32)


def _separable_reflect(value: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    return cv2.sepFilter2D(
        np.ascontiguousarray(value, np.float32),
        cv2.CV_32F,
        kernel,
        kernel,
        borderType=cv2.BORDER_REFLECT_101,
    )


def local_amplitude(legacy: np.ndarray) -> np.ndarray:
    legacy = np.asarray(legacy, dtype=np.float32)
    if legacy.ndim != 3 or legacy.shape[2] != 3:
        raise ValueError(f"legacy must be HWC RGB, got {legacy.shape}")
    energy = np.mean(legacy * legacy, axis=2, dtype=np.float32)
    amplitude = np.sqrt(np.maximum(_separable_reflect(energy, _gaussian_kernel(16, 4.0)), 0.0))
    return np.ascontiguousarray(amplitude[None], np.float32)


def finish_feature(phase: np.ndarray, legacy: np.ndarray) -> np.ndarray:
    legacy = np.asarray(legacy, dtype=np.float32)
    height, width = legacy.shape[:2]
    phase = np.asarray(phase, dtype=np.float32)
    reuse_phase_storage = phase.shape == (FEATURE_DIM, height, width)
    if phase.shape == (height * width, 144):
        phase = phase.T.reshape(144, height, width)
    if (
        phase.shape not in ((144, height, width), (FEATURE_DIM, height, width))
        or legacy.shape != (height, width, 3)
    ):
        raise ValueError(f"feature input mismatch: phase={phase.shape}, legacy={legacy.shape}")
    leg = np.ascontiguousarray(legacy.transpose(2, 0, 1), np.float32)
    grey = np.ascontiguousarray(
        np.float32(0.299) * leg[0] + np.float32(0.587) * leg[1] + np.float32(0.114) * leg[2],
        np.float32,
    )
    sobel = np.asarray(
        [[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]],
        dtype=np.float32,
    )
    gx = cv2.filter2D(grey, cv2.CV_32F, sobel, borderType=cv2.BORDER_REFLECT_101)
    gy = cv2.filter2D(grey, cv2.CV_32F, sobel.T, borderType=cv2.BORDER_REFLECT_101)
    kernel = _gaussian_kernel(8, 2.0)
    if reuse_phase_storage:
        feature = phase
        feature[144:147] = leg
        feature[147] = _separable_reflect(gx * gx, kernel)
        feature[148] = _separable_reflect(gx * gy, kernel)
        feature[149] = _separable_reflect(gy * gy, kernel)
        feature[150].fill(0.0)
    else:
        structure = np.stack(
            (
                _separable_reflect(gx * gx, kernel),
                _separable_reflect(gx * gy, kernel),
                _separable_reflect(gy * gy, kernel),
            ),
            axis=0,
        )
        noise = np.zeros((1, height, width), dtype=np.float32)
        feature = np.concatenate((phase, leg, structure, noise), axis=0)
    if feature.shape != (FEATURE_DIM, height, width) or not np.isfinite(feature).all():
        raise RuntimeError("constructed local-V7 feature is invalid")
    return np.ascontiguousarray(feature, np.float32)


def canonicalize(raw_feature: np.ndarray, legacy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    raw_feature = np.asarray(raw_feature, dtype=np.float32)
    if raw_feature.ndim != 3 or raw_feature.shape[0] != FEATURE_DIM:
        raise ValueError(f"raw feature must be {FEATURE_DIM},H,W; got {raw_feature.shape}")
    amplitude = local_amplitude(legacy)
    denominator = np.where(amplitude > 0.0, amplitude, np.float32(1.0))
    degree = np.zeros((FEATURE_DIM, 1, 1), dtype=np.float32)
    for color in range(3):
        base = color * 48
        degree[base + 16 : base + 48] = 1.0
    degree[144:147] = 1.0
    degree[147:150] = 2.0
    degree[150] = 1.0
    canonical = raw_feature / np.power(denominator, degree, dtype=np.float32)
    return np.ascontiguousarray(canonical, np.float32), amplitude


def static_affine(value: np.ndarray, affine_path: Path) -> np.ndarray:
    with np.load(affine_path, allow_pickle=False) as archive:
        mean = np.ascontiguousarray(archive["mean"], np.float32)
        std = np.ascontiguousarray(archive["std"], np.float32)
        ids = tuple(int(item) for item in archive["calibration_ids"].tolist())
    if ids != tuple(range(16)) or mean.shape != (1, FEATURE_DIM, 1, 1) or std.shape != mean.shape:
        raise RuntimeError("frozen static-affine schema drifted")
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std <= 0.0):
        raise RuntimeError("frozen static-affine values are invalid")
    result = (np.asarray(value, np.float32)[None] - mean) / std
    return np.ascontiguousarray(result[0], np.float32)


def load_static_affine(affine_path: Path) -> tuple[np.ndarray, np.ndarray]:
    with np.load(affine_path, allow_pickle=False) as archive:
        mean = np.ascontiguousarray(archive["mean"], np.float32)
        std = np.ascontiguousarray(archive["std"], np.float32)
        ids = tuple(int(item) for item in archive["calibration_ids"].tolist())
    if ids != tuple(range(16)) or mean.shape != (1, FEATURE_DIM, 1, 1) or std.shape != mean.shape:
        raise RuntimeError("frozen static-affine schema drifted")
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std <= 0.0):
        raise RuntimeError("frozen static-affine values are invalid")
    return mean[0], std[0]


class CachedLocalV7Frontend:

    def __init__(self, affine_path: Path) -> None:
        self.mean, self.std = load_static_affine(affine_path)

    def make_controller_input(
        self, phase: np.ndarray, legacy: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        feature = finish_feature(phase, legacy)
        amplitude = local_amplitude(legacy)
        denominator = np.where(amplitude > 0.0, amplitude, np.float32(1.0))

        # The frozen degree vector contains only 0, 1, and 2. Contiguous
        # slices avoid broadcasting a 151-channel exponent tensor.
        for start, stop in ((16, 48), (64, 96), (112, 147), (150, 151)):
            np.divide(feature[start:stop], denominator, out=feature[start:stop])
        denominator_squared = np.square(denominator, dtype=np.float32)
        np.divide(feature[147:150], denominator_squared, out=feature[147:150])

        np.subtract(feature, self.mean, out=feature)
        np.divide(feature, self.std, out=feature)
        if not np.isfinite(feature).all():
            raise RuntimeError("cached local-V7 frontend produced NaN/Inf")
        return feature, amplitude


def make_controller_input(phase: np.ndarray, legacy: np.ndarray, affine_path: Path) -> tuple[np.ndarray, np.ndarray]:
    feature = finish_feature(phase, legacy)
    canonical, amplitude = canonicalize(feature, legacy)
    return static_affine(canonical, affine_path), amplitude
