#!/usr/bin/env python3
"""Checks an upstream pacman package before repacking and the .deb made from it.

check-archive.py guard PKG.pkg.tar.zst
    Refuses archives tools/eb-build-debs cannot repack faithfully as an ordinary
    user: extended attributes, hard links, device nodes or FIFOs,
    setuid/setgid/sticky modes, and owners other than root.

check-archive.py verify PKG.pkg.tar.zst PKG.deb
    Proves the repack changed nothing: every regular file and symlink the
    upstream .MTREE lists is in the .deb with the same SHA-256 (or link
    target) and mode, except the pacman hooks, which are dropped on purpose,
    and the license directory, which moves from /usr/share/licenses/PKG to
    /usr/share/doc/PKG/licenses. The only other files allowed in the .deb are
    the Debian copyright, changelog and README.Debian.

Run with python3 -I. Only reads the archives; writes nothing.
"""
import gzip
import hashlib
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


def verify(upstream, deb):
    pkgname, entries = read_mtree(upstream)
    members = deb_members(deb)
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
            if got[0] != "file" or got[1] != keywords.get("sha256digest"):
                problems.append(f"content differs: {target}")
            elif "mode" in keywords and got[2] != int(keywords["mode"], 8):
                problems.append(f"mode {got[2]:o} instead of {keywords['mode']}: {target}")
            checked += 1
        elif kind == "link":
            if got[0] != "link" or got[1] != unvis(keywords.get("link", "")):
                problems.append(f"link differs: {target}")
            checked += 1
    for name, got in members.items():
        if got[0] in ("file", "link") and name not in expected and name not in allowed_extra:
            problems.append(f"unexpected in .deb: {name}")
    if problems:
        sys.exit(f"{deb}: {len(problems)} problems\n  " + "\n  ".join(problems[:50]))
    print(f"verify ok: {deb}: {checked} files and links identical to {pkgname} upstream")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "guard":
        guard(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "verify":
        verify(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
