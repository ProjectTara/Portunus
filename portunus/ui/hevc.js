// Read the picture size from an HEVC decoder configuration record (hvcC).
//
// WebCodecs lets a decoder start without size hints. The hardware HEVC path in
// WebView2 on Windows then assumes 1280x720 and reports frames as
// display 1280x720 with visibleRect 1184x720 even though the stream is
// 1184x2576 - so only the top strip of the phone screen is shown. Passing the
// real size from the SPS (coded size and conformance-window crop) fixes that.

class BitReader {
  constructor(bytes) {
    this.bytes = bytes;
    this.pos = 0; // in bits
  }

  bit() {
    if (this.pos >= this.bytes.length * 8) throw new Error("SPS ended early");
    const byte = this.bytes[this.pos >> 3];
    const value = (byte >> (7 - (this.pos & 7))) & 1;
    this.pos += 1;
    return value;
  }

  bits(n) {
    let value = 0;
    for (let i = 0; i < n; i += 1) value = value * 2 + this.bit();
    return value;
  }

  skip(n) {
    this.pos += n;
  }

  ue() { // unsigned Exp-Golomb
    let zeros = 0;
    while (this.bit() === 0) {
      zeros += 1;
      if (zeros > 31) throw new Error("bad Exp-Golomb code");
    }
    return zeros === 0 ? 0 : (2 ** zeros - 1) + this.bits(zeros);
  }
}

// Remove emulation-prevention bytes (00 00 03 -> 00 00).
function rbsp(nal) {
  const out = [];
  let zeros = 0;
  for (const b of nal) {
    if (zeros >= 2 && b === 3) { zeros = 0; continue; }
    out.push(b);
    zeros = b === 0 ? zeros + 1 : 0;
  }
  return Uint8Array.from(out);
}

function findSps(hvcc) {
  if (hvcc.length < 23) throw new Error("hvcC too short");
  let p = 22;
  const arrays = hvcc[p];
  p += 1;
  for (let a = 0; a < arrays; a += 1) {
    const type = hvcc[p] & 0x3f;
    const count = (hvcc[p + 1] << 8) | hvcc[p + 2];
    p += 3;
    for (let n = 0; n < count; n += 1) {
      const len = (hvcc[p] << 8) | hvcc[p + 1];
      p += 2;
      if (type === 33) return hvcc.subarray(p, p + len); // SPS
      p += len;
    }
  }
  throw new Error("no SPS in hvcC");
}

function skipProfileTierLevel(r, maxSubLayersMinus1) {
  r.skip(96); // general profile (88 bits) + general_level_idc (8 bits)
  const profilePresent = [];
  const levelPresent = [];
  for (let i = 0; i < maxSubLayersMinus1; i += 1) {
    profilePresent.push(r.bit());
    levelPresent.push(r.bit());
  }
  if (maxSubLayersMinus1 > 0) r.skip(2 * (8 - maxSubLayersMinus1));
  for (let i = 0; i < maxSubLayersMinus1; i += 1) {
    if (profilePresent[i]) r.skip(88);
    if (levelPresent[i]) r.skip(8);
  }
}

// Returns { codedWidth, codedHeight, width, height } where width/height are the
// visible picture after the conformance-window crop.
export function hevcSize(hvcc) {
  const r = new BitReader(rbsp(findSps(hvcc)));
  r.skip(16); // NAL unit header
  r.skip(4); // sps_video_parameter_set_id
  const maxSubLayersMinus1 = r.bits(3);
  r.skip(1); // sps_temporal_id_nesting_flag
  skipProfileTierLevel(r, maxSubLayersMinus1);
  r.ue(); // sps_seq_parameter_set_id
  const chroma = r.ue();
  let separatePlanes = 0;
  if (chroma === 3) separatePlanes = r.bit();
  const codedWidth = r.ue();
  const codedHeight = r.ue();
  let left = 0; let right = 0; let top = 0; let bottom = 0;
  if (r.bit()) { // conformance_window_flag
    left = r.ue(); right = r.ue(); top = r.ue(); bottom = r.ue();
  }
  const format = separatePlanes ? 0 : chroma;
  const subW = format === 1 || format === 2 ? 2 : 1;
  const subH = format === 1 ? 2 : 1;
  const width = codedWidth - subW * (left + right);
  const height = codedHeight - subH * (top + bottom);
  if (!(width > 0 && height > 0 && width <= 16384 && height <= 16384)) throw new Error("implausible SPS size");
  return { codedWidth, codedHeight, width, height };
}
