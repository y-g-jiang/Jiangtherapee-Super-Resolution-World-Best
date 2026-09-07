"""Pooling, bilinear resizing and concatenation shaders for the Controller."""

WG = 256            # workgroup 线程数


MAXG = 32768        # 单维 workgroup 上限留量（规范保证 >= 65535）


POOL_WGSL = """
struct P { c : u32, hi : u32, wi : u32, ho : u32, wo : u32,
           stride : u32, total : u32, _pad : u32, };
@group(0) @binding(0) var<storage, read>       inp : array<f32>;
@group(0) @binding(1) var<storage, read_write> outp: array<f32>;
@group(0) @binding(2) var<uniform>             p   : P;

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid : vec3<u32>) {
  let idx : u32 = gid.y * p.stride + gid.x;
  if (idx >= p.total) { return; }
  let ohw : u32 = p.ho * p.wo;
  let c : u32 = idx / ohw;
  let r : u32 = idx % ohw;
  let oy : u32 = r / p.wo;
  let ox : u32 = r % p.wo;
  let b : u32 = c * p.hi * p.wi;
  let y0 : u32 = oy * 2u;
  let x0 : u32 = ox * 2u;
  let s : f32 = inp[b + y0 * p.wi + x0] + inp[b + y0 * p.wi + x0 + 1u]
              + inp[b + (y0 + 1u) * p.wi + x0] + inp[b + (y0 + 1u) * p.wi + x0 + 1u];
  outp[idx] = s * 0.25;
}
"""


UP_WGSL = """
struct P { c : u32, hi : u32, wi : u32, ho : u32, wo : u32,
           stride : u32, total : u32, _pad : u32, };
@group(0) @binding(0) var<storage, read>       inp : array<f32>;
@group(0) @binding(1) var<storage, read_write> outp: array<f32>;
@group(0) @binding(2) var<uniform>             p   : P;

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid : vec3<u32>) {
  let idx : u32 = gid.y * p.stride + gid.x;
  if (idx >= p.total) { return; }
  let ohw : u32 = p.ho * p.wo;
  let c : u32 = idx / ohw;
  let r : u32 = idx % ohw;
  let oy : u32 = r / p.wo;
  let ox : u32 = r % p.wo;

  let sh : f32 = f32(p.hi) / f32(p.ho);
  let sw : f32 = f32(p.wi) / f32(p.wo);
  var sy : f32 = (f32(oy) + 0.5) * sh - 0.5;
  var sx : f32 = (f32(ox) + 0.5) * sw - 0.5;
  sy = max(sy, 0.0);
  sx = max(sx, 0.0);
  let fy : f32 = floor(sy);
  let fx : f32 = floor(sx);
  let ly : f32 = sy - fy;
  let lx : f32 = sx - fx;
  let y0 : u32 = u32(fy);
  let x0 : u32 = u32(fx);
  let y1 : u32 = min(y0 + 1u, p.hi - 1u);
  let x1 : u32 = min(x0 + 1u, p.wi - 1u);

  let b : u32 = c * p.hi * p.wi;
  let v00 : f32 = inp[b + y0 * p.wi + x0];
  let v01 : f32 = inp[b + y0 * p.wi + x1];
  let v10 : f32 = inp[b + y1 * p.wi + x0];
  let v11 : f32 = inp[b + y1 * p.wi + x1];
  let top : f32 = v00 + (v01 - v00) * lx;
  let bot : f32 = v10 + (v11 - v10) * lx;
  outp[idx] = top + (bot - top) * ly;
}
"""


CAT_WGSL = """
struct P { c1 : u32, c2 : u32, h : u32, w : u32,
           stride : u32, total : u32, _p0 : u32, _p1 : u32, };
@group(0) @binding(0) var<storage, read>       a   : array<f32>;
@group(0) @binding(1) var<storage, read>       b   : array<f32>;
@group(0) @binding(2) var<storage, read_write> outp: array<f32>;
@group(0) @binding(3) var<uniform>             p   : P;

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid : vec3<u32>) {
  let idx : u32 = gid.y * p.stride + gid.x;
  if (idx >= p.total) { return; }
  let hw : u32 = p.h * p.w;
  let n1 : u32 = p.c1 * hw;
  if (idx < n1) { outp[idx] = a[idx]; } else { outp[idx] = b[idx - n1]; }
}
"""
