#!/usr/bin/env python3
"""
i3-bind — explore and create i3 keybindings from the command line.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import argparse
from pathlib import Path
from typing import Optional

# ── Config discovery ──────────────────────────────────────────────────────────

CONFIG_PATHS = [
    Path.home() / ".config/i3/config",
    Path.home() / ".i3/config",
    Path("/etc/i3/config"),
]


def find_config(override=None):
    if override:
        p = Path(override)
        if not p.exists():
            die(f"config not found: {p}")
        return p
    for p in CONFIG_PATHS:
        if p.exists():
            return p
    die("no i3 config found")


def die(msg):
    print(f"i3-bind: {msg}", file=sys.stderr)
    sys.exit(1)


# ── Parsing ───────────────────────────────────────────────────────────────────

def get_mod_var(config_path):
    for line in config_path.read_text().splitlines():
        m = re.match(r'^set\s+\$mod\s+(\S+)', line)
        if m:
            return m.group(1)
    return "Mod4"


def iter_bindings(config_path):
    mod_var      = get_mod_var(config_path)
    current_mode = "default"
    brace_depth  = 0

    for raw in config_path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("# "):
            continue
        m = re.match(r'^mode\s+["\']?([^"\'{}\\s]+)["\']?\s*{?', line)
        if m and not line.startswith("bindsym"):
            current_mode = m.group(1)
            if "{" in line:
                brace_depth += 1
            continue
        opens  = line.count("{")
        closes = line.count("}")
        if opens and not re.match(r'^mode\b', line):
            brace_depth += opens
        if closes:
            brace_depth -= closes
            if brace_depth <= 0:
                brace_depth  = 0
                current_mode = "default"
            continue
        m = re.match(r'^bindsym\s+(\S+)\s+(.*)', line)
        if m:
            key = m.group(1).replace("$mod", mod_var)
            yield current_mode, key, m.group(2).strip()


# ── Config writing ────────────────────────────────────────────────────────────

def backup(config_path):
    shutil.copy2(config_path, config_path.with_suffix(".bak"))


def do_add(key, command, mode, config_path):
    text = config_path.read_text()
    line = f"bindsym {key} {command}\n"
    backup(config_path)
    if mode == "default":
        text = text.rstrip("\n") + "\n\n" + line
    else:
        pattern = re.compile(
            r'(^mode\s+["\']?' + re.escape(mode) + r'["\']?\s*\{[^}]*)(\})',
            re.MULTILINE | re.DOTALL
        )
        m = pattern.search(text)
        if not m:
            die(f"mode '{mode}' not found in config")
        text = text[:m.start(2)] + "    " + line + text[m.start(2):]
    config_path.write_text(text)


def do_delete(key, mode, config_path):
    mod_var      = get_mod_var(config_path)
    lines        = config_path.read_text().splitlines(keepends=True)
    current_mode = "default"
    brace_depth  = 0
    target       = None

    for i, raw in enumerate(lines):
        line = raw.strip()
        m = re.match(r'^mode\s+["\']?([^"\'{}\\s]+)["\']?\s*{?', line)
        if m and not line.startswith("bindsym"):
            current_mode = m.group(1)
            if "{" in line:
                brace_depth += 1
            continue
        opens  = line.count("{")
        closes = line.count("}")
        if opens and not re.match(r'^mode\b', line):
            brace_depth += opens
        if closes:
            brace_depth -= closes
            if brace_depth <= 0:
                brace_depth  = 0
                current_mode = "default"
            continue
        m = re.match(r'^bindsym\s+(\S+)', line)
        if m and current_mode == mode:
            if m.group(1).replace("$mod", mod_var) == key:
                target = i
                break

    if target is None:
        return False
    backup(config_path)
    lines[target] = "# [deleted] " + lines[target]
    config_path.write_text("".join(lines))
    return True


def reload_i3():
    subprocess.Popen(["i3-msg", "reload"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(args):
    config = find_config(args.config)
    for mode, key, command in iter_bindings(config):
        if args.mode and args.mode != "default" and mode != args.mode:
            continue
        if args.mode and args.mode != "default":
            print(f"{key}\t{command}")
        else:
            print(f"{mode}\t{key}\t{command}")


def cmd_add(args):
    config = find_config(args.config)
    do_add(args.key, args.command, args.mode, config)
    suffix = f"  (mode: {args.mode})" if args.mode != "default" else ""
    print(f"added: bindsym {args.key} {args.command}{suffix}")
    if not args.no_reload:
        reload_i3()


def cmd_delete(args):
    config = find_config(args.config)
    ok = do_delete(args.key, args.mode, config)
    if not ok:
        suffix = f" in mode {args.mode}" if args.mode != "default" else ""
        die(f"binding not found: {args.key}{suffix}")
    suffix = f"  (mode: {args.mode})" if args.mode != "default" else ""
    print(f"deleted: {args.key}{suffix}")
    if not args.no_reload:
        reload_i3()


def cmd_modes(args):
    config = find_config(args.config)
    seen = dict.fromkeys(mode for mode, *_ in iter_bindings(config))
    for m in seen:
        print(m)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="i3-bind",
        description="Explore and create i3 keybindings from the command line.")
    parser.add_argument("--config",    default=None)
    parser.add_argument("--no-reload", action="store_true")
    parser.add_argument("--mode",      default="default",
                        help="i3 mode (default: default)")

    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="Add a keybinding")
    p_add.add_argument("key")
    p_add.add_argument("command")
    p_add.set_defaults(func=cmd_add)

    p_del = sub.add_parser("delete", aliases=["del", "rm"])
    p_del.add_argument("key")
    p_del.set_defaults(func=cmd_delete)

    p_list = sub.add_parser("list", aliases=["ls"])
    p_list.set_defaults(func=cmd_list)

    p_modes = sub.add_parser("modes")
    p_modes.set_defaults(func=cmd_modes)

    args = parser.parse_args()
    if args.command is None:
        cmd_list(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()