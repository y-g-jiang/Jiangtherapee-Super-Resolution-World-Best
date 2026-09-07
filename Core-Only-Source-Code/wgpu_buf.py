"""Shared WGPU device and tensor buffers."""

import numpy as np


_STATE = {}


def dev():
    if "d" not in _STATE:
        import wgpu
        ad = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
        _STATE["info"] = dict(ad.info)
        _STATE["d"] = ad.request_device_sync()
    return _STATE["d"]


def info():
    dev()
    return _STATE.get("info", {})


def set_device(d, adapter_info=None):
    _STATE.setdefault("d", d)
    if adapter_info is not None:
        _STATE.setdefault("info", adapter_info)
    return _STATE["d"]


def usage(*names):
    import wgpu
    U = wgpu.BufferUsage
    f = 0
    for n in names:
        f |= getattr(U, n)
    return f


class GBuf:

    __slots__ = ("buf", "shape", "dtype")

    def __init__(self, buf, shape, dtype=np.float32):
        self.buf = buf
        self.shape = tuple(int(s) for s in shape)
        self.dtype = np.dtype(dtype)

    @property
    def size(self):
        n = 1
        for s in self.shape:
            n *= s
        return n

    @property
    def nbytes(self):
        return self.size * self.dtype.itemsize

    @property
    def ndim(self):
        return len(self.shape)

    def reshape(self, *shape):
        if len(shape) == 1 and isinstance(shape[0], (tuple, list)):
            shape = tuple(shape[0])
        shape = tuple(int(s) for s in shape)
        n = 1
        for s in shape:
            n *= s
        if n != self.size:
            raise ValueError("reshape %s 与元素数 %d 不符" % (shape, self.size))
        return GBuf(self.buf, shape, self.dtype)

    def view(self, dtype):
        return GBuf(self.buf, self.shape, dtype)

    def numpy(self):
        raw = dev().queue.read_buffer(self.buf, 0, self.nbytes)
        return np.frombuffer(raw, self.dtype).reshape(self.shape).copy()

    def destroy(self):
        try:
            self.buf.destroy()
        except Exception:
            pass

    def __repr__(self):
        return "GBuf%s %s %.1f MB" % (self.shape, self.dtype.name, self.nbytes / 1e6)


def is_gbuf(x):
    return isinstance(x, GBuf)


def upload(arr, dtype=None, copy_src=True):
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    a = np.ascontiguousarray(arr, dtype if dtype is not None else None)
    u = usage("STORAGE", "COPY_SRC") if copy_src else usage("STORAGE")
    b = dev().create_buffer_with_data(data=a.reshape(-1).tobytes(), usage=u)
    return GBuf(b, a.shape, a.dtype)


def bind(pipe, bufs, group=0):
    d = dev()
    ent = []
    for i, b in enumerate(bufs):
        bb = b.buf if is_gbuf(b) else b
        ent.append({"binding": i,
                    "resource": {"buffer": bb, "offset": 0, "size": bb.size}})
    return d.create_bind_group(layout=pipe.get_bind_group_layout(group), entries=ent)


def poll():
    device = dev()
    poll_device = getattr(device, "_poll", None)
    if callable(poll_device):
        poll_device(False)
    else:
        # Compatibility for older wgpu-py releases without GPUDevice._poll.
        device.queue.submit([])
