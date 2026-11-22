#!/usr/bin/env python3
"""Checks an upstream pacman package before repacking and the .deb made from it.

check-archive.py guard PKG.pkg.tar.zst
    Refuses archives tools/eb-build-debs cannot repack faithfully as an ordinary
    user: extended attributes, hard links, device nodes or FIFOs,
    setuid/setgid/sticky modes, and owners other than root.

check-archive.py elves PKG.pkg.tar.zst
    Lists the ELF files (regular files that start with \\x7fELF) of the
    upstream package, one path per line: the programs a rebuild replaces.

check-archive.py verify PKG.pkg.tar.zst PKG.deb [--replaced DIR] [--added DIR]
    Proves the repack changed nothing: every regular file and symlink the
    upstream .MTREE lists is in the .deb with the same SHA-256 (or link
    target) and mode, except the pacman hooks, which are dropped on purpose,
    and the license directory, which moves from /usr/share/licenses/PKG to
    /usr/share/doc/PKG/licenses. The only other files allowed in the .deb are
    the Debian copyright, changelog and README.Debian, and the files of
    --added.

    --replaced is a directory laid out like the package's root (the output of
    tools/eb-build-runtime). Each file in it must stand for a regular file
    upstream ships at the same path, and the .deb must hold that file's
    content, with upstream's mode, instead of upstream's.

    --added is a directory laid out like the package's root, with the files
    this packaging ships and upstream does not (packaging/eb-ubuntu/debian/
    PKG/added). Each must be at a path upstream does not use, and the .deb
    must hold exactly that content, with mode 644, owned by root. The result
    names them.

Run with python3 -I. Only reads the archives; writes nothing.
"""
import gzip
import hashlib
import os
import subprocess
import sys
import tarfile


def open_zstd_tar(path):
    process = subprocess.Popen(["zstd", "-dcq", "--", path], stdout=subprocess.PIPE)
    return process, tarfile.open(fileobj=process.stdout, mode="r|")


def finish(process):
    if process.wait() != 0:
        sys.exit(f"zstd failed on the archive ({process.returncode})")


def guard(path):
    process, archive = open_zstd_tar(path)
    problems = []
    for member in archive:
        if any(key.startswith(("SCHILY.xattr", "LIBARCHIVE.xattr")) for key in member.pax_headers):
            problems.append(f"extended attribute: {member.name}")
        if member.islnk() or member.isdev() or member.isfifo():
            problems.append(f"hard link, device or FIFO: {member.name}")
        if member.mode & 0o7000:
            problems.append(f"mode {member.mode:o}: {member.name}")
        if member.uid or member.gid:
            problems.append(f"owner {member.uid}:{member.gid}: {member.name}")
    finish(process)
    if problems:
        sys.exit("cannot repack faithfully:\n  " + "\n  ".join(problems))
    print(f"guard ok: {path}")


def elves(path):
    process, archive = open_zstd_tar(path)
    names = []
    for member in archive:
        if member.isfile() and archive.extractfile(member).read(4) == b"\x7fELF":
            names.append(member.name[2:] if member.name.startswith("./") else member.name)
    finish(process)
    print("\n".join(sorted(names)))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_files(root, what):
    """Maps each file below root, as a package path, to its SHA-256."""
    found = {}
    for directory, _, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            if os.path.islink(path) or not os.path.isfile(path):
                sys.exit(f"{what} is not a regular file: {path}")
            found[os.path.relpath(path, root)] = sha256_file(path)
    if not found:
        sys.exit(f"no {what} files in {root}")
    return found


def unvis(text):
    """Decodes mtree's \\ooo escapes into the original UTF-8 path."""
    raw = bytearray()
    i = 0
    data = text.encode("utf-8", "surrogateescape")
    while i < len(data):
        if data[i] == 0x5C and i + 3 < len(data) and all(0x30 <= b <= 0x37 for b in data[i + 1:i + 4]):
            raw.append(int(data[i + 1:i + 4], 8))
            i += 4
        else:
            raw.append(data[i])
            i += 1
    return raw.decode("utf-8", "surrogateescape")


def read_mtree(path):
    process, archive = open_zstd_tar(path)
    mtree = None
    pkgname = None
    for member in archive:
        if member.name in (".MTREE", ".PKGINFO"):
            data = archive.extractfile(member).read()
            if member.name == ".MTREE":
                mtree = gzip.decompress(data).decode("utf-8", "surrogateescape")
            else:
                for line in data.decode().splitlines():
                    if line.startswith("pkgname = "):
                        pkgname = line.split(" = ", 1)[1]
    finish(process)
    if mtree is None or pkgname is None:
        sys.exit("upstream package has no .MTREE or .PKGINFO")
    defaults = {}
    entries = {}
    for line in mtree.splitlines():
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if words[0] == "/set":
            defaults.update(word.split("=", 1) for word in words[1:])
            continue
        if words[0] == "/unset":
            for word in words[1:]:
                defaults.pop(word, None)
            continue
        keywords = dict(defaults)
        keywords.update(word.split("=", 1) for word in words[1:] if "=" in word)
        name = unvis(words[0])
        if name.startswith("./"):
            name = name[2:]
        entries[name] = keywords
    return pkgname, entries


def deb_members(path):
    process = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", path], stdout=subprocess.PIPE)
    archive = tarfile.open(fileobj=process.stdout, mode="r|")
    members = {}
    for member in archive:
        name = member.name[2:] if member.name.startswith("./") else member.name
        if member.isfile():
            digest = hashlib.sha256()
            stream = archive.extractfile(member)
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
            members[name] = ("file", digest.hexdigest(), member.mode & 0o7777, member.uid, member.gid)
        elif member.issym():
            members[name] = ("link", member.linkname, None, member.uid, member.gid)
        elif member.isdir():
            members[name.rstrip("/")] = ("dir", None, member.mode & 0o7777, member.uid, member.gid)
    if process.wait() != 0:
        sys.exit("dpkg-deb --fsys-tarfile failed")
    return members


def verify(upstream, deb, replaced_root=None, added_root=None):
    pkgname, entries = read_mtree(upstream)
    members = deb_members(deb)
    replaced = tree_files(replaced_root, "replacement") if replaced_root else {}
    added = tree_files(added_root, "added file") if added_root else {}
    replaced_count = 0
    licenses = f"usr/share/licenses/{pkgname}/"
    doc = f"usr/share/doc/{pkgname}/"
    allowed_extra = {doc + "copyright", doc + "changelog.Debian.gz", doc + "README.Debian"}
    expected = set()
    problems = []
    checked = 0
    for name, keywords in sorted(entries.items()):
        kind = keywords.get("type", "file")
        if name.startswith(".") or name.startswith("usr/share/libalpm/"):
            continue
        target = doc + "licenses/" + name[len(licenses):] if name.startswith(licenses) else name
        if kind == "dir":
            continue
        expected.add(target)
        got = members.get(target)
        if got is None:
            problems.append(f"missing in .deb: {target}")
            continue
        if got[3] or got[4]:
            problems.append(f"not owned by root in .deb: {target}")
        if kind == "file":
            ours = replaced.pop(target, None)
            if ours is not None:
                replaced_count += 1
            want = ours if ours is not None else keywords.get("sha256digest")
            if got[0] != "file" or got[1] != want:
                problems.append(f"content differs: {target}")
            elif "mode" in keywords and got[2] != int(keywords["mode"], 8):
                problems.append(f"mode {got[2]:o} instead of {keywords['mode']}: {target}")
            checked += 1
        elif kind == "link":
            if got[0] != "link" or got[1] != unvis(keywords.get("link", "")):
                problems.append(f"link differs: {target}")
            checked += 1
    for name in sorted(replaced):
        problems.append(f"replacement for a file upstream does not ship: {name}")
    for name, want in sorted(added.items()):
        got = members.get(name)
        if name in expected or name in allowed_extra:
            problems.append(f"added file is at a path of upstream's or Debian's: {name}")
        elif got is None:
            problems.append(f"added file missing in .deb: {name}")
        elif got[0] != "file" or got[1] != want:
            problems.append(f"added file's content differs: {name}")
        elif got[2] != 0o644 or got[3] or got[4]:
            problems.append(f"added file is not root's with mode 644: {name}")
    for name, got in members.items():
        if (got[0] in ("file", "link") and name not in expected
                and name not in allowed_extra and name not in added):
            problems.append(f"unexpected in .deb: {name}")
    if problems:
        sys.exit(f"{deb}: {len(problems)} problems\n  " + "\n  ".join(problems[:50]))
    result = (f"verify ok: {deb}: {checked - replaced_count} files and links identical to "
              f"{pkgname} upstream")
    if replaced_count:
        result += f", {replaced_count} replaced by the files in {replaced_root}"
    if added:
        result += (f", {len(added)} added by this packaging: "
                   + " ".join("/" + name for name in sorted(added)))
    print(result)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "guard":
        guard(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "elves":
        elves(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "verify":
        names, values = sys.argv[4::2], sys.argv[5::2]
        if (len(names) != len(values) or len(set(names)) != len(names)
                or not set(names) <= {"--replaced", "--added"}):
            sys.exit(__doc__)
        roots = dict(zip(names, values))
        verify(sys.argv[2], sys.argv[3], roots.get("--replaced"), roots.get("--added"))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
