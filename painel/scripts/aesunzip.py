import sys, struct, zipfile, zlib, os, hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
path, pw, out = sys.argv[1], sys.argv[2].encode(), sys.argv[3]
zf = zipfile.ZipFile(path); raw = open(path,'rb').read()
for zi in zf.infolist():
    if zi.is_dir(): continue
    o = zi.header_offset
    sig, ver, flag, meth, t, d, crc, csz, usz, nl, el = struct.unpack('<IHHHHHIIIHH', raw[o:o+30])
    extra = raw[o+30+nl:o+30+nl+el]
    i=0; strength=3; real=8
    while i < len(extra):
        hid, hl = struct.unpack('<HH', extra[i:i+4])
        if hid == 0x9901: _,_,strength,real = struct.unpack('<HHBH', extra[i+4:i+4+hl])
        i += 4+hl
    kl = {1:16,2:24,3:32}[strength]; sl = {1:8,2:12,3:16}[strength]
    start = o+30+nl+el; csz = zi.compress_size
    blob = raw[start:start+csz]
    salt, pv, data = blob[:sl], blob[sl:sl+2], blob[sl+2:-10]
    dk = hashlib.pbkdf2_hmac('sha1', pw, salt, 1000, 2*kl+2)
    if dk[-2:] != pv: print('bad password', zi.filename); sys.exit(1)
    # AES-CTR little-endian counter starting at 1
    key = dk[:kl]; enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    outb = bytearray(); n = len(data)
    ks = bytearray()
    blocks = (n+15)//16
    ctrs = b''.join(struct.pack('<QQ', c, 0) for c in range(1, blocks+1))
    ks = enc.update(ctrs)
    plain = bytes(a^b for a,b in zip(data, ks))
    if real == 8: plain = zlib.decompress(plain, -15)
    p = os.path.join(out, zi.filename); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p,'wb').write(plain); print('ok', zi.filename, len(plain))
