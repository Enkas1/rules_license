#!/usr/bin/env python3
# Fills missing package_version in rules_license metadata using full `git describe`
# - External repos: describe in .git/repos/<subrepo> (git-toprepo)
# - Internal code: describe <last-touch commit for dir> in the monorepo
# If no tag exists (describe fails), internals fall back to YYYY.MM.DD+g<sha>.
# Externals can be strict: error if none when --externals-require-tag is set.

import json, os, re, subprocess, sys
from pathlib import Path
from __future__ import annotations

NOASSERTION = "NOASSERTION"

def run(cmd, cwd=None):
    try:
        return subprocess.check_output(cmd, cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""

def norm(lbl: str) -> str:
    s = (lbl or "").strip()
    if s.startswith("@@"): s = "@" + s[2:]
    if s.startswith("@//"): s = s[1:]
    if s.startswith("@") and "//" in s:
        repo, rest = s[1:].split("//", 1)
        s = "@" + repo.split("+", 1)[0] + "//" + rest
    return s

def is_external_label(lbl: str) -> bool:
    s = norm(lbl)
    return s.startswith("@") or s.startswith("//external/") or "/external/" in s or s.startswith("//hpp/external/")

def label_to_rel(lbl: str) -> str | None:
    s = norm(lbl)
    m = re.match(r"^//([^:]+):", s)
    return m.group(1) if m else None

def ws_root_from_marker(marker: str | None) -> str | None:
    try:
        if not marker:
            return None
        p = os.path.realpath(marker)
        d = os.path.dirname(p)
        return d if os.path.isdir(d) else None
    except Exception:
        return None

def ws_root(marker_arg: str | None = None) -> str:
    # 1) prefer explicit marker (e.g., //:MODULE.bazel realpath)
    m = ws_root_from_marker(marker_arg)
    if m and os.path.isdir(os.path.join(m, ".git", "repos")):
        return m
    # 2) fallback: ask git
    g = run(["git", "rev-parse", "--show-toplevel"])
    if g:
        return g
    # 3) last resort
    return os.getcwd()

def toprepo_guess_dirs(ws: str, pkgname: str, maybe_url: str | None) -> list[str]:
    """Find subrepo dirs under .git/repos matching the package."""
    root = os.path.join(ws, ".git", "repos")
    if not os.path.isdir(root):
        return []
    hints = set()
    if pkgname:
        hints.add(pkgname.lower())
        hints.add(f"external-{pkgname.lower()}")
    if maybe_url:
        tail = re.sub(r"[./]+$", "", maybe_url.strip())
        tail = tail.rsplit("/", 1)[-1].lower()
        hints.add(tail)
        hints.add(f"external-{tail}")
    cands = []
    for d in os.listdir(root):
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        name = d.lower()
        if any(h in name for h in hints):
            cands.append(p)
    if not cands:
        # try matching from toprepo config urls
        cfg = run(["git", "-C", ws, "config", "-z", "--get-regexp", r"^toprepo\.repo\..*\.urls$"])
        parts = [x for x in cfg.split("\x00") if x]
        for i in range(0, len(parts), 2):
            key, urls = parts[i], parts[i+1]
            m = re.match(r"^toprepo\.repo\.(.+)\.urls$", key)
            if not m:
                continue
            name = m.group(1)
            if maybe_url and maybe_url.lower().split("/")[-1] in urls.lower():
                p = os.path.join(root, name)
                if os.path.isdir(p):
                    cands.append(p)
    return cands

def external_describe_from_toprepo(ws: str, pkgname: str, url: str | None) -> str:
    """Full describe string for external repo (e.g. 2337.1.0-35-g9c97273)."""
    for repo_dir in toprepo_guess_dirs(ws, pkgname, url):
        desc = run(["git", "-C", repo_dir, "describe"])
        if desc:
            return desc
    return ""

def internal_describe(ws: str, rel: str) -> str:
    """Describe the last-touch commit for rel-path in the monorepo; fallback to date+gsha."""
    rev = run(["git", "-C", ws, "rev-list", "-1", "HEAD", "--", rel]) or run(["git", "-C", ws, "rev-parse", "HEAD"])
    if not rev:
        return ""
    desc = run(["git", "-C", ws, "describe", rev])
    if desc:
        return desc
    # no reachable tag → readable fallback
    date = run(["git", "-C", ws, "show", "-s", "--date=format:%Y.%m.%d", "--format=%cd", rev])
    return f"{date}+g{rev[:12]}" if date else f"g{rev[:12]}"

def fill_versions(entries: list[dict], externals_require_tag: bool, ws_marker_arg: str | None) -> None:
    """
    Fill missing package_version using full `git describe` for all packages.
      - External (@... or //.../external/...): describe in .git/repos/<subrepo>
        (error if none and externals_require_tag=True)
      - Internal: describe <last-touch commit> in monorepo; fallback YYYY.MM.DD+g<sha>
    """
    ws = ws_root(ws_marker_arg)

    # hints from purl / package_url
    name2purl = {}
    for e in entries:
        for p in e.get("packages", []) or []:
            n = (p.get("package_name") or "").strip()
            u = (p.get("purl") or p.get("package_url") or "").strip()
            if n and u:
                name2purl.setdefault(n, u)

    def set_pkg_ver(pkg: str, ver: str):
        for e in entries:
            for li in e.get("licenses", []) or []:
                if (li.get("package_name") or "") == pkg and not (li.get("package_version") or "").strip():
                    li["package_version"] = ver
            for p in e.get("packages", []) or []:
                if (p.get("package_name") or "") == pkg and not (p.get("package_version") or "").strip():
                    p["package_version"] = ver

    for e in entries:
        etarget = ""
        if isinstance(e.get("target"), str):
            etarget = norm(e["target"])
        elif isinstance(e.get("label"), str):
            etarget = norm(e["label"])

        for li in e.get("licenses", []) or []:
            pkgname = (li.get("package_name") or "").strip()
            if not pkgname:
                continue
            curver = (li.get("package_version") or "").strip()
            if curver:
                continue

            # pick a label to classify external/internal
            used_by = li.get("used_by") or []
            label = norm(used_by[0]) if isinstance(used_by, list) and used_by else etarget

            if is_external_label(label):
                desc = external_describe_from_toprepo(ws, pkgname, name2purl.get(pkgname))
                if desc:
                    set_pkg_ver(pkgname, desc)
                elif externals_require_tag:
                    print(f"[SBOM ERROR] External package '{pkgname}' has no tag/describe (cannot infer from .git/repos).", file=sys.stderr)
                # else leave NOASSERTION
            else:
                rel = label_to_rel(label) or ""
                if not rel:
                    traces = e.get("traces") or []
                    if isinstance(traces, list):
                        for t in traces:
                            rel = label_to_rel(norm(t)) or rel
                            if rel:
                                break
                if rel:
                    set_pkg_ver(pkgname, internal_describe(ws, rel))

def main():
    if len(sys.argv) < 3:
        print("usage: git_version_fill.py <in.json> <out.json> [--externals-require-tag] [--ws-marker <path>]", file=sys.stderr)
        sys.exit(2)
    in_p, out_p = sys.argv[1], sys.argv[2]
    strict = False
    ws_marker = None
    i = 3
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--externals-require-tag":
            strict = True
        elif a == "--ws-marker" and i + 1 < len(sys.argv):
            i += 1
            ws_marker = sys.argv[i]
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
        i += 1

    data = json.loads(Path(in_p).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    fill_versions(data, strict, ws_marker)
    Path(out_p).write_text(json.dumps(data, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
