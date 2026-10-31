#!/usr/bin/env python3
"""Build a self-contained DarkPlaces.app and a sibling dedicated-server directory.

Homebrew dylibs are copied into Contents/Frameworks, rewritten to @rpath install
names, and given an @loader_path rpath so they find each other. Filenames match
the macOS dlopen() list in jpeg.c, image_png.c, snd_ogg.c, cap_ogg.c, ft2.c,
snd_xmp.c and libcurl.c. libjpeg must be the v6b ABI (libjpeg.62.dylib);
Homebrew's libjpeg-turbo bottle is v8 and is not used.

DarkPlaces looks for those names next to the executable and, on macOS, in
../Frameworks relative to Contents/MacOS.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_ID = "io.github.darkplacesengine.darkplaces"
EXE_NAME = "darkplaces-sdl"

# Names DarkPlaces passes to dlopen() on macOS, in the order the engine tries.
# The first existing file for each family is the one we install under that name.
DLOPEN_PREFER = [
    "libjpeg.62.dylib",
    "libpng16.16.dylib",
    "libpng15.15.dylib",
    "libpng14.14.dylib",
    "libpng12.0.dylib",
    "libvorbisfile.dylib",
    "libvorbisenc.dylib",
    "libvorbis.dylib",
    "libogg.dylib",
    "libtheora.dylib",
    "libfreetype.6.dylib",
    "libfreetype.dylib",
    "libxmp.4.dylib",
    "libxmp.dylib",
    "libcurl.4.dylib",
    "libcurl.3.dylib",
    "libcurl.2.dylib",
    "libSDL2-2.0.0.dylib",
    "libSDL2.0.dylib",
    "libSDL2.dylib",
]

# Libraries that must end up in the bundle even when the executable does not
# link them (they are dlopen'd). Missing any of these fails the build.
REQUIRED_SEEDS = [
    "libjpeg.62.dylib",
    "libpng16.16.dylib",
    "libvorbis.dylib",
    "libvorbisfile.dylib",
    "libvorbisenc.dylib",
    "libogg.dylib",
    "libtheora.dylib",
    "libfreetype.6.dylib",
    "libxmp.4.dylib",
    "libcurl.4.dylib",
]

# Extra dlopen aliases that should resolve to the bundled soname.
ALIASES = {
    "libfreetype.dylib": "libfreetype.6.dylib",
    "libxmp.dylib": "libxmp.4.dylib",
}

SYSTEM_PREFIXES = ("/usr/lib/", "/System/")
# Exact LC_RPATH allowlist. A prefix check is not enough: Homebrew's
# sdl2-compat shim uses @loader_path/../../../../opt/sdl3/lib, which otool -L
# never shows and which still starts with @loader_path.
ALLOWED_RPATHS = {
    "@loader_path",
    "@executable_path",
    "@executable_path/../Frameworks",
}


def run(cmd: list[str], cwd: Path | None = None) -> str:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc.stdout


def capture(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
    return proc.stdout


def is_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    proc = subprocess.run(["file", "-b", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode == 0 and "Mach-O" in proc.stdout


def otool_id(path: Path) -> str | None:
    out = capture(["otool", "-D", str(path)])
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    if "is not a" in lines[1] or lines[1].startswith("Archive :"):
        return None
    return lines[1]


def otool_deps(path: Path) -> list[str]:
    out = capture(["otool", "-L", str(path)])
    deps = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        deps.append(line.split(" (", 1)[0].strip())
    return deps


def otool_rpaths(path: Path) -> list[str]:
    out = capture(["otool", "-l", str(path)])
    rpaths = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for follow in lines[i + 1 : i + 6]:
            stripped = follow.strip()
            if stripped.startswith("path "):
                rpaths.append(stripped.split("path ", 1)[1].split(" (", 1)[0].strip())
                break
    return rpaths


def min_os_version(path: Path) -> tuple[int, ...] | None:
    out = capture(["otool", "-l", str(path)])
    found: list[tuple[int, ...]] = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("minos "):
            found.append(parse_version(stripped.split()[1]))
        elif stripped.startswith("version ") and " sdk " in f" {stripped} ":
            # LC_VERSION_MIN_MACOSX: version 11.0 sdk 14.0
            parts = stripped.split()
            if len(parts) >= 2 and parts[0] == "version":
                found.append(parse_version(parts[1]))
    if not found:
        return None
    return max(found)


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def is_system_path(path: str) -> bool:
    return path.startswith(SYSTEM_PREFIXES)


def is_libz(dep: str) -> bool:
    base = os.path.basename(dep)
    return base == "libz.dylib" or base.startswith("libz.")


class Bundler:
    def __init__(self, frameworks: Path, search_dirs: list[Path], main_exe: Path):
        self.frameworks = frameworks
        self.search_dirs = search_dirs
        self.main_exe = main_exe
        self.real_to_name: dict[str, str] = {}

    def find_basename(self, base: str) -> Path | None:
        for directory in self.search_dirs:
            candidate = directory / base
            if candidate.exists():
                return candidate
        return None

    def choose_name(self, src: Path, real: Path) -> str:
        names = {src.name, real.name}
        for directory in {src.parent, real.parent}:
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                try:
                    if entry.resolve() == real:
                        names.add(entry.name)
                except OSError:
                    continue
        taken = set(self.real_to_name.values())
        # DarkPlaces dlopens fixed filenames. Prefer those, then the library's
        # own install-name basename (the soname other binaries already use),
        # then the shortest alias so install_name_tool replacements fit.
        for preferred in DLOPEN_PREFER:
            if preferred in names and preferred not in taken:
                return preferred
        ident = otool_id(real)
        if ident:
            ident_base = os.path.basename(ident)
            if ident_base in names and ident_base not in taken:
                return ident_base
        for candidate in sorted(names, key=len):
            if candidate not in taken:
                return candidate
        raise SystemExit(f"could not choose a bundled name for {real}")

    def resolve_dep(self, dep: str, loader: Path) -> Path | None:
        if dep.startswith("@loader_path/"):
            return (loader.parent / dep[len("@loader_path/") :]).resolve()
        if dep.startswith("@executable_path/"):
            return (self.main_exe.parent / dep[len("@executable_path/") :]).resolve()
        if dep.startswith("@rpath/"):
            tail = dep[len("@rpath/") :]
            for rpath in otool_rpaths(loader):
                if rpath.startswith("@loader_path"):
                    root = loader.parent / rpath[len("@loader_path") :].lstrip("/")
                elif rpath.startswith("@executable_path"):
                    root = self.main_exe.parent / rpath[len("@executable_path") :].lstrip("/")
                else:
                    root = Path(rpath)
                candidate = (root / tail).resolve()
                if candidate.exists():
                    return candidate
            found = self.find_basename(os.path.basename(tail))
            return found.resolve() if found else None
        if dep.startswith("/"):
            if Path(dep).exists():
                return Path(dep).resolve()
            found = self.find_basename(os.path.basename(dep))
            return found.resolve() if found else None
        found = self.find_basename(dep)
        return found.resolve() if found else None

    def ensure_bundled(self, src: Path) -> str:
        real = src.resolve()
        real_key = str(real)
        if is_system_path(real_key):
            raise SystemExit(f"refusing to bundle system library {real}")
        existing = self.real_to_name.get(real_key)
        if existing:
            return existing
        if not real.is_file():
            raise SystemExit(f"missing library: {src}")
        name = self.choose_name(src, real)
        self.real_to_name[real_key] = name
        dest = self.frameworks / name
        self.frameworks.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, dest)
        dest.chmod(0o755)
        # @rpath dependencies are resolved via this dylib's @loader_path and via
        # the host executable (@executable_path/../Frameworks or @executable_path).
        self.rewrite(dest, ["@loader_path"])
        return name

    def rewrite(self, path: Path, rpaths: list[str]) -> None:
        path.chmod(0o755)
        subprocess.run(["codesign", "--remove-signature", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ident = otool_id(path)
        for dep in otool_deps(path):
            if ident and dep == ident:
                continue
            if is_system_path(dep):
                continue
            if is_libz(dep):
                if dep != "/usr/lib/libz.1.dylib":
                    run(["install_name_tool", "-change", dep, "/usr/lib/libz.1.dylib", str(path)])
                continue
            resolved = self.resolve_dep(dep, path)
            if resolved is None:
                raise SystemExit(f"cannot resolve dependency {dep} of {path}")
            if is_system_path(str(resolved)):
                continue
            bundled = self.ensure_bundled(resolved)
            new = f"@rpath/{bundled}"
            if dep != new:
                run(["install_name_tool", "-change", dep, new, str(path)])
        ident = otool_id(path)
        if ident:
            new_id = f"@rpath/{path.name}"
            if ident != new_id:
                run(["install_name_tool", "-id", new_id, str(path)])
        # Drop every existing rpath, including @loader_path/.../opt/sdl3/lib,
        # then install only the bundle-relative ones this binary needs.
        for rpath in otool_rpaths(path):
            run(["install_name_tool", "-delete_rpath", rpath, str(path)])
        for rpath in rpaths:
            if rpath not in ALLOWED_RPATHS:
                raise SystemExit(f"refusing to add rpath {rpath} on {path}")
            run(["install_name_tool", "-add_rpath", rpath, str(path)])

    def seed(self, base: str) -> str:
        found = self.find_basename(base)
        if found is None:
            searched = "\n".join(f"  {directory}" for directory in self.search_dirs)
            raise SystemExit(f"required library {base} was not found. Searched:\n{searched}")
        return self.ensure_bundled(found)


def search_dirs(jpeg_prefix: Path) -> list[Path]:
    dirs = []
    jpeg_lib = jpeg_prefix / "lib"
    if jpeg_lib.is_dir():
        dirs.append(jpeg_lib)
    brew_lib = Path("/opt/homebrew/lib")
    if brew_lib.is_dir():
        dirs.append(brew_lib)
    opt = Path("/opt/homebrew/opt")
    if opt.is_dir():
        for entry in sorted(opt.iterdir()):
            lib = entry / "lib"
            if lib.is_dir():
                dirs.append(lib)
    local_lib = Path("/usr/local/lib")
    if local_lib.is_dir():
        dirs.append(local_lib)
    return dirs


def write_plist(app: Path, version_short: str, version_bundle: str, git_describe: str, commit: str, min_os: str) -> None:
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "DarkPlaces",
        "CFBundleExecutable": EXE_NAME,
        "CFBundleIconFile": "Darkplaces",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "DarkPlaces",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version_short,
        "CFBundleSignature": "????",
        "CFBundleSupportedPlatforms": ["MacOSX"],
        "CFBundleVersion": version_bundle,
        "CFBundleGetInfoString": f"DarkPlaces {git_describe} (unofficial arm64 build)",
        "DarkPlacesGitDescribe": git_describe,
        "DarkPlacesCommit": commit,
        "LSApplicationCategoryType": "public.app-category.action-games",
        "LSMinimumSystemVersion": min_os,
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    }
    info = app / "Contents" / "Info.plist"
    with info.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    (app / "Contents" / "PkgInfo").write_text("APPL????", encoding="ascii")


def highest_min_os(paths: list[Path]) -> str:
    versions = [(11, 0)]
    for path in paths:
        found = min_os_version(path)
        if found:
            versions.append(found)
    return format_version(max(versions))


def sign(path: Path) -> None:
    run(["codesign", "--force", "--sign", "-", str(path)])


def macho_files(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink():
                continue
            if is_macho(path):
                found.append(path)
    return found


def rpath_is_hostile(rpath: str) -> bool:
    lowered = rpath.lower()
    return any(token in lowered for token in ("homebrew", "opt/sdl3", "/opt/", "usr/local", "sdl3"))


# System dylibs a bundled library may name without shipping a copy.
SYSTEM_DYLIB_BASES = {
    "libz.dylib",
    "libz.1.dylib",
    "libSystem.B.dylib",
    "libc++.1.dylib",
    "libc++abi.dylib",
    "libobjc.A.dylib",
    "libiconv.2.dylib",
    "libiconv.dylib",
    "libresolv.9.dylib",
    "libcharset.1.dylib",
}

DYLIB_REF = re.compile(rb"[\w@./+\-]{1,240}\.dylib")


def hidden_dlopen_problems(root: Path) -> list[str]:
    """Flag bundled libraries that name a dylib the bundle does not contain.

    otool -L misses dlopen() targets. sdl2-compat does this for libSDL3.0.dylib.
    The engine binary itself lists optional sonames (libd0_blind_id, …), so only
    the shipped dylibs are scanned here.
    """
    bundled = set()
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            if name.endswith(".dylib"):
                bundled.add(name)
    problems = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink() or not name.endswith(".dylib"):
                continue
            data = path.read_bytes()
            seen = set()
            for match in DYLIB_REF.findall(data):
                text = match.decode("ascii", "ignore")
                base = os.path.basename(text)
                if base in seen:
                    continue
                seen.add(base)
                if base in bundled or base in SYSTEM_DYLIB_BASES:
                    continue
                if text.startswith("/usr/lib/") or text.startswith("/System/"):
                    continue
                problems.append(f"{path} names non-bundled dylib {text}")
    return problems


def audit(root: Path) -> None:
    root = root.resolve()
    print(f"auditing {root}")
    dangling = []
    outside = []
    bad_arch = []
    bad_deps = []
    framework_errors = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if current.name.endswith(".framework"):
            current_link = current / "Versions" / "Current"
            if not current_link.is_symlink() or not current_link.exists():
                framework_errors.append(f"{current} is missing a live Versions/Current symlink")
            else:
                target = (current / "Versions" / os.readlink(current_link)).resolve()
                versions = (current / "Versions").resolve()
                if versions not in target.parents and target != versions:
                    framework_errors.append(f"{current_link} does not point inside Versions/")
        for name in list(dirnames) + list(filenames):
            path = current / name
            if not path.is_symlink():
                continue
            if not path.exists():
                dangling.append(str(path))
                continue
            real = path.resolve()
            if root != real and root not in real.parents:
                outside.append(f"{path} -> {real}")

    for path in macho_files(root):
        arches = capture(["lipo", "-archs", str(path)]).strip()
        if arches != "arm64":
            bad_arch.append(f"{path}: {arches}")
        ident = otool_id(path)
        loads = []
        if ident:
            loads.append(ident)
        loads.extend(otool_deps(path))
        for dep in loads:
            if ident and dep == ident and dep.startswith("@rpath/"):
                continue
            if is_system_path(dep) or dep.startswith(("@rpath/", "@executable_path/", "@loader_path/")):
                if "/opt/homebrew" in dep or "/usr/local" in dep:
                    bad_deps.append(f"{path}: {dep}")
                continue
            bad_deps.append(f"{path}: {dep}")
        for rpath in otool_rpaths(path):
            if rpath not in ALLOWED_RPATHS or rpath_is_hostile(rpath):
                bad_deps.append(f"{path} LC_RPATH: {rpath}")
        blob = path.read_bytes()
        for needle in (b"libSDL3", b"sdl2-compat", b"opt/sdl3", b"/opt/homebrew", b"/usr/local/"):
            if needle in blob:
                bad_deps.append(f"{path} contains {needle.decode()}")

    hidden = hidden_dlopen_problems(root)
    problems = dangling + outside + bad_arch + bad_deps + framework_errors + hidden
    if problems:
        print("bundle audit failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(f"audit ok ({len(macho_files(root))} Mach-O files, arm64, no Homebrew/usr/local load commands or hostile LC_RPATH)")


def bundle(args: argparse.Namespace) -> None:
    app = Path(args.app).resolve()
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    frameworks = app / "Contents" / "Frameworks"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    frameworks.mkdir()
    resources.mkdir()

    exe_src = Path(args.exe).resolve()
    exe = macos / EXE_NAME
    shutil.copy2(exe_src, exe)
    exe.chmod(0o755)

    icon_src = Path(args.icon).resolve()
    shutil.copy2(icon_src, resources / "Darkplaces.icns")

    bundler = Bundler(frameworks, search_dirs(Path(args.jpeg_prefix)), exe)
    bundler.rewrite(exe, ["@executable_path/../Frameworks"])
    for seed in REQUIRED_SEEDS:
        print(f"seeding {seed}")
        bundled_name = bundler.seed(seed)
        if bundled_name != seed:
            raise SystemExit(f"seed {seed} was bundled as {bundled_name}; DarkPlaces dlopens {seed}")

    dedicated_dir = Path(args.dedicated_dir).resolve()
    if dedicated_dir.exists():
        shutil.rmtree(dedicated_dir)
    dedicated_dir.mkdir(parents=True)
    dedicated = dedicated_dir / "darkplaces-dedicated"
    shutil.copy2(Path(args.dedicated).resolve(), dedicated)
    dedicated.chmod(0o755)
    # Pull any non-system deps into Frameworks before the app is signed.
    # The dedicated binary itself stays outside the app and gets its own rpath.
    saved_exe = bundler.main_exe
    bundler.main_exe = dedicated
    bundler.rewrite(dedicated, ["@executable_path"])
    bundler.main_exe = saved_exe

    # Aliases live next to the real libraries so either dlopen name works.
    for alias, target in ALIASES.items():
        if (frameworks / target).is_file() and not (frameworks / alias).exists():
            (frameworks / alias).symlink_to(target)

    machos = [exe, dedicated] + [
        path for path in frameworks.iterdir() if path.is_file() and not path.is_symlink()
    ]
    min_os = highest_min_os(machos)
    write_plist(app, args.version_short, args.version_bundle, args.git_describe, args.commit, min_os)

    subprocess.run(["xattr", "-cr", str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for name in sorted(os.listdir(frameworks)):
        path = frameworks / name
        if path.is_file() and not path.is_symlink():
            sign(path)
    sign(exe)
    sign(app)
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])

    for name in sorted(os.listdir(frameworks)):
        src = frameworks / name
        dest = dedicated_dir / name
        if src.is_symlink():
            dest.symlink_to(os.readlink(src))
        elif src.is_file():
            shutil.copy2(src, dest)
            dest.chmod(0o755)
    subprocess.run(["xattr", "-cr", str(dedicated_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    nested = [
        dedicated_dir / name
        for name in sorted(os.listdir(dedicated_dir))
        if (dedicated_dir / name).is_file() and not (dedicated_dir / name).is_symlink()
    ]
    for path in nested:
        if path.name != "darkplaces-dedicated":
            sign(path)
    sign(dedicated)

    print("bundled libraries:")
    for name in sorted(os.listdir(frameworks)):
        print(f"  {name}")
    print(f"LSMinimumSystemVersion={min_os}")
    audit(app)
    audit(dedicated_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    bundle_parser = sub.add_parser("bundle")
    bundle_parser.add_argument("--app", required=True)
    bundle_parser.add_argument("--exe", required=True)
    bundle_parser.add_argument("--dedicated", required=True)
    bundle_parser.add_argument("--dedicated-dir", required=True)
    bundle_parser.add_argument("--icon", required=True)
    bundle_parser.add_argument("--jpeg-prefix", required=True)
    bundle_parser.add_argument("--version-short", required=True)
    bundle_parser.add_argument("--version-bundle", required=True)
    bundle_parser.add_argument("--git-describe", required=True)
    bundle_parser.add_argument("--commit", required=True)
    bundle_parser.set_defaults(func=bundle)

    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("root")
    audit_parser.set_defaults(func=lambda args: audit(Path(args.root)))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
