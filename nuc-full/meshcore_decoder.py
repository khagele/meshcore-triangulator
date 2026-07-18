"""
Decoder for raw MeshCore ADVERT packets.

The MeshCore packet format (verified empirically against real packets):

  Offset   Field                                    Notes
  -------  ---------------------------------------  -----------------------------
  byte 0   header                                   ignored — JSON envelope already
                                                    gives packet_type, route_type
  byte 1   path_meta:
              bits 0-5 = num_hops
              bits 6-7 = bytes_per_hop_minus_one    0→1 byte, 1→2 bytes, 2→3 bytes
  bytes 2..(2+num_hops*bytes_per_hop-1)
           inline path bytes (one hop at a time)    same hashes that appear in
                                                    the JSON 'path' / 'path_json'
  +32      pubkey (Ed25519 public key)              big-endian 32 bytes
  + 4      timestamp (Unix epoch)                   little-endian uint32
  +64      signature (Ed25519)                      big-endian 64 bytes
  + 1      flags:
              bits 0-2 = role (1=companion,
                          2=repeater, 3=room,
                          4=sensor)
              bit 4    = hasLocation
              bit 5    = hasFeat1
              bit 6    = hasFeat2
              bit 7    = hasName
  if hasLocation:
  + 4      lat (LE int32 / 1e6)
  + 4      lng (LE int32 / 1e6)
  if hasFeat1:
  + 4      feature 1 (skipped)
  if hasFeat2:
  + 4      feature 2 (skipped)
  if hasName:
  rest     UTF-8 name (no length prefix; runs to end-of-packet)
"""
from __future__ import annotations

ROLE_MAP = {1: "companion", 2: "repeater", 3: "roomserver", 4: "sensor"}


class DecodeError(ValueError):
    pass


def decode_advert(raw: bytes) -> dict:
    """Decode a raw MeshCore ADVERT packet body.

    Returns a dict with: pubkey (lowercase hex), timestamp (int), role (str
    or None), lat (float or None), lng (float or None), name (str or None),
    plus path (list of hex hop strings as they appeared inline in the packet)
    and bytes_per_hop. Raises DecodeError for malformed packets.
    """
    if len(raw) < 105:  # header(1) + meta(1) + min payload(pubkey+ts+sig+flags = 101) + ?
        raise DecodeError(f"packet too short: {len(raw)} bytes")

    path_meta = raw[1]
    num_hops = path_meta & 0x3F
    bytes_per_hop = ((path_meta >> 6) & 0x03) + 1

    pos = 2
    path_total = num_hops * bytes_per_hop
    if len(raw) < pos + path_total + 32 + 4 + 64 + 1:
        raise DecodeError("packet truncated before flags")
    inline_path = []
    for i in range(num_hops):
        h = raw[pos + i * bytes_per_hop : pos + (i + 1) * bytes_per_hop].hex()
        inline_path.append(h)
    pos += path_total

    pubkey = raw[pos : pos + 32].hex()
    pos += 32
    timestamp = int.from_bytes(raw[pos : pos + 4], "little")
    pos += 4
    pos += 64  # signature (we don't verify)
    flags = raw[pos]
    pos += 1

    role = ROLE_MAP.get(flags & 0x07)
    has_location = bool(flags & 0x10)
    has_feat1 = bool(flags & 0x20)
    has_feat2 = bool(flags & 0x40)
    has_name = bool(flags & 0x80)

    lat = lng = None
    if has_location:
        if len(raw) < pos + 8:
            raise DecodeError("packet truncated before location")
        lat_int = int.from_bytes(raw[pos : pos + 4], "little", signed=True)
        lng_int = int.from_bytes(raw[pos + 4 : pos + 8], "little", signed=True)
        lat = lat_int / 1e6
        lng = lng_int / 1e6
        pos += 8

    if has_feat1:
        pos += 4
    if has_feat2:
        pos += 4

    name = None
    if has_name and pos < len(raw):
        name = raw[pos:].decode("utf-8", errors="replace").rstrip("\x00")

    return {
        "pubkey": pubkey,
        "timestamp": timestamp,
        "role": role,
        "lat": lat,
        "lng": lng,
        "name": name,
        "inline_path": inline_path,
        "bytes_per_hop": bytes_per_hop,
        "flags_raw": flags,
    }


def decode_advert_hex(hex_str: str) -> dict:
    """Convenience wrapper — accepts hex string with optional whitespace/0x prefix."""
    s = hex_str.replace("0x", "").replace(" ", "").replace(":", "").lower()
    return decode_advert(bytes.fromhex(s))
