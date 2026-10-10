"""Add or update a support-agent account in config/users.yaml (stores a salted hash only).

  python -m scripts.add_agent <username>      # prompts for the password
"""
from __future__ import annotations

import getpass
import os
import sys

import yaml

from app.auth import USERS_FILE, hash_password


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.add_agent <username>")
    username = sys.argv[1].strip()
    password = getpass.getpass(f"Password for {username}: ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters.")
    cfg = yaml.safe_load(USERS_FILE.read_text(encoding="utf-8"))
    salt = os.urandom(16).hex()
    cfg.setdefault("agents", {})[username] = {
        "salt": salt, "hash": hash_password(password, salt, int(cfg.get("pbkdf2_iterations", 310000)))}
    USERS_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    print(f"Saved agent '{username}' to {USERS_FILE}")


if __name__ == "__main__":
    main()
