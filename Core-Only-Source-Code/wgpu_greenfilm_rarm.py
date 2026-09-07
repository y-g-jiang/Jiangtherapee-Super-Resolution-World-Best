"""JSR RefineNet: green-guided FiLM and color-difference residuals."""

from __future__ import annotations


import gc


from pathlib import Path


import numpy as np


import wgpu_buf as gb


import wgpu_rarm_tiled as base


FILM_WORKGROUP = 256


MAX_DISPATCH_GROUPS = 65535


FILM_WGSL = r"""
struct P {
  total : u32, mode : u32, row_stride : u32, _p0 : u32,
};
@group(0) @binding(0) var<storage, read_write> hidden : array<f32>;
@group(0) @binding(1) var<storage, read>       modulation : array<f32>;
@group(0) @binding(2) var<uniform>             p : P;

@compute @workgroup_size(256, 1, 1)
fn main(@builtin(global_invocation_id) gid : vec3<u32>) {
  let index = gid.y * p.row_stride + gid.x;
  if (index >= p.total) { return; }
  if (p.mode == 0u) {
    hidden[index] = hidden[index] * (1.0 + 0.1 * tanh(modulation[index]));
  } else {
    hidden[index] = hidden[index] + modulation[index];
  }
}
"""


def film_dispatch_geometry(
    total: int,
    max_groups: int = MAX_DISPATCH_GROUPS,
) -> tuple[tuple[int, int, int], int]:
    total = int(total)
    max_groups = int(max_groups)
    if total < 1:
        raise ValueError("FiLM dispatch total must be positive")
    if max_groups < 1:
        raise ValueError("WGPU max workgroups per dimension must be positive")
    groups = (total + FILM_WORKGROUP - 1) // FILM_WORKGROUP
    groups_x = min(groups, max_groups)
    groups_y = (groups + groups_x - 1) // groups_x
    if groups_y > max_groups:
        raise RuntimeError(
            f"FiLM dispatch needs {groups_x}x{groups_y} workgroups; "
            f"device limit is {max_groups} per dimension"
        )
    return (groups_x, groups_y, 1), groups_x * FILM_WORKGROUP


def load_weights(path: str | Path) -> dict[str, np.ndarray]:
    with np.load(Path(path), allow_pickle=False) as archive:
        weights = {
            name: np.ascontiguousarray(archive[name], np.float32)
            for name in archive.files
        }
    validate_weights(weights)
    return weights


def validate_weights(weights: dict[str, np.ndarray]) -> tuple[int, int]:
    width = int(weights["inp.weight"].shape[0])
    if weights["inp.weight"].shape != (width, 6, 3, 3):
        raise ValueError("invalid GreenFiLM inp.weight")
    if weights["guide.weight"].shape != (width, 2, 3, 3):
        raise ValueError("invalid GreenFiLM guide.weight")
    blocks = 0
    while f"body.{blocks}.c1.weight" in weights:
        for branch in ("body",):
            for conv in ("c1", "c2"):
                prefix = f"{branch}.{blocks}.{conv}"
                if weights[prefix + ".weight"].shape != (width, width, 3, 3):
                    raise ValueError(f"invalid {prefix}.weight")
                if weights[prefix + ".bias"].shape != (width,):
                    raise ValueError(f"invalid {prefix}.bias")
        for branch in ("gammas", "betas"):
            prefix = f"{branch}.{blocks}"
            if weights[prefix + ".weight"].shape != (width, width, 1, 1):
                raise ValueError(f"invalid {prefix}.weight")
            if weights[prefix + ".bias"].shape != (width,):
                raise ValueError(f"invalid {prefix}.bias")
        blocks += 1
    if blocks < 1 or weights["out.weight"].shape != (3, width, 1, 1):
        raise ValueError("invalid GreenFiLM RArm structure")
    expected = {
        "inp.weight", "inp.bias", "guide.weight", "guide.bias",
        "out.weight", "out.bias",
    }
    for block in range(blocks):
        for branch, layers in (
            ("body", ("c1", "c2")),
            ("gammas", (None,)),
            ("betas", (None,)),
        ):
            for layer in layers:
                prefix = f"{branch}.{block}" + (f".{layer}" if layer else "")
                expected.update((prefix + ".weight", prefix + ".bias"))
    if set(weights) != expected:
        missing = sorted(expected - set(weights))
        extra = sorted(set(weights) - expected)
        raise ValueError(f"incomplete GreenFiLM state: missing={missing}, extra={extra}")
    for prefix, channels in (("inp", width), ("guide", width)):
        if weights[prefix + ".bias"].shape != (channels,):
            raise ValueError(f"invalid {prefix}.bias")
    if weights["out.bias"].shape != (3,):
        raise ValueError("invalid out.bias")
    return width, blocks


class GreenFiLMTiledRArm:

    def __init__(self, weights: dict[str, np.ndarray]) -> None:
        import wgpu

        self.weights = weights
        self.width, self.blocks = validate_weights(weights)
        self.device = gb.dev()
        limits = dict(self.device.limits)
        self.max_dispatch_groups = int(
            limits.get("max-compute-workgroups-per-dimension", MAX_DISPATCH_GROUPS)
        )
        required = {
            "max-compute-invocations-per-workgroup": max(
                base.WORKGROUP * base.WORKGROUP, FILM_WORKGROUP
            ),
            "max-compute-workgroup-size-x": FILM_WORKGROUP,
            "max-compute-workgroup-size-y": base.WORKGROUP,
            "max-compute-workgroup-storage-size": (
                2 * base.OUTPUT_TILE * base.INNER_TILE * 4
            ),
        }
        failed = {
            name: {"required": value, "actual": int(limits.get(name, 0))}
            for name, value in required.items()
            if int(limits.get(name, 0)) < value
        }
        if failed:
            raise RuntimeError(f"standard WGPU limits are insufficient: {failed}")
        conv_module = self.device.create_shader_module(code=base.CONV_WGSL)
        film_module = self.device.create_shader_module(code=FILM_WGSL)
        self.conv_pipeline = self.device.create_compute_pipeline(
            layout=wgpu.enums.AutoLayoutMode.auto,
            compute={"module": conv_module, "entry_point": "main"},
        )
        self.film_pipeline = self.device.create_compute_pipeline(
            layout=wgpu.enums.AutoLayoutMode.auto,
            compute={"module": film_module, "entry_point": "main"},
        )
        self.weight_buffers = {
            name: self.device.create_buffer_with_data(
                data=value.reshape(-1).tobytes(), usage=wgpu.BufferUsage.STORAGE
            )
            for name, value in weights.items()
        }
        self._workspaces: dict[tuple[int, int, int], dict[str, object]] = {}
        self._closed = False
        self.contract = {
            "single_wgpu_codepath": True,
            "architecture": "GreenFiLMColorDiffRArm",
            "weight_tensor_count": len(weights),
            "workgroup": [base.WORKGROUP, base.WORKGROUP],
            "film_workgroup": FILM_WORKGROUP,
            "max_dispatch_groups_per_dimension": self.max_dispatch_groups,
            "output_tile": [base.OUTPUT_TILE, base.OUTPUT_TILE],
            "inner_tile": base.INNER_TILE,
            "vendor_branches": 0,
            "backend_branches": 0,
            "required_limits": required,
        }

    def _uniform(self, values: list[int]):
        import wgpu

        data = np.asarray(values, dtype=np.uint32)
        return self.device.create_buffer_with_data(
            data=data.tobytes(), usage=wgpu.BufferUsage.UNIFORM
        )

    @staticmethod
    def _release_workspace(workspace: dict[str, object]) -> None:
        owned = tuple(workspace.get("owned_buffers", ()))
        workspace.clear()
        gc.collect()
        for buffer in owned:
            try:
                buffer.destroy()
            except Exception:
                pass
        gb.poll()

    def release_workspaces(self) -> None:
        while self._workspaces:
            _, workspace = self._workspaces.popitem()
            self._release_workspace(workspace)

    def close(self) -> None:
        if self._closed:
            return
        self.release_workspaces()
        weights = tuple(self.weight_buffers.values())
        self.weight_buffers.clear()
        for buffer in weights:
            try:
                buffer.destroy()
            except Exception:
                pass
        gc.collect()
        gb.poll()
        self._closed = True

    def _workspace(self, batch: int, height: int, width: int) -> dict[str, object]:
        import wgpu

        key = (batch, height, width)
        if key in self._workspaces:
            workspace = self._workspaces.pop(key)
            self._workspaces[key] = workspace
            return workspace
        while len(self._workspaces) >= base.MAX_CACHED_WORKSPACES:
            oldest = next(iter(self._workspaces))
            self._release_workspace(self._workspaces.pop(oldest))
        spatial = height * width
        usage = wgpu.BufferUsage
        max_binding = int(self.device.limits["max-storage-buffer-binding-size"])

        def allocate(elements: int, flags: int):
            size = max(4, elements * 4)
            if size > max_binding:
                raise RuntimeError(
                    f"WGPU buffer {size} exceeds max storage binding {max_binding}"
                )
            return self.device.create_buffer(size=size, usage=flags)

        source = allocate(batch * 6 * spatial, usage.STORAGE | usage.COPY_DST)
        guide_source = allocate(batch * 2 * spatial, usage.STORAGE | usage.COPY_DST)
        main = allocate(batch * self.width * spatial, usage.STORAGE)
        temp = allocate(batch * self.width * spatial, usage.STORAGE)
        guide = allocate(batch * self.width * spatial, usage.STORAGE)
        output = allocate(batch * 3 * spatial, usage.STORAGE | usage.COPY_SRC)
        owned_buffers = [source, guide_source, main, temp, guide, output]
        operations: list[tuple[object, object, tuple[int, int, int]]] = []

        def convolution(
            prefix: str,
            inp: object,
            out: object,
            cin: int,
            cout: int,
            ks: int,
            preact: bool = False,
            residual: bool = False,
        ) -> None:
            inner = cin * ks * ks
            uniform = self._uniform([
                batch, cin, cout, height,
                width, ks, int(preact), int(residual),
                spatial, inner, 0, 0,
            ])
            owned_buffers.append(uniform)
            bind = gb.bind(self.conv_pipeline, [
                inp,
                self.weight_buffers[prefix + ".weight"],
                self.weight_buffers[prefix + ".bias"],
                out,
                uniform,
            ])
            operations.append((
                self.conv_pipeline,
                bind,
                (
                    (spatial + base.OUTPUT_TILE - 1) // base.OUTPUT_TILE,
                    (cout + base.OUTPUT_TILE - 1) // base.OUTPUT_TILE,
                    batch,
                ),
            ))

        def film(mode: int) -> None:
            total = batch * self.width * spatial
            grid, row_stride = film_dispatch_geometry(
                total, self.max_dispatch_groups
            )
            uniform = self._uniform([total, mode, row_stride, 0])
            owned_buffers.append(uniform)
            bind = gb.bind(self.film_pipeline, [main, temp, uniform])
            operations.append((
                self.film_pipeline,
                bind,
                grid,
            ))

        convolution("guide", guide_source, guide, 2, self.width, 3)
        convolution("inp", source, main, 6, self.width, 3)
        for block in range(self.blocks):
            convolution(
                f"body.{block}.c1", main, temp,
                self.width, self.width, 3, preact=True,
            )
            convolution(
                f"body.{block}.c2", temp, main,
                self.width, self.width, 3, preact=True, residual=True,
            )
            convolution(f"gammas.{block}", guide, temp, self.width, self.width, 1)
            film(0)
            convolution(f"betas.{block}", guide, temp, self.width, self.width, 1)
            film(1)
        convolution("out", main, output, self.width, 3, 1)
        workspace = {
            "source": source,
            "guide_source": guide_source,
            "output": output,
            "operations": operations,
            "output_bytes": batch * 3 * spatial * 4,
            "owned_buffers": owned_buffers,
        }
        self._workspaces[key] = workspace
        return workspace

    def forward_residual_batch(self, inputs: np.ndarray) -> np.ndarray:
        if self._closed:
            raise RuntimeError("GreenFiLM RArm executor is closed")
        array = np.ascontiguousarray(inputs, np.float32)
        if array.ndim == 3:
            array = array[None]
        if array.ndim != 4 or array.shape[1] != 6:
            raise ValueError(f"RArm input must be NCHW C=6, got {array.shape}")
        batch, _, height, width = array.shape
        workspace = self._workspace(batch, height, width)
        guide = np.ascontiguousarray(array[:, (1, 4)], np.float32)
        self.device.queue.write_buffer(workspace["source"], 0, array.tobytes())
        self.device.queue.write_buffer(workspace["guide_source"], 0, guide.tobytes())
        for pipeline, bind, grid in workspace["operations"]:
            encoder = self.device.create_command_encoder()
            compute = encoder.begin_compute_pass()
            compute.set_pipeline(pipeline)
            compute.set_bind_group(0, bind)
            compute.dispatch_workgroups(*grid)
            compute.end()
            self.device.queue.submit([encoder.finish()])
        raw = self.device.queue.read_buffer(
            workspace["output"], 0, workspace["output_bytes"]
        )
        return np.frombuffer(raw, np.float32).reshape(batch, 3, height, width).copy()

    def factorized(
        self,
        value: np.ndarray,
        amplitude: np.ndarray,
        codes: tuple[int, ...],
        reference: np.float32 = base.REFERENCE_AMPLITUDE,
    ) -> np.ndarray:
        value = np.ascontiguousarray(value, np.float32)
        amplitude = np.ascontiguousarray(amplitude, np.float32)
        if value.ndim != 3 or value.shape[0] != 6:
            raise ValueError(f"value must be (6,H,W), got {value.shape}")
        if amplitude.shape == value.shape[1:]:
            amplitude = amplitude[None]
        if amplitude.shape != (1, value.shape[1], value.shape[2]):
            raise ValueError(f"amplitude mismatch: {amplitude.shape}")
        if not codes or any(code < 0 or code > 7 for code in codes):
            raise ValueError(f"invalid TTA codes: {codes}")
        if len(set(codes)) != len(codes):
            raise ValueError(f"repeated TTA codes: {codes}")
        total = np.zeros((3, value.shape[1], value.shape[2]), np.float32)
        for code in codes:
            member = base._transform_chw(value, code)
            scale = base._transform_chw(amplitude, code) / reference
            denominator = np.where(scale > 0.0, scale, np.float32(1.0))
            normalized = np.ascontiguousarray(member / denominator, np.float32)
            delta = self.forward_residual_batch(normalized)[0]
            r0, g0, b0 = normalized[0], normalized[1], normalized[2]
            g = g0 + delta[0]
            r = g + (r0 - g0) + delta[1]
            b = g + (b0 - g0) + delta[2]
            restored = scale * np.clip(np.stack((r, g, b), axis=0), 0.0, 1.0)
            total += base._inverse_chw(restored, code)
        return total * np.float32(1.0 / len(codes))


__all__ = [
    "GreenFiLMTiledRArm",
    "FILM_WORKGROUP",
    "MAX_DISPATCH_GROUPS",
    "film_dispatch_geometry",
    "load_weights",
    "validate_weights",
]
