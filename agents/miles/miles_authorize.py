#!/usr/bin/env python3
"""Create and verify passive Miles conversational authorization tokens.

This tool intentionally keeps Greg's Ed25519 private key outside GitHub,
ChatGPT, and AI-accessible workspaces.  It uses the system OpenSSL executable
instead of implementing cryptography itself or requiring a Python package.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


VERSION = 1
PREFIX = "MILES1"
SUBJECT = "gregkrsak"
MODE = "greg-trident"
MAX_VALIDITY_SECONDS = 7 * 24 * 60 * 60
DEFAULT_VALIDITY_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
MAX_BLOCK_BYTES = 2048
NONCE_BYTES = 16
RAW_PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64
PUBLIC_KEY_PLACEHOLDER = (
    "<PENDING_ED25519_PUBLIC_KEY_32_BYTES_BASE64URL_NO_PADDING>"
)
SPKI_ED25519_PREFIX = bytes.fromhex("302a300506032b6570032100")
BEGIN = "-----BEGIN MILES AUTHORIZATION-----"
END = "-----END MILES AUTHORIZATION-----"
PUBLIC_KEY_LINE_RE = re.compile(
    r"^public-key-base64url: ([A-Za-z0-9_-]+)$", re.MULTILINE
)
TOKEN_RE = re.compile(
    rf"\A{re.escape(BEGIN)}\r?\n"
    r"(MILES1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\r?\n"
    rf"{re.escape(END)}\r?\n?\Z"
)


class AuthorizationError(Exception):
    """A deterministic authorization rejection."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str, expected_bytes: int | None = None) -> bytes:
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise AuthorizationError("malformed base64url")
    padding = "=" * ((4 - len(text) % 4) % 4)
    try:
        value = base64.b64decode(
            text + padding, altchars=b"-_", validate=True
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise AuthorizationError("malformed base64url") from exc
    if expected_bytes is not None and len(value) != expected_bytes:
        raise AuthorizationError("incorrect decoded length")
    if b64url_encode(value) != text:
        raise AuthorizationError("non-canonical base64url")
    return value


def require_openssl() -> str:
    executable = shutil.which("openssl")
    if executable is None:
        raise AuthorizationError("OpenSSL is not installed or not on PATH")
    probe = subprocess.run(
        [executable, "version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        raise AuthorizationError("OpenSSL could not be executed")
    return executable


def run_checked(args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        args,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise AuthorizationError(detail or f"command failed: {args[0]}")
    return result.stdout


def public_raw_from_pem(public_key_path: Path) -> bytes:
    openssl = require_openssl()
    der = run_checked(
        [
            openssl,
            "pkey",
            "-pubin",
            "-in",
            str(public_key_path),
            "-outform",
            "DER",
        ]
    )
    if not der.startswith(SPKI_ED25519_PREFIX):
        raise AuthorizationError("public key is not an Ed25519 SubjectPublicKeyInfo key")
    raw = der[len(SPKI_ED25519_PREFIX) :]
    if len(raw) != RAW_PUBLIC_KEY_BYTES:
        raise AuthorizationError("public key has an unexpected length")
    return raw


def public_fingerprint(raw: bytes) -> str:
    return "SHA256:" + b64url_encode(hashlib.sha256(raw).digest())


def safe_write(path: Path, data: str, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def generate_keypair(key_dir: Path) -> tuple[Path, Path, bytes]:
    openssl = require_openssl()
    key_dir = key_dir.expanduser().resolve()
    if key_dir.exists() and any(key_dir.iterdir()):
        raise AuthorizationError(f"refusing to overwrite nonempty key directory: {key_dir}")
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(key_dir, 0o700)
    private_key = key_dir / "miles_ed25519_private.pem"
    public_key = key_dir / "miles_ed25519_public.pem"

    old_umask = os.umask(0o077)
    try:
        run_checked(
            [
                openssl,
                "genpkey",
                "-algorithm",
                "ED25519",
                "-out",
                str(private_key),
            ]
        )
        run_checked(
            [
                openssl,
                "pkey",
                "-in",
                str(private_key),
                "-pubout",
                "-out",
                str(public_key),
            ]
        )
    finally:
        os.umask(old_umask)

    os.chmod(private_key, stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(public_key, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    raw = public_raw_from_pem(public_key)
    metadata = {
        "algorithm": "Ed25519",
        "created_at": int(time.time()),
        "mode": MODE,
        "public_key_base64url": b64url_encode(raw),
        "public_key_fingerprint": public_fingerprint(raw),
        "subject": SUBJECT,
        "version": VERSION,
    }
    safe_write(
        key_dir / "miles_ed25519_public.json",
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        0o644,
    )
    return private_key, public_key, raw


def render_charter(template: Path, output: Path, raw_public_key: bytes) -> None:
    source = template.read_text(encoding="utf-8")
    count = source.count(PUBLIC_KEY_PLACEHOLDER)
    if count != 1:
        raise AuthorizationError(
            f"charter template must contain the public-key placeholder exactly once; found {count}"
        )
    rendered = source.replace(PUBLIC_KEY_PLACEHOLDER, b64url_encode(raw_public_key))
    if output.exists():
        raise AuthorizationError(f"refusing to overwrite charter output: {output}")
    safe_write(output, rendered, 0o644)


def canonical_payload(issued_at: int, expires_at: int, nonce: bytes) -> bytes:
    payload = {
        "exp": expires_at,
        "iat": issued_at,
        "mode": MODE,
        "nonce": b64url_encode(nonce),
        "sub": SUBJECT,
        "v": VERSION,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_message(private_key: Path, message: bytes) -> bytes:
    openssl = require_openssl()
    with tempfile.TemporaryDirectory(prefix="miles-auth-sign-") as temp:
        root = Path(temp)
        message_path = root / "message.bin"
        signature_path = root / "signature.bin"
        message_path.write_bytes(message)
        run_checked(
            [
                openssl,
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key),
                "-in",
                str(message_path),
                "-out",
                str(signature_path),
            ]
        )
        signature = signature_path.read_bytes()
    if len(signature) != SIGNATURE_BYTES:
        raise AuthorizationError("OpenSSL produced an unexpected signature length")
    return signature


def issue_token(private_key: Path, validity_seconds: int, now: int | None = None) -> str:
    if validity_seconds <= 0 or validity_seconds > MAX_VALIDITY_SECONDS:
        raise AuthorizationError("validity must be between 1 second and 7 days")
    if not private_key.is_file():
        raise AuthorizationError(f"private key not found: {private_key}")
    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + validity_seconds
    payload_segment = b64url_encode(
        canonical_payload(issued_at, expires_at, secrets.token_bytes(NONCE_BYTES))
    )
    signed = f"{PREFIX}.{payload_segment}".encode("ascii")
    signature = sign_message(private_key, signed)
    token = f"{signed.decode('ascii')}.{b64url_encode(signature)}"
    block = f"{BEGIN}\n{token}\n{END}\n"
    if len(block.encode("utf-8")) > MAX_BLOCK_BYTES:
        raise AuthorizationError("generated authorization block exceeds maximum size")
    return block


def raw_public_from_charter(charter: Path) -> bytes:
    source = charter.read_text(encoding="utf-8")
    matches = PUBLIC_KEY_LINE_RE.findall(source)
    if len(matches) != 1:
        raise AuthorizationError("charter must contain exactly one concrete public key")
    return b64url_decode(matches[0], RAW_PUBLIC_KEY_BYTES)


def parse_token_block(block: str) -> tuple[bytes, bytes]:
    encoded = block.encode("utf-8")
    if len(encoded) > MAX_BLOCK_BYTES:
        raise AuthorizationError("authorization block is too large")
    match = TOKEN_RE.fullmatch(block)
    if match is None:
        raise AuthorizationError("authorization block has an invalid envelope")
    token_line = match.group(1)
    parts = token_line.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise AuthorizationError("authorization token has an invalid version prefix")
    payload_segment, signature_segment = parts[1], parts[2]
    payload = b64url_decode(payload_segment)
    signature = b64url_decode(signature_segment, SIGNATURE_BYTES)
    return f"{PREFIX}.{payload_segment}".encode("ascii"), signature


def verify_signature(raw_public_key: bytes, message: bytes, signature: bytes) -> bool:
    openssl = require_openssl()
    if len(raw_public_key) != RAW_PUBLIC_KEY_BYTES:
        raise AuthorizationError("public key has an incorrect length")
    with tempfile.TemporaryDirectory(prefix="miles-auth-verify-") as temp:
        root = Path(temp)
        public_der = root / "public.der"
        public_pem = root / "public.pem"
        message_path = root / "message.bin"
        signature_path = root / "signature.bin"
        public_der.write_bytes(SPKI_ED25519_PREFIX + raw_public_key)
        message_path.write_bytes(message)
        signature_path.write_bytes(signature)
        run_checked(
            [
                openssl,
                "pkey",
                "-pubin",
                "-inform",
                "DER",
                "-in",
                str(public_der),
                "-out",
                str(public_pem),
            ]
        )
        result = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_pem),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorizationError("payload contains a duplicate JSON key")
        result[key] = value
    return result


def validate_payload(payload_bytes: bytes, now: int) -> dict[str, Any]:
    try:
        payload_text = payload_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AuthorizationError("payload is not valid UTF-8") from exc
    try:
        payload = json.loads(payload_text, object_pairs_hook=strict_object)
    except AuthorizationError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise AuthorizationError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthorizationError("payload must be a JSON object")
    required = {"v", "sub", "mode", "iat", "exp", "nonce"}
    if set(payload) != required:
        raise AuthorizationError("payload fields do not match the version-one schema")
    if type(payload["v"]) is not int or payload["v"] != VERSION:
        raise AuthorizationError("payload version is invalid")
    if payload["sub"] != SUBJECT or type(payload["sub"]) is not str:
        raise AuthorizationError("payload subject is invalid")
    if payload["mode"] != MODE or type(payload["mode"]) is not str:
        raise AuthorizationError("payload mode is invalid")
    if type(payload["iat"]) is not int or type(payload["exp"]) is not int:
        raise AuthorizationError("payload timestamps must be integers")
    issued_at = payload["iat"]
    expires_at = payload["exp"]
    if expires_at <= issued_at:
        raise AuthorizationError("payload expiration must follow issuance")
    if expires_at - issued_at > MAX_VALIDITY_SECONDS:
        raise AuthorizationError("payload validity exceeds seven days")
    if issued_at > now + MAX_FUTURE_SKEW_SECONDS:
        raise AuthorizationError("payload issuance is too far in the future")
    if expires_at <= now:
        raise AuthorizationError("authorization token has expired")
    if type(payload["nonce"]) is not str:
        raise AuthorizationError("payload nonce must be a string")
    b64url_decode(payload["nonce"], NONCE_BYTES)
    return payload


def verify_token(block: str, raw_public_key: bytes, now: int | None = None) -> dict[str, Any]:
    message, signature = parse_token_block(block)
    if not verify_signature(raw_public_key, message, signature):
        raise AuthorizationError("Ed25519 signature verification failed")
    payload_segment = message.decode("ascii").split(".", 1)[1]
    payload_bytes = b64url_decode(payload_segment)
    payload = validate_payload(payload_bytes, int(time.time()) if now is None else now)
    return {
        "valid": True,
        "v": payload["v"],
        "sub": payload["sub"],
        "mode": payload["mode"],
        "iat": payload["iat"],
        "exp": payload["exp"],
        "public_key_fingerprint": public_fingerprint(raw_public_key),
    }


def key_paths(key_dir: Path) -> tuple[Path, Path]:
    root = key_dir.expanduser().resolve()
    return root / "miles_ed25519_private.pem", root / "miles_ed25519_public.pem"


def command_init(args: argparse.Namespace) -> int:
    private_key, public_key, raw = generate_keypair(args.key_dir)
    if args.charter_template is not None or args.charter_output is not None:
        if args.charter_template is None or args.charter_output is None:
            raise AuthorizationError(
                "--charter-template and --charter-output must be supplied together"
            )
        render_charter(args.charter_template, args.charter_output, raw)
    result = {
        "algorithm": "Ed25519",
        "private_key": str(private_key),
        "public_key": str(public_key),
        "public_key_base64url": b64url_encode(raw),
        "public_key_fingerprint": public_fingerprint(raw),
    }
    if args.charter_output is not None:
        result["rendered_charter"] = str(args.charter_output.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_render(args: argparse.Namespace) -> int:
    _, public_key = key_paths(args.key_dir)
    raw = public_raw_from_pem(public_key)
    render_charter(args.charter_template, args.charter_output, raw)
    print(
        json.dumps(
            {
                "rendered_charter": str(args.charter_output.resolve()),
                "public_key_base64url": b64url_encode(raw),
                "public_key_fingerprint": public_fingerprint(raw),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_issue(args: argparse.Namespace) -> int:
    private_key, _ = key_paths(args.key_dir)
    if args.valid_seconds is not None:
        validity = args.valid_seconds
    else:
        validity = args.valid_hours * 60 * 60
    block = issue_token(private_key, validity)
    if args.output is None:
        sys.stdout.write(block)
    else:
        output = args.output.expanduser().resolve()
        if output.exists():
            raise AuthorizationError(f"refusing to overwrite token output: {output}")
        safe_write(output, block, 0o600)
        print(str(output))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    if (args.charter is None) == (args.public_key_base64url is None):
        raise AuthorizationError(
            "supply exactly one of --charter or --public-key-base64url"
        )
    raw = (
        raw_public_from_charter(args.charter)
        if args.charter is not None
        else b64url_decode(args.public_key_base64url, RAW_PUBLIC_KEY_BYTES)
    )
    block = (
        args.token_file.read_text(encoding="utf-8")
        if args.token_file is not None
        else sys.stdin.read(MAX_BLOCK_BYTES + 1)
    )
    result = verify_token(block, raw)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_self_test(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="miles-auth-selftest-") as temp:
        root = Path(temp)
        key_dir = root / "keys"
        template = root / "CHARTER.template.md"
        charter = root / "CHARTER.md"
        template.write_text(
            "algorithm: Ed25519\n"
            f"public-key-base64url: {PUBLIC_KEY_PLACEHOLDER}\n",
            encoding="utf-8",
        )
        private_key, _, raw = generate_keypair(key_dir)
        render_charter(template, charter, raw)
        now = int(time.time())
        block = issue_token(private_key, 60, now=now)
        result = verify_token(block, raw, now=now)
        assert result["valid"] is True
        assert result["sub"] == SUBJECT
        assert result["mode"] == MODE
        assert raw_public_from_charter(charter) == raw

        token_line = block.splitlines()[1]
        replacement = "A" if token_line[-1] != "A" else "B"
        tampered = f"{BEGIN}\n{token_line[:-1]}{replacement}\n{END}\n"
        try:
            verify_token(tampered, raw, now=now)
        except AuthorizationError:
            pass
        else:
            raise AssertionError("tampered signature was accepted")

        try:
            verify_token(block, raw, now=now + 61)
        except AuthorizationError:
            pass
        else:
            raise AssertionError("expired token was accepted")

        other_dir = root / "other-keys"
        _, _, other_raw = generate_keypair(other_dir)
        try:
            verify_token(block, other_raw, now=now)
        except AuthorizationError:
            pass
        else:
            raise AssertionError("wrong public key was accepted")

        permissions = stat.S_IMODE(private_key.stat().st_mode)
        assert permissions == 0o600, oct(permissions)

    print("Miles authorization self-test: PASS")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Generate and verify passive Miles Ed25519 authorization tokens."
    )
    sub = result.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="generate a new local Ed25519 key pair")
    init.add_argument("--key-dir", type=Path, required=True)
    init.add_argument("--charter-template", type=Path)
    init.add_argument("--charter-output", type=Path)
    init.set_defaults(function=command_init)

    render = sub.add_parser("render-charter", help="render a charter from an existing public key")
    render.add_argument("--key-dir", type=Path, required=True)
    render.add_argument("--charter-template", type=Path, required=True)
    render.add_argument("--charter-output", type=Path, required=True)
    render.set_defaults(function=command_render)

    issue = sub.add_parser("issue", help="issue a short-lived greg-trident token")
    issue.add_argument("--key-dir", type=Path, required=True)
    validity = issue.add_mutually_exclusive_group()
    validity.add_argument("--valid-hours", type=int, default=24)
    validity.add_argument("--valid-seconds", type=int)
    issue.add_argument("--output", type=Path)
    issue.set_defaults(function=command_issue)

    verify = sub.add_parser("verify", help="verify a token and print bounded JSON")
    verify.add_argument("--charter", type=Path)
    verify.add_argument("--public-key-base64url")
    verify.add_argument("--token-file", type=Path)
    verify.set_defaults(function=command_verify)

    self_test = sub.add_parser("self-test", help="exercise keygen, signing, and rejection cases")
    self_test.set_defaults(function=command_self_test)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return args.function(args)
    except (AuthorizationError, OSError) as exc:
        print(f"miles_authorize: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
