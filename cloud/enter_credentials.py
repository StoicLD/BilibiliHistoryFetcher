"""Interactive credential setup, to be run directly by the account owner."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import uuid
import warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parents[1])
    base = parser.parse_args().base.resolve()
    if not sys.stdin.isatty():
        raise SystemExit("Run this setup yourself in an interactive terminal; piped input is refused")
    folder = base / "secrets"
    target = folder / "bilibili.env"
    print("Account-owner setup on the deployment host.")
    print("This stores your Bilibili SESSDATA in this cloud workspace for the daily job.")
    print("The folder will be owner-only (700), and the file owner-only (600).")
    print("Do not paste the value into chat. Input below is hidden and is not echoed.")
    if input("Continue with this credential storage and permissions? [yes/NO] ").strip().lower() != "yes":
        print("Nothing changed.")
        return 1
    if folder.is_symlink() or target.is_symlink():
        raise SystemExit("Refusing symbolic-link credential paths")
    if target.exists() and input("Replace the existing credential file? [yes/NO] ").strip().lower() != "yes":
        print("Nothing changed.")
        return 1
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("Bilibili SESSDATA value only: ").strip()
    except (getpass.GetPassWarning, EOFError):
        raise SystemExit("Hidden input is unavailable; no credential was saved") from None
    if not value or not value.isascii() or any(c.isspace() for c in value) or ";" in value or value.startswith("SESSDATA="):
        raise SystemExit("Invalid input: use only the nonempty SESSDATA value, not a Cookie header")
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    temporary = folder / (".bilibili-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("SESSDATA=" + value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    print("Credential saved. No value was printed. The deployment can now use the saved credential file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
