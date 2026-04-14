from __future__ import annotations

import argparse
from pathlib import Path


KEY = "TELEGRAM_ADMIN_IDS"


def _parse_ids(raw: str) -> list[int]:
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(int(chunk))
    return values


def _format_ids(ids: list[int]) -> str:
    return ",".join(str(item) for item in sorted(set(ids)))


def _read_env(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _extract_current_ids(lines: list[str]) -> tuple[list[int], int | None]:
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith(f"{KEY}="):
            continue
        raw = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        try:
            return _parse_ids(raw), idx
        except Exception:
            return [], idx
    return [], None


def _write_ids(path: Path, ids: list[int], existing_lines: list[str], key_index: int | None) -> None:
    line = f"{KEY}={_format_ids(ids)}"
    lines = list(existing_lines)
    if key_index is None:
        lines.append(line)
    else:
        lines[key_index] = line
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def cmd_add(env_path: Path, user_id: int) -> None:
    lines = _read_env(env_path)
    ids, key_index = _extract_current_ids(lines)
    if user_id in ids:
        print(f"{KEY} already contains {user_id}")
        return
    ids.append(user_id)
    _write_ids(env_path, ids, lines, key_index)
    print(f"Added {user_id} to {KEY}: {_format_ids(ids)}")


def cmd_remove(env_path: Path, user_id: int) -> None:
    lines = _read_env(env_path)
    ids, key_index = _extract_current_ids(lines)
    if user_id not in ids:
        print(f"{user_id} is not present in {KEY}")
        return
    ids = [item for item in ids if item != user_id]
    _write_ids(env_path, ids, lines, key_index)
    print(f"Removed {user_id} from {KEY}: {_format_ids(ids)}")


def cmd_list(env_path: Path) -> None:
    lines = _read_env(env_path)
    ids, _ = _extract_current_ids(lines)
    if not ids:
        print(f"{KEY} is empty")
        return
    print(_format_ids(ids))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage TELEGRAM_ADMIN_IDS in .env")
    parser.add_argument("--env", default=".env", help="Path to .env file")

    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="Add admin id")
    add.add_argument("user_id", type=int)

    remove = sub.add_parser("remove", help="Remove admin id")
    remove.add_argument("user_id", type=int)

    sub.add_parser("list", help="List admin ids")

    args = parser.parse_args()
    env_path = Path(args.env)
    env_path.parent.mkdir(parents=True, exist_ok=True)

    if args.command == "add":
        cmd_add(env_path, args.user_id)
        return
    if args.command == "remove":
        cmd_remove(env_path, args.user_id)
        return
    if args.command == "list":
        cmd_list(env_path)
        return


if __name__ == "__main__":
    main()
