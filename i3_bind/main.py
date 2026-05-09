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

# ── Errors ────────────────────────────────────────────────────────────────────
class BindError(Exception):
    """A binding operation could not be performed."""

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

# ── Key normalization (for comparison) ────────────────────────────────────────
# Used internally for duplicate detection and lookup. Aliases (super/meta/win/
# alt/ctrl) all collapse onto i3's underlying ModN/Control names.
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
    """Canonical form of a binding key string (for comparison only).

    Modifier names are lowercased and aliased, $mod is resolved, modifiers are
    sorted (state mask is order-independent), and an uppercase single-letter
    keysym is lifted to Shift+<lower>.
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

def validate_key(key):
    parts = key.split("+")
    for mod in parts[:-1]:
        if mod.startswith("$"):
            continue
        if mod.lower() not in VALID_MODIFIERS:
            raise BindError(
                f"unknown modifier {mod!r} in {key!r}; "
                f"valid: Shift, Control/Ctrl, Alt, Super/Meta/Win, "
                f"Mod1-Mod5, or $variable.")

# ── Key form for writing to the config ────────────────────────────────────────
# i3 only understands Shift / Lock / Control / Mod1..Mod5 as modifier names.
# Friendly aliases that the user is allowed to type get translated here.
WRITE_ALIASES = {
    "super": "Mod4", "meta": "Mod4", "win": "Mod4",
    "alt":   "Mod1",
    "ctrl":  "Control",
}

def to_i3_form(key):
    """Rewrite friendly aliases into forms i3 actually accepts.

    Keysym, $variables, and capitalisation of already-canonical modifier
    names are left untouched.
    """
    parts = key.split("+")
    *mods, keysym = parts
    out = []
    for m in mods:
        if m.startswith("$"):
            out.append(m)
        else:
            out.append(WRITE_ALIASES.get(m.lower(), m))
    return "+".join(out + [keysym])

# ── Pure text transforms ──────────────────────────────────────────────────────
def _get_mod_var_text(text):
    for line in text.splitlines():
        m = re.match(r'^set\s+\$mod\s+(\S+)', line)
        if m:
            return m.group(1)
    return "Mod4"

def _iter_bindings_text(text, mod_var):
    current_mode = "default"
    brace_depth  = 0
    for raw in text.splitlines():
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

def _apply_add(text, key, command, mode, mod_var):
    """Add a binding. Returns new text. Raises BindError on conflict."""
    validate_key(key)
    norm = normalize_key(key, mod_var)
    for existing_mode, existing_key, existing_cmd in _iter_bindings_text(text, mod_var):
        if existing_mode == mode and existing_key == norm:
            raise BindError(
                f"binding already exists: {key} → {existing_cmd}" +
                (f" in mode {mode}" if mode != "default" else "") +
                " (delete it first)")
    line = f"bindsym {to_i3_form(key)} {command}\n"
    if mode == "default":
        return text.rstrip("\n") + "\n\n" + line
    pattern = re.compile(
        r'(^mode\s+["\']?' + re.escape(mode) + r'["\']?\s*\{[^}]*)(\})',
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        raise BindError(f"mode '{mode}' not found in config")
    return text[:m.start(2)] + "    " + line + text[m.start(2):]

def _apply_delete(text, key, mode, mod_var):
    """Delete a binding (comment it out). Returns new text. Raises if not found."""
    target_norm  = normalize_key(key, mod_var)
    lines        = text.splitlines(keepends=True)
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
        suffix = f" in mode {mode}" if mode != "default" else ""
        raise BindError(f"binding not found: {key}{suffix}")
    lines[target] = "# [deleted] " + lines[target]
    return "".join(lines)

def _apply_op(text, line, mode, mod_var):
    """Parse and apply a single batch line. Returns new text."""
    parts = line.split(None, 2)
    op = parts[0].lower()
    if op == "add":
        if len(parts) < 3:
            raise BindError(f"add requires KEY and COMMAND: {line!r}")
        return _apply_add(text, parts[1], parts[2], mode, mod_var)
    if op in ("del", "delete", "rm"):
        if len(parts) != 2:
            raise BindError(f"del requires exactly KEY: {line!r}")
        return _apply_delete(text, parts[1], mode, mod_var)
    raise BindError(f"unknown op {op!r} (expected add or del)")

# ── File-level wrappers ───────────────────────────────────────────────────────
def get_mod_var(config_path):
    return _get_mod_var_text(config_path.read_text())

def iter_bindings(config_path):
    text = config_path.read_text()
    yield from _iter_bindings_text(text, _get_mod_var_text(text))

def backup(config_path):
    shutil.copy2(config_path, config_path.with_suffix(".bak"))

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
    text = config.read_text()
    mod_var = _get_mod_var_text(text)
    try:
        new_text = _apply_add(text, args.key, args.command, mode, mod_var)
    except BindError as e:
        die(str(e))
    backup(config)
    config.write_text(new_text)
    suffix = f"  (mode: {mode})" if mode != "default" else ""
    print(f"added: bindsym {to_i3_form(args.key)} {args.command}{suffix}")
    if not args.no_reload:
        reload_i3()

def cmd_delete(args):
    config = find_config(args.config)
    mode = args.mode or "default"
    text = config.read_text()
    mod_var = _get_mod_var_text(text)
    try:
        new_text = _apply_delete(text, args.key, mode, mod_var)
    except BindError as e:
        die(str(e))
    backup(config)
    config.write_text(new_text)
    suffix = f"  (mode: {mode})" if mode != "default" else ""
    print(f"deleted: {args.key}{suffix}")
    if not args.no_reload:
        reload_i3()

def cmd_modes(args):
    config = find_config(args.config)
    seen = dict.fromkeys(mode for mode, *_ in iter_bindings(config))
    for m in seen:
        print(m)

def cmd_batch(args):
    """Apply a series of add/del ops atomically from stdin.

    Each non-blank, non-comment line is one op:
        add KEY COMMAND...
        del KEY        (also: delete, rm)

    The whole batch runs in memory; the file is written once at the end.
    If any op fails (unknown modifier, duplicate, missing target), nothing
    is written and i3 is not reloaded.
    """
    config = find_config(args.config)
    mode = args.mode or "default"
    text = config.read_text()
    mod_var = _get_mod_var_text(text)

    ops_applied = 0
    lineno = 0
    try:
        for lineno, raw in enumerate(sys.stdin, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            text = _apply_op(text, line, mode, mod_var)
            ops_applied += 1
    except BindError as e:
        die(f"batch line {lineno}: {e} (no changes written)")

    if ops_applied == 0:
        print("batch: no operations")
        return

    backup(config)
    config.write_text(text)
    print(f"batch applied: {ops_applied} op{'s' if ops_applied != 1 else ''}")
    if not args.no_reload:
        reload_i3()

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

    p_batch = sub.add_parser(
        "batch",
        help="Apply add/del ops from stdin atomically (one per line)")
    p_batch.set_defaults(func=cmd_batch)

    args = parser.parse_args()
    if args.command is None:
        cmd_list(args)
    else:
        args.func(args)

if __name__ == "__main__":
    main()