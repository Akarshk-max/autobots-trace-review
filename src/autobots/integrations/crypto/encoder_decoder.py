import base64
import json
import os
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENVELOPE_VERSION = 1
_ALGORITHM = "RSA-OAEP/AES-256-GCM"
_KEY_ID = "key-1"
_AES_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12

_public_key: rsa.RSAPublicKey | None = None
_private_key: rsa.RSAPrivateKey | None = None


def init_crypto_keys(public_key_b64: str, private_key_b64: str) -> None:
    global _public_key, _private_key
    _public_key = serialization.load_pem_public_key(base64.b64decode(public_key_b64))
    _private_key = serialization.load_pem_private_key(base64.b64decode(private_key_b64), password=None)


def is_encrypted(data: dict) -> bool:
    return isinstance(data, dict) and data.get("_enc") is True


def encrypt(plaintext: dict) -> dict:
    if _public_key is None:
        raise RuntimeError("crypto not initialized: call init_keys first")

    aes_key = AESGCM.generate_key(bit_length=_AES_KEY_BYTES * 8)
    nonce = os.urandom(_GCM_NONCE_BYTES)
    plaintext_bytes = _serialize(plaintext)

    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext_bytes, None)

    encrypted_dek = _public_key.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )

    return {
        "_enc": True,
        "v": _ENVELOPE_VERSION,
        "alg": _ALGORITHM,
        "kid": _KEY_ID,
        "dek": base64.b64encode(encrypted_dek).decode(),
        "data": base64.b64encode(ciphertext).decode(),
        "iv": base64.b64encode(nonce).decode(),
    }


def decrypt(envelope: dict) -> dict:
    if _private_key is None:
        raise RuntimeError("crypto not initialized: call init_keys first")

    encrypted_dek = base64.b64decode(envelope["dek"])
    nonce = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["data"])

    aes_key = _private_key.decrypt(
        encrypted_dek,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )

    aesgcm = AESGCM(aes_key)
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, None)
    return _deserialize(plaintext_bytes)

def _serialize(data: dict) -> bytes:

    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
    try:
        serialize_obj = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=default).encode("utf-8")
    except Exception as e:
        raise ValueError(f"Error serializing data: {e!s}")
    return serialize_obj

def _deserialize(data: bytes) -> dict:
    try:
        deserialize_obj = json.loads(data.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Error deserializing data: {e!s}")
    return deserialize_obj
