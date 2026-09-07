"""Shared convolution shader and spatial transforms for network inference."""

import numpy as np


WORKGROUP = 8


OUTPUT_TILE = 32


INNER_TILE = 16


REFERENCE_AMPLITUDE = np.float32(0.34523068818006963)


TTA4_CODES = (0, 3, 5, 6)


TTA1_CODES = (0,)


MAX_CACHED_WORKSPACES = 1


CONV_WGSL = r"""
struct P {
  batch : u32, cin : u32, cout : u32, h : u32,
  w : u32, ks : u32, preact : u32, residual : u32,
  spatial : u32, inner : u32, postact : u32, _p0 : u32,
};
@group(0) @binding(0) var<storage, read>       inp  : array<f32>;
@group(0) @binding(1) var<storage, read>       wt   : array<f32>;
@group(0) @binding(2) var<storage, read>       bs   : array<f32>;
@group(0) @binding(3) var<storage, read_write> outp : array<f32>;
@group(0) @binding(4) var<uniform>             p    : P;

var<workgroup> tile_w : array<f32, 512>;
var<workgroup> tile_x : array<f32, 512>;

fn erf_(x : f32) -> f32 {
  let s : f32 = sign(x);
  let a : f32 = abs(x);
  let t : f32 = 1.0 / (1.0 + 0.3275911 * a);
  let y : f32 = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                        - 0.284496736) * t + 0.254829592) * t * exp(-a * a);
  return s * y;
}

fn gelu(x : f32) -> f32 {
  return 0.5 * x * (1.0 + erf_(x * 0.70710678118654752440));
}

fn store_result(bn : u32, oc : u32, sp : u32, value : f32) {
  if (oc < p.cout && sp < p.spatial) {
    let index : u32 = (bn * p.cout + oc) * p.spatial + sp;
    var result : f32 = value;
    if (p.postact == 1u) { result = gelu(result); }
    if (p.residual == 1u) {
      outp[index] = outp[index] + result;
    } else {
      outp[index] = result;
    }
  }
}

@compute @workgroup_size(8, 8, 1)
fn main(
  @builtin(local_invocation_id) lid : vec3<u32>,
  @builtin(workgroup_id) wid : vec3<u32>,
) {
  let bn : u32 = wid.z;
  let lane : u32 = lid.y * 8u + lid.x;
  let oc0 : u32 = wid.y * 32u + lid.y;
  let oc1 : u32 = oc0 + 8u;
  let oc2 : u32 = oc0 + 16u;
  let oc3 : u32 = oc0 + 24u;
  var b0 : f32 = 0.0;
  var b1 : f32 = 0.0;
  var b2 : f32 = 0.0;
  var b3 : f32 = 0.0;
  if (oc0 < p.cout) { b0 = bs[oc0]; }
  if (oc1 < p.cout) { b1 = bs[oc1]; }
  if (oc2 < p.cout) { b2 = bs[oc2]; }
  if (oc3 < p.cout) { b3 = bs[oc3]; }
  var a00 : f32 = b0; var a01 : f32 = b0; var a02 : f32 = b0; var a03 : f32 = b0;
  var a10 : f32 = b1; var a11 : f32 = b1; var a12 : f32 = b1; var a13 : f32 = b1;
  var a20 : f32 = b2; var a21 : f32 = b2; var a22 : f32 = b2; var a23 : f32 = b2;
  var a30 : f32 = b3; var a31 : f32 = b3; var a32 : f32 = b3; var a33 : f32 = b3;

  for (var base : u32 = 0u; base < p.inner; base = base + 16u) {
    for (var index : u32 = lane; index < 512u; index = index + 64u) {
      let row : u32 = index / 16u;
      let q : u32 = index % 16u;
      let oc : u32 = wid.y * 32u + row;
      let wk : u32 = base + q;
      if (oc < p.cout && wk < p.inner) {
        tile_w[index] = wt[oc * p.inner + wk];
      } else {
        tile_w[index] = 0.0;
      }
    }

    for (var index : u32 = lane; index < 512u; index = index + 64u) {
      let q : u32 = index / 32u;
      let column : u32 = index % 32u;
      let xk : u32 = base + q;
      let sp : u32 = wid.x * 32u + column;
      var value : f32 = 0.0;
      if (sp < p.spatial && xk < p.inner) {
        let oy : u32 = sp / p.w;
        let ox : u32 = sp % p.w;
        if (p.ks == 1u) {
          value = inp[(bn * p.cin + xk) * p.spatial + sp];
        } else {
          let ic : u32 = xk / 9u;
          let kr : u32 = xk % 9u;
          let ky : u32 = kr / 3u;
          let kx : u32 = kr % 3u;
          let sy : u32 = oy + ky;
          let sx : u32 = ox + kx;
          if (sy >= 1u && sy <= p.h && sx >= 1u && sx <= p.w) {
            value = inp[(bn * p.cin + ic) * p.spatial + (sy - 1u) * p.w + (sx - 1u)];
          }
        }
        if (p.preact == 1u) { value = gelu(value); }
      }
      tile_x[index] = value;
    }
    workgroupBarrier();

    for (var q : u32 = 0u; q < 16u; q = q + 1u) {
      let w0 : f32 = tile_w[lid.y * 16u + q];
      let w1 : f32 = tile_w[(lid.y + 8u) * 16u + q];
      let w2 : f32 = tile_w[(lid.y + 16u) * 16u + q];
      let w3 : f32 = tile_w[(lid.y + 24u) * 16u + q];
      let x0 : f32 = tile_x[q * 32u + lid.x];
      let x1 : f32 = tile_x[q * 32u + lid.x + 8u];
      let x2 : f32 = tile_x[q * 32u + lid.x + 16u];
      let x3 : f32 = tile_x[q * 32u + lid.x + 24u];
      a00 = a00 + w0 * x0; a01 = a01 + w0 * x1; a02 = a02 + w0 * x2; a03 = a03 + w0 * x3;
      a10 = a10 + w1 * x0; a11 = a11 + w1 * x1; a12 = a12 + w1 * x2; a13 = a13 + w1 * x3;
      a20 = a20 + w2 * x0; a21 = a21 + w2 * x1; a22 = a22 + w2 * x2; a23 = a23 + w2 * x3;
      a30 = a30 + w3 * x0; a31 = a31 + w3 * x1; a32 = a32 + w3 * x2; a33 = a33 + w3 * x3;
    }
    workgroupBarrier();
  }

  let sp0 : u32 = wid.x * 32u + lid.x;
  let sp1 : u32 = sp0 + 8u;
  let sp2 : u32 = sp0 + 16u;
  let sp3 : u32 = sp0 + 24u;
  store_result(bn, oc0, sp0, a00); store_result(bn, oc0, sp1, a01);
  store_result(bn, oc0, sp2, a02); store_result(bn, oc0, sp3, a03);
  store_result(bn, oc1, sp0, a10); store_result(bn, oc1, sp1, a11);
  store_result(bn, oc1, sp2, a12); store_result(bn, oc1, sp3, a13);
  store_result(bn, oc2, sp0, a20); store_result(bn, oc2, sp1, a21);
  store_result(bn, oc2, sp2, a22); store_result(bn, oc2, sp3, a23);
  store_result(bn, oc3, sp0, a30); store_result(bn, oc3, sp1, a31);
  store_result(bn, oc3, sp2, a32); store_result(bn, oc3, sp3, a33);
}
"""


def _transform_chw(value: np.ndarray, code: int) -> np.ndarray:
    result = value
    if code & 1:
        result = np.flip(result, axis=-1)
    if code & 2:
        result = np.flip(result, axis=-2)
    if code & 4:
        result = np.swapaxes(result, -1, -2)
    return np.ascontiguousarray(result, np.float32)


def _inverse_chw(value: np.ndarray, code: int) -> np.ndarray:
    result = value
    if code & 4:
        result = np.swapaxes(result, -1, -2)
    if code & 2:
        result = np.flip(result, axis=-2)
    if code & 1:
        result = np.flip(result, axis=-1)
    return np.ascontiguousarray(result, np.float32)
