import argparse
import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from loguru import logger

_ENVELOPE_VERSION = 1
_ALGORITHM = "RSA-OAEP/AES-256-GCM"
_AES_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12


@dataclass(frozen=True)
class Target:
    collection: str
    field: str


TARGETS: Tuple[Target, ...] = (
    Target(collection="AppCreds", field="creds"),
    Target(collection="AppAuth", field="auth"),
)


def _ensure_repo_root_on_path() -> None:
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rotate envelope-encrypted secrets in MongoDB.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write updates to MongoDB. Without this flag, runs in dry-run mode.",
    )
    p.add_argument(
        "--only",
        choices=["all", "app_creds", "app_auth"],
        default="all",
        help="Which collection(s) to rotate.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional cap on number of documents updated per target. 0 = no limit.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Mongo cursor batch size.",
    )
    p.add_argument(
        "--old-public-env",
        default="ENCRYPTION_PUBLIC_KEY_B64",
        help="Env var name holding the OLD public key (b64 PEM).",
    )
    p.add_argument(
        "--old-private-env",
        default="ENCRYPTION_PRIVATE_KEY_B64",
        help="Env var name holding the OLD private key (b64 PEM).",
    )
    p.add_argument(
        "--new-public-env",
        default="ENCRYPTION_PUBLIC_KEY_B64_NEW",
        help="Env var name holding the NEW public key (b64 PEM).",
    )
    p.add_argument(
        "--new-private-env",
        default="ENCRYPTION_PRIVATE_KEY_B64_NEW",
        help="Env var name holding the NEW private key (b64 PEM).",
    )
    p.add_argument(
        "--match-kid",
        default="",
        help="If set, only rotate envelopes with this kid.",
    )
    p.add_argument(
        "--new-kid",
        default="",
        help="If set, write this kid into the new envelopes.",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="Verify by decrypting the newly encrypted envelope with the NEW private key.",
    )
    return p.parse_args()


def _select_targets(only: str) -> Iterable[Target]:
    if only == "app_creds":
        return (TARGETS[0],)
    if only == "app_auth":
        return (TARGETS[1],)
    return TARGETS


def _load_public_key(b64_pem: str) -> rsa.RSAPublicKey:
    return serialization.load_pem_public_key(base64.b64decode(b64_pem))  # type: ignore


def _load_private_key(b64_pem: str) -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(base64.b64decode(b64_pem), password=None)  # type: ignore


def _is_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("_enc") is True


def _serialize(data: Dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _deserialize(data: bytes) -> Dict[str, Any]:
    return json.loads(data.decode("utf-8"))


def _encrypt(*, public_key: rsa.RSAPublicKey, plaintext: Dict[str, Any], kid: str) -> Dict[str, Any]:
    aes_key = AESGCM.generate_key(bit_length=_AES_KEY_BYTES * 8)
    nonce = os.urandom(_GCM_NONCE_BYTES)

    ciphertext = AESGCM(aes_key).encrypt(nonce, _serialize(plaintext), None)

    encrypted_dek = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    return {
        "_enc": True,
        "v": _ENVELOPE_VERSION,
        "alg": _ALGORITHM,
        "kid": kid,
        "dek": base64.b64encode(encrypted_dek).decode(),
        "data": base64.b64encode(ciphertext).decode(),
        "iv": base64.b64encode(nonce).decode(),
    }


def _decrypt(*, private_key: rsa.RSAPrivateKey, envelope: Dict[str, Any]) -> Dict[str, Any]:
    encrypted_dek = base64.b64decode(envelope["dek"])
    nonce = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["data"])

    aes_key = private_key.decrypt(
        encrypted_dek,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    plaintext_bytes = AESGCM(aes_key).decrypt(nonce, ciphertext, None)
    return _deserialize(plaintext_bytes)


async def _rotate_target(
    *,
    db: Any,
    target: Target,
    apply: bool,
    limit: int,
    batch_size: int,
    match_kid: str,
    new_kid: str,
    old_private: rsa.RSAPrivateKey,
    new_public: rsa.RSAPublicKey,
    new_private: rsa.RSAPrivateKey | None,
    verify: bool,
) -> None:
    coll = db[target.collection]

    query = {target.field: {"$type": "object"}, f"{target.field}._enc": True}
    projection = {target.field: 1}

    scanned = 0
    rotated = 0
    errors = 0

    cursor = coll.find(query, projection=projection).batch_size(batch_size)
    async for doc in cursor:
        scanned += 1
        raw = doc.get(target.field)
        if not _is_envelope(raw):
            continue

        if match_kid and str(raw.get("kid") or "") != match_kid:
            continue

        try:
            plaintext = _decrypt(private_key=old_private, envelope=raw)
            envelope_out = _encrypt(public_key=new_public, plaintext=plaintext, kid=new_kid or str(raw.get("kid") or ""))

            if verify:
                if new_private is None:
                    raise RuntimeError("--verify requires NEW private key")
                roundtrip = _decrypt(private_key=new_private, envelope=envelope_out)
                if roundtrip != plaintext:
                    raise RuntimeError("verification failed: decrypted payload mismatch")

            if not apply:
                rotated += 1
            else:
                rs = await coll.update_one(
                    {"_id": doc["_id"]}, {"$set": {target.field: envelope_out}}
                )
                if rs.modified_count == 1:
                    rotated += 1

        except Exception:
            errors += 1

        if rotated and rotated % 500 == 0:
            logger.info(
                f"{target.collection}.{target.field}: scanned={scanned} rotated={rotated} errors={errors}"
            )

        if limit and rotated >= limit:
            break

    logger.info(
        f"{target.collection}.{target.field}: done scanned={scanned} rotated={rotated} errors={errors} apply={apply}"
    )


async def main() -> None:
    _ensure_repo_root_on_path()
    args = _parse_args()

    from src.autobots import SettingsProvider
    from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_db

    _ = SettingsProvider.sget()

    old_public_b64 = os.environ.get(args.old_public_env, "")
    old_private_b64 = os.environ.get(args.old_private_env, "")
    new_public_b64 = os.environ.get(args.new_public_env, "")
    new_private_b64 = os.environ.get(args.new_private_env, "")

    if not old_private_b64:
        raise SystemExit(f"Missing env var: {args.old_private_env}")
    if not new_public_b64:
        raise SystemExit(f"Missing env var: {args.new_public_env}")
    if not new_private_b64 and args.verify:
        raise SystemExit(f"Missing env var: {args.new_private_env} (required for --verify)")

    old_private = _load_private_key(old_private_b64)
    new_public = _load_public_key(new_public_b64)
    new_private = _load_private_key(new_private_b64) if new_private_b64 else None

    if old_public_b64:
        _ = _load_public_key(old_public_b64)

    db = next(get_mongo_db())
    try:
        logger.info(
            f"Starting key rotation: apply={args.apply} only={args.only} limit={args.limit} "
            f"match_kid={args.match_kid or '<any>'} new_kid={args.new_kid or '<preserve>'} verify={args.verify}"
        )
        for target in _select_targets(args.only):
            await _rotate_target(
                db=db,
                target=target,
                apply=args.apply,
                limit=args.limit,
                batch_size=args.batch_size,
                match_kid=(args.match_kid or "").strip(),
                new_kid=(args.new_kid or "").strip(),
                old_private=old_private,
                new_public=new_public,
                new_private=new_private,
                verify=bool(args.verify),
            )
        logger.info("Rotation finished")
    finally:
        close_mongo_client()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
