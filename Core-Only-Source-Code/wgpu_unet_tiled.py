"""JSR Controller: 151 input channels, 36 output channels."""

from __future__ import annotations


import numpy as np


import wgpu_buf as gb


import wgpu_net


from wgpu_rarm_tiled import CONV_WGSL, INNER_TILE, OUTPUT_TILE, WORKGROUP


def validate_weights(weights: dict[str, np.ndarray]) -> tuple[int, int, int, int]:
    cin = int(weights["enc.0.0.weight"].shape[1])
    width = int(weights["enc.0.0.weight"].shape[0])
    cout = int(weights["out.weight"].shape[0])
    depth = len([key for key in weights if key.startswith("enc.") and key.endswith(".0.weight")])
    if cin != 151 or cout != 36 or width != 32 or depth != 3 or len(weights) != 30:
        raise ValueError(f"unexpected local-V7 U-Net: cin={cin} cout={cout} width={width} depth={depth}")
    return cin, cout, width, depth


class TiledUNet:

    def __init__(self, weights: dict[str, np.ndarray]) -> None:
        import wgpu

        self.weights = weights
        self.cin, self.cout, self.width, self.depth = validate_weights(weights)
        self.device = gb.dev()
        required = {
            "max-compute-invocations-per-workgroup": WORKGROUP * WORKGROUP,
            "max-compute-workgroup-size-x": WORKGROUP,
            "max-compute-workgroup-size-y": WORKGROUP,
            "max-compute-workgroup-storage-size": 2 * OUTPUT_TILE * INNER_TILE * 4,
        }
        limits = dict(self.device.limits)
        failed = {
            name: {"required": value, "actual": int(limits.get(name, 0))}
            for name, value in required.items()
            if int(limits.get(name, 0)) < value
        }
        if failed:
            raise RuntimeError(f"standard WGPU limits are insufficient: {failed}")
        self.conv_pipeline = self.device.create_compute_pipeline(
            layout=wgpu.enums.AutoLayoutMode.auto,
            compute={"module": self.device.create_shader_module(code=CONV_WGSL), "entry_point": "main"},
        )
        self.pool_pipeline = self._pipeline(wgpu_net.POOL_WGSL)
        self.up_pipeline = self._pipeline(wgpu_net.UP_WGSL)
        self.cat_pipeline = self._pipeline(wgpu_net.CAT_WGSL)
        self.weight_buffers = {
            name: self.device.create_buffer_with_data(
                data=value.reshape(-1).tobytes(), usage=wgpu.BufferUsage.STORAGE
            )
            for name, value in weights.items()
        }
        self.contract = {
            "single_wgpu_codepath": True,
            "shared_tiled_conv_wgsl": True,
            "workgroup": [WORKGROUP, WORKGROUP],
            "output_tile": [OUTPUT_TILE, OUTPUT_TILE],
            "inner_tile": INNER_TILE,
            "vendor_branches": 0,
            "backend_branches": 0,
            "required_limits": required,
        }

    def _pipeline(self, code: str):
        import wgpu

        return self.device.create_compute_pipeline(
            layout=wgpu.enums.AutoLayoutMode.auto,
            compute={"module": self.device.create_shader_module(code=code), "entry_point": "main"},
        )

    def _uniform(self, values: list[int]):
        import wgpu

        return self.device.create_buffer_with_data(
            data=np.asarray(values, np.uint32).tobytes(), usage=wgpu.BufferUsage.UNIFORM
        )

    def _allocate(self, elements: int, copy_src: bool = False):
        import wgpu

        usage = wgpu.BufferUsage.STORAGE | (wgpu.BufferUsage.COPY_SRC if copy_src else 0)
        size = max(4, int(elements) * 4)
        if size > int(self.device.limits["max-storage-buffer-binding-size"]):
            raise RuntimeError(f"WGPU buffer {size} exceeds max storage binding")
        return self.device.create_buffer(size=size, usage=usage)

    def _dispatch(self, pipeline, buffers: list[object], grid: tuple[int, int, int]) -> None:
        bind = gb.bind(pipeline, buffers)
        encoder = self.device.create_command_encoder()
        compute = encoder.begin_compute_pass()
        compute.set_pipeline(pipeline)
        compute.set_bind_group(0, bind)
        compute.dispatch_workgroups(*grid)
        compute.end()
        self.device.queue.submit([encoder.finish()])

    @staticmethod
    def _linear_grid(total: int) -> tuple[int, int, int, int]:
        workgroup = int(wgpu_net.WG)
        groups = (total + workgroup - 1) // workgroup
        gx = min(groups, int(wgpu_net.MAXG))
        gy = (groups + gx - 1) // gx
        return gx, gy, 1, gx * workgroup

    def _conv(self, source, cin: int, height: int, width: int, prefix: str, postact: bool):
        weight = self.weights[prefix + ".weight"]
        cout, ks = int(weight.shape[0]), int(weight.shape[2])
        if weight.shape[1] != cin or weight.shape[3] != ks:
            raise ValueError(f"convolution schema mismatch: {prefix}")
        spatial = height * width
        output = self._allocate(cout * spatial, copy_src=True)
        inner = cin * ks * ks
        uniform = self._uniform([
            1, cin, cout, height, width, ks, 0, 0,
            spatial, inner, int(postact), 0,
        ])
        self._dispatch(
            self.conv_pipeline,
            [source, self.weight_buffers[prefix + ".weight"], self.weight_buffers[prefix + ".bias"], output, uniform],
            ((spatial + OUTPUT_TILE - 1) // OUTPUT_TILE, (cout + OUTPUT_TILE - 1) // OUTPUT_TILE, 1),
        )
        gb.poll()
        return output, cout

    def _pool(self, source, channels: int, height: int, width: int):
        ho, wo = height // 2, width // 2
        total = channels * ho * wo
        output = self._allocate(total)
        gx, gy, gz, stride = self._linear_grid(total)
        uniform = self._uniform([channels, height, width, ho, wo, stride, total, 0])
        self._dispatch(self.pool_pipeline, [source, output, uniform], (gx, gy, gz))
        gb.poll()
        return output, ho, wo

    def _up(self, source, channels: int, height: int, width: int, ho: int, wo: int):
        total = channels * ho * wo
        output = self._allocate(total)
        gx, gy, gz, stride = self._linear_grid(total)
        uniform = self._uniform([channels, height, width, ho, wo, stride, total, 0])
        self._dispatch(self.up_pipeline, [source, output, uniform], (gx, gy, gz))
        gb.poll()
        return output

    def _cat(self, first, second, c1: int, c2: int, height: int, width: int):
        total = (c1 + c2) * height * width
        output = self._allocate(total)
        gx, gy, gz, stride = self._linear_grid(total)
        uniform = self._uniform([c1, c2, height, width, stride, total, 0, 0])
        self._dispatch(self.cat_pipeline, [first, second, output, uniform], (gx, gy, gz))
        gb.poll()
        return output

    def forward_buffer(self, value: np.ndarray) -> gb.GBuf:
        value = np.ascontiguousarray(value, np.float32)
        if value.ndim != 3 or value.shape[0] != self.cin:
            raise ValueError(f"U-Net input must be ({self.cin},H,W), got {value.shape}")
        channels, height, width = value.shape
        current = gb.upload(value).buf
        skips: list[tuple[object, int, int, int]] = []
        for index in range(self.depth):
            current, channels = self._conv(current, channels, height, width, f"enc.{index}.0", True)
            current, channels = self._conv(current, channels, height, width, f"enc.{index}.2", True)
            skips.append((current, channels, height, width))
            current, height, width = self._pool(current, channels, height, width)
        current, channels = self._conv(current, channels, height, width, "mid.0", True)
        current, channels = self._conv(current, channels, height, width, "mid.2", True)
        for index, (skip, skip_channels, skip_h, skip_w) in enumerate(reversed(skips)):
            current = self._up(current, channels, height, width, skip_h, skip_w)
            height, width = skip_h, skip_w
            current = self._cat(current, skip, channels, skip_channels, height, width)
            channels += skip_channels
            current, channels = self._conv(current, channels, height, width, f"dec.{index}.0", True)
            current, channels = self._conv(current, channels, height, width, f"dec.{index}.2", True)
        current, channels = self._conv(current, channels, height, width, "out", False)
        result = gb.GBuf(current, (channels, height, width), np.float32)
        gb.poll()
        return result

    def forward(self, value: np.ndarray) -> np.ndarray:
        result = self.forward_buffer(value)
        host = result.numpy()
        del result
        gb.poll()
        return host
