"""
Base64 encode/decode utility script.

Usage:
    python scripts/base64_encode_decode.py <input_string>
    python scripts/base64_encode_decode.py --file <file_path>

Encodes the input to base64 (prints it), then decodes it back (prints it),
confirming round-trip integrity.
"""

import argparse
import base64
import sys


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def decode(encoded: str) -> bytes:
    return base64.b64decode(encoded)


def main():
    parser = argparse.ArgumentParser(description="Base64 encode then decode a string or file")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("text", nargs="?", help="Text string to encode/decode")
    group.add_argument("--file", "-f", help="Path to a file to encode/decode")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "rb") as f:
            raw = f.read()
        label = f"file: {args.file}"
    else:
        raw = args.text.encode("utf-8")
        label = f"text: {args.text}"

    print(f"--- Input ({label}) ---")
    print(raw.decode("utf-8", errors="replace"))
    print()

    encoded = encode(raw)
    print("--- Encoded (base64) ---")
    print(encoded)
    print()

    decoded = decode(encoded)
    print("--- Decoded (round-trip) ---")
    print(decoded.decode("utf-8", errors="replace"))
    print()

    if decoded == raw:
        print("Round-trip OK")
    else:
        print("ERROR: Round-trip mismatch!", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
