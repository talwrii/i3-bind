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

# ── Key normalization ─────────────────────────────────────────────────────────
# Maps every accepted modifier spelling to its canonical i3 form.
# Friendly aliases (super, meta, win, alt) resolve to the corresponding ModN.
MODIFIER_CANON = {
    "ctrl": "control", "control": "control",
    "shift": "shift",
    "lock": "lock",
    "alt": "mod1",
    "meta": "mod4", "super": "mod4", "win": "mod4",
    "mod1": "mod1", "mod2": "mod2", "mod3": "mod3",
    "mod4": "mod4", "mod5": "mod5",
}
VALID_MODIFIERS = set(MODIFIER_CANON.keys())

def normalize_key(key, mod_var):
    """Return a canonical form of a binding key string.

    Modifier names are lowercased and aliased (ctrl→control, super→mod4, etc.),
    $mod is resolved, modifiers are sorted (state mask is order-independent),
    and an uppercase single-letter keysym is lifted to Shift+<lower>.
    """
    parts = key.split("+")
    *mods, keysym = parts
    canon_mods = []
    for m in mods:
        if m.startswith("$"):
            m = mod_var if m == "$mod" else m
        canon_mods.append(MODIFIER_CANON.get(m.lower(), m.lower()))
    if len(keysym) == 1 and keysym.isalpha() and keysym.isupper():
        keysym = keysym.lower()
        if "shift" not in canon_mods:
            canon_mods.append("shift")
    canon_mods.sort()
    return "+".join(canon_mods + [keysym])

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
        if line.startswith("#"):
            continue
        m = re.match(r'^mode\s+["\']?([^"\'{}\s]+)["\']?\s*{?', line)
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
            key = normalize_key(m.group(1), mod_var)
            yield current_mode, key, m.group(2).strip()

# ── Config writing ────────────────────────────────────────────────────────────
def backup(config_path):
    shutil.copy2(config_path, config_path.with_suffix(".bak"))

def validate_key(key):
    parts = key.split("+")
    for mod in parts[:-1]:
        if mod.startswith("$"):
            continue
        if mod.lower() not in VALID_MODIFIERS:
            die(f"unknown modifier {mod!r} in {key!r}; "
                f"valid: Shift, Control/Ctrl, Alt, Super/Meta/Win, "
                f"Mod1-Mod5, or $variable.")

def do_add(key, command, mode, config_path):
    validate_key(key)
    mod_var = get_mod_var(config_path)
    norm = normalize_key(key, mod_var)
    for existing_mode, existing_key, _ in iter_bindings(config_path):
        if existing_mode == mode and existing_key == norm:
            die(f"binding already exists: {key}" +
                (f" in mode {mode}" if mode != "default" else "") +
                " (delete it first)")

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
    target_norm  = normalize_key(key, mod_var)
    lines        = config_path.read_text().splitlines(keepends=True)
    current_mode = "default"
    brace_depth  = 0
    target       = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("#"):
            continue
        m = re.match(r'^mode\s+["\']?([^"\'{}\s]+)["\']?\s*{?', line)
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
            if normalize_key(m.group(1), mod_var) == target_norm:
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
    activate_only = getattr(args, 'activate_mode', False)
    filter_mode = args.mode  # may be None
    for mode, key, command in iter_bindings(config):
        if filter_mode is not None and mode != filter_mode:
            continue
        if activate_only and not re.match(r'^mode\s+["\']?\S+', command):
            continue
        if filter_mode is not None:
            print(f"{key}\t{command}")
        else:
            print(f"{mode}\t{key}\t{command}")

def cmd_add(args):
    config = find_config(args.config)
    mode = args.mode or "default"
    do_add(args.key, args.command, mode, config)
    suffix = f"  (mode: {mode})" if mode != "default" else ""
    print(f"added: bindsym {args.key} {args.command}{suffix}")
    if not args.no_reload:
        reload_i3()

def cmd_delete(args):
    config = find_config(args.config)
    mode = args.mode or "default"
    ok = do_delete(args.key, mode, config)
    if not ok:
        suffix = f" in mode {mode}" if mode != "default" else ""
        die(f"binding not found: {args.key}{suffix}")
    suffix = f"  (mode: {mode})" if mode != "default" else ""
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
    parser.add_argument("--mode",      default=None,
                        help="i3 mode to filter by (default: all modes)")

    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="Add a keybinding")
    p_add.add_argument("key")
    p_add.add_argument("command")
    p_add.set_defaults(func=cmd_add)

    p_del = sub.add_parser("delete", aliases=["del", "rm"])
    p_del.add_argument("key")
    p_del.set_defaults(func=cmd_delete)

    p_list = sub.add_parser("list", aliases=["ls"])
    p_list.add_argument("--activate-mode", action="store_true",
                        help="Only show bindings that activate a mode")
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