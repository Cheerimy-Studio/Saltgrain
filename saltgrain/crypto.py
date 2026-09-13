"""
Saltgrain (盐粒) cryptographic primitives. Pure Python standard library only.

Contains:
  - sha256d            : Bitcoin's double-SHA256
  - secp256k1          : the same curve Bitcoin uses, affine arithmetic
  - ECDSA sign/verify  : RFC 6979 deterministic nonces, low-s enforced (BIP 62)
  - bech32             : BIP 173 encoding, human-readable part "salt"

Addresses minted before the rename used HRP "rofl" and are still accepted, so
the historical chain keeps replaying and those outputs stay spendable. New
addresses are minted with HRP "salt"; ownership checks compare the 20-byte
payload and ignore which prefix carries it.

Nothing here is constant-time. This chain secures no real value, and the
private keys it generates must never be reused for anything that does.
"""

import hashlib
import hmac

# hashing


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def sha256d(b: bytes) -> bytes:
    """Double SHA-256, as used for Bitcoin block and transaction hashes."""
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def hash_to_int(h: bytes) -> int:
    """Interpret a 32-byte digest as a big-endian integer."""
    return int.from_bytes(h, "big")


# secp256k1

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
A = 0
B = 7
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (GX, GY)

INF = None  # point at infinity


def _inv(x: int, m: int = P) -> int:
    return pow(x, m - 2, m)


def point_add(p, q):
    if p is INF:
        return q
    if q is INF:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return INF
        lam = (3 * x1 * x1 + A) * _inv(2 * y1) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def point_mul(k: int, p=G):
    """Scalar multiplication by double-and-add."""
    if k % N == 0 or p is INF:
        return INF
    if k < 0:
        return point_mul(-k, (p[0], (-p[1]) % P))
    result = INF
    addend = p
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


def on_curve(p) -> bool:
    if p is INF:
        return False
    x, y = p
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - x * x * x - A * x - B) % P == 0


# keys


def privkey_from_bytes(b: bytes) -> int:
    k = int.from_bytes(b, "big") % N
    if k == 0:
        raise ValueError("invalid private key")
    return k


def pubkey(priv: int):
    return point_mul(priv, G)


def ser_pubkey(pub) -> bytes:
    """33-byte SEC1 compressed public key, identical to Bitcoin's format."""
    if pub is INF:
        raise ValueError("cannot serialize point at infinity")
    x, y = pub
    prefix = b"\x03" if y & 1 else b"\x02"
    return prefix + x.to_bytes(32, "big")


def parse_pubkey(b: bytes):
    """Parse a 33-byte compressed public key, recovering y by parity."""
    if len(b) != 33 or b[0] not in (2, 3):
        raise ValueError("bad public key encoding")
    x = int.from_bytes(b[1:], "big")
    if x >= P:
        raise ValueError("public key x out of range")
    alpha = (pow(x, 3, P) + A * x + B) % P
    beta = pow(alpha, (P + 1) // 4, P)
    if (beta * beta - alpha) % P != 0:
        raise ValueError("public key x is not on the curve")
    y = beta if (beta & 1) == (b[0] & 1) else P - beta
    pt = (x, y)
    if not on_curve(pt):
        raise ValueError("public key is not on the curve")
    return pt


# ECDSA with RFC 6979 deterministic nonces


def _rfc6979_k(priv: int, digest: bytes) -> int:
    """Deterministic nonce generation, RFC 6979 section 3.2 with SHA-256."""
    x = priv.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(priv: int, digest: bytes) -> bytes:
    """
    Produce a 64-byte compact signature r||s over a 32-byte digest.

    s is normalised to the lower half of the curve order, as Bitcoin requires
    since BIP 62, so a signature has exactly one valid encoding.
    """
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    z = int.from_bytes(digest, "big")
    while True:
        k = _rfc6979_k(priv, digest)
        pt = point_mul(k, G)
        if pt is INF:
            continue
        r = pt[0] % N
        if r == 0:
            continue
        s = (_inv(k, N) * (z + r * priv)) % N
        if s == 0:
            continue
        if s > N // 2:  # low-s normalisation
            s = N - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify(pub_bytes: bytes, digest: bytes, sig: bytes) -> bool:
    """Verify a 64-byte compact signature. Rejects high-s (malleable) sigs."""
    try:
        if len(sig) != 64 or len(digest) != 32:
            return False
        pub = parse_pubkey(pub_bytes)
        r = int.from_bytes(sig[:32], "big")
        s = int.from_bytes(sig[32:], "big")
        if not (1 <= r < N and 1 <= s < N):
            return False
        if s > N // 2:
            return False
        z = int.from_bytes(digest, "big")
        w = _inv(s, N)
        u1 = (z * w) % N
        u2 = (r * w) % N
        pt = point_add(point_mul(u1, G), point_mul(u2, pub))
        if pt is INF:
            return False
        return pt[0] % N == r
    except (ValueError, TypeError):
        return False


# bech32 (BIP 173)

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
HRP = "salt"
LEGACY_HRPS = ("rofl",)  # addresses minted before the Saltgrain rename


def _bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def bech32_encode(hrp: str, data) -> str:
    combined = list(data) + _bech32_polymod_checksum(hrp, list(data))
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def _bech32_polymod_checksum(hrp, data):
    values = _hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_decode(addr: str):
    if any(ord(c) < 33 or ord(c) > 126 for c in addr):
        return None, None
    if addr.lower() != addr and addr.upper() != addr:
        return None, None
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        return None, None
    hrp = addr[:pos]
    if any(c not in CHARSET for c in addr[pos + 1:]):
        return None, None
    data = [CHARSET.find(c) for c in addr[pos + 1:]]
    if _bech32_polymod(_hrp_expand(hrp) + data) != 1:
        return None, None
    return hrp, data[:-6]


def pubkey_to_address(pub_bytes: bytes) -> str:
    """
    Address = bech32(hrp="salt", version 0, sha256(pubkey)[:20]).

    Bitcoin hashes with RIPEMD160(SHA256(pk)); RIPEMD160 is absent from many
    modern OpenSSL builds, so Saltgrain truncates a single SHA-256 instead. The
    20-byte payload and the encoding are otherwise identical to BIP 173.
    """
    h = sha256(pub_bytes)[:20]
    data = [0] + _convertbits(h, 8, 5)
    return bech32_encode(HRP, data)


def _accepted_hrps():
    return (HRP,) + LEGACY_HRPS


def address_payload(addr: str):
    """The 20 address bytes, or None if `addr` is not a valid address.

    The HRP is deliberately ignored beyond being one we accept: a `rofl1…`
    address and a `salt1…` address can name the same payload, and both are
    valid ways to receive. Spend checks compare payloads.
    """
    hrp, data = bech32_decode(addr)
    if hrp not in _accepted_hrps() or not data or data[0] != 0:
        return None
    decoded = _convertbits(data[1:], 5, 8, False)
    if decoded is None or len(decoded) == 0:
        return None
    return bytes(decoded)


def same_address(a: str, b: str) -> bool:
    """True if two addresses name the same 20-byte payload, prefix aside."""
    pa, pb = address_payload(a), address_payload(b)
    return pa is not None and pa == pb


def address_is_valid(addr: str) -> bool:
    payload = address_payload(addr)
    return payload is not None and len(payload) == 20
