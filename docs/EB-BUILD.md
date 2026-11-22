# Building Droidloom's Ubuntu packages (EvilBorsch fork)

The erhai tablet (OnePlus Pad 3) runs Ubuntu 26.04 arm64. Droidloom is
installed there from three `.deb` packages made of upstream's Arch Linux ARM
packages of one release (now release 49, `0.1.0-49`):

| Package | Contents | Made from |
|---|---|---|
| `droidloom-runtime` | host programs, systemd units, polkit policy, cell template, notices | upstream's files, **programs built from this fork**, one udev rule of this packaging |
| `droidloom-image` | Android vendor image and framework overrides | upstream's package, files unchanged |
| `droidloom-base` | AOSP `system`, `system_ext`, `product` images | upstream's package, files unchanged |

Versions are `<upstream version>+ebN`, e.g. `0.1.0-49+eb2`; `N` grows with
every change of our packaging or programs.

## Why the programs are rebuilt

Upstream's release 49 does not work on Ubuntu 26.04 as built:

- `droidloom-package-helper` runs `/usr/bin/runuser`; Debian and Ubuntu have
  `/usr/sbin/runuser`. Upgrading or removing the package fails while the
  user is logged in (`201000a`, branch `eb/ubuntu-runuser`).
- `droidloomd` and `droidloom-supervisor` start Android with
  `unshare --forward-signals`, which util-linux 2.41 (Ubuntu 26.04, Debian
  13) rejects, so Android never starts (`5380af3`, branch
  `eb/unshare-util-linux-2.41`, which contains `201000a`).

Later changes of the fork's programs, in the package since the revision
named:

| Since | Change | Commit |
|---|---|---|
| `eb3` | the host's resolvers in the lease; Android is stopped by powering it off | `2712d37` |
| `eb4` | `droidloomctl launch`, which the exported desktop entries run, starts a stopped Droidloom as `droidloomctl start` does and then launches; it does not enable the user service (`docs/desktop-integration.md`) | `e20c4cb` |
| `eb4` | the desktop entries of Android's own applications (`com.android.*`) are named "Settings (Android)", "Files (Android)" | `1805815` |
| `eb4` | not a program: the udev rule `60-droidloom-loop.rules` and its `postinst` lines ([below](#the-loop-device-rule)) | `8f37ec6` |
| `eb5` | Android counts as ready once ActivityManager has user 0 in `RUNNING_UNLOCKED`, not already at `sys.boot_completed`; Files (Android), launched right after a start, failed with "Activity start failed: -92" (`docs/contracts/runtime-layout-v1.md`, "Lifecycle readiness") | `a856d4d` |
| `eb6` | `droidloom-package-helper prepare` says why pkexec ran no setup: its dialog dismissed (126), authorization not obtained or no agent (127), or the setup's own failure | `4c651ec` |
| `eb6` | `droidloomctl install` starts a stopped Droidloom as a launch does, and sets it up first where it never was (`start`'s `prepare`, which asks for an administrator through polkit); it names the app it installed or updated, from the launcher entry that is new or has a new `icon_key` (which carries the package's last update time); `--notify` follows the start, setup and install in one desktop notification that ends with that name or the reason it failed | `1019dad` |
| `eb6` | not a program: the desktop entry `droidloom-install-apk.desktop` ([below](#the-apk-desktop-entry)) | `2822b9c` |

Android itself (`droidloom-image`, `droidloom-base`) is not changed, so it is
not rebuilt: that is an AOSP build of tens of gigabytes and hours
(`docs/BUILDING.md`).

The tools below are on branch `eb/ubuntu-packaging`, which is based on
`eb/unshare-util-linux-2.41`:

| File | What it does |
|---|---|
| `tools/eb-arm64-sysroot` | makes the Ubuntu 26.04 arm64 root the programs are built against |
| `tools/eb-build-runtime` | cross-builds the runtime's programs from one commit |
| `tools/eb-build-debs` | makes the three packages |
| `packaging/eb-ubuntu/upstream.lock` | upstream release, source commit, SHA-256 of its archives |
| `packaging/eb-ubuntu/debian/` | control templates, maintainer scripts, copyright, README.Debian |
| `packaging/eb-ubuntu/debian/droidloom-runtime/added/` | files this packaging adds to upstream's, at their paths in the package |
| `packaging/eb-ubuntu/check-archive.py` | archive checks used by `eb-build-debs` |

## Prerequisites

An x86-64 Linux host (here WSL Ubuntu 26.04) with network access and:

```sh
sudo apt install git curl dpkg zstd python3 mmdebstrap pkg-config \
    gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu glslang-tools qemu-user
```

`glslang-tools` gives `glslangValidator`, which `droidloom-gpu-server`'s
build script needs for its texture decoders; `qemu-user` is only for the
checks below. Rust comes from rustup with the toolchain `rust-toolchain.toml`
pins (1.93.0, the compiler upstream's release 49 programs name) and its arm64
target:

```sh
rustup toolchain install 1.93.0 --profile minimal
rustup target add aarch64-unknown-linux-gnu --toolchain 1.93.0
```

## 1. The Ubuntu root

Once, as root (about 3 minutes, 342 MB):

```sh
sudo tools/eb-arm64-sysroot ~/work-droidloom-sysroot
```

It extracts the `-dev` packages the programs build against (`libgbm`,
`libxkbcommon`, `libwayland`, `libpipewire`; `libpam` and `libvulkan`, which
the programs load with `dlopen`, so the root can also run them under qemu)
with their dependencies from `ports.ubuntu.com` (`resolute`, `-updates`,
`-security`), with mmdebstrap's extract variant: no maintainer scripts run,
so no emulator is needed. In the root, `EB-SYSROOT-PACKAGES.txt` lists the
exact packages and `EB-SYSROOT-FILES.txt` the package of every file. On
22.11.2026: 107 packages, among them `libc6 2.43-2ubuntu2.7`,
`libgbm1 26.0.8-1ubuntu0.3`, `libpipewire-0.3-0t64 1.6.2-1ubuntu1.2`,
`libwayland-server0 1.24.0-2`, `libxkbcommon0 1.13.1-1`.

## 2. The programs

```sh
EB_JOBS=8 tools/eb-build-runtime ~/work-droidloom-sysroot ~/work-droidloom-runtime/out 5380af3
```

The last argument is the commit to build (default `HEAD`). The tool builds
it as upstream's package build does: `cargo build --locked --release
--target aarch64-unknown-linux-gnu` of the nine packages in `HOST_PACKAGES`
(`tools/droidloom-update/src/assemble.rs`), so Cargo unifies the same
features, cross-compiled with `aarch64-linux-gnu-gcc` against the root. It
refuses to build:

- a commit that does not contain the upstream release in `upstream.lock`;
- a commit whose `Cargo.lock` or `rust-toolchain.toml` differ from that
  release (the package's Cargo notices come from upstream's lock);
- when upstream's lists of programs (`HOST_PACKAGES`, `HOST_BINARIES`) no
  longer match the tool's.

The commit is exported with `git archive` into `~/work-droidloom-runtime/src`
(`EB_WORK`), so uncommitted changes never enter. Path remapping gives Cargo's
registry upstream's name `/build/cargo-home` and the work directory `/build`,
so no path of the build machine ends up in the programs.

57 seconds with 10 jobs from an empty target directory, once Cargo has
downloaded the crates. The output:

```
root/usr/bin/droidloom{d,ctl,-supervisor,-wayland,-applications,-audio,-gpu-server,-devices,-doctor}
root/usr/lib/droidloom/droidloom-package-helper
source-commit  source-date  source-changes  needed.txt  glibc-max
BUILD-INFO.txt  sysroot-packages.txt
```

`needed.txt` names, for every library a program links, the Ubuntu package of
the root that provides it; the build fails if one is not in the root or if a
program needs a newer GLIBC symbol version than the root's glibc.
`droidloom-update` is built too, as upstream does, but not packaged.

The build is reproducible: a second build from scratch (empty `EB_WORK`)
gave bit-identical programs.

### Checked against upstream's programs

Built from `5380af3` on 22.11.2026 and compared with the ten ELF files of
upstream's `droidloom-runtime-0.1.0-49-aarch64.pkg.tar.zst`:

- the same libraries (`NEEDED`) and the same versioned imports
  (`objdump -T`), newest `GLIBC_2.39`, except `droidloom-audio`: Ubuntu's gcc
  compiles its small C adapter (`src/pipewire.c`) with the stack protector,
  so it also imports `__stack_chk_fail` and `__stack_chk_guard`
  (`GLIBC_2.17`) and links `ld-linux-aarch64.so.1`; both are in `libc6`;
- under `qemu-aarch64 -L ~/work-droidloom-sysroot` the eight programs with a
  command line parser (all but `droidloom-wayland` and `droidloom-gpu-server`)
  print the same `--help` as upstream's, and the dynamic loader resolves
  every symbol of all ten (`LD_TRACE_LOADED_OBJECTS=1 LD_BIND_NOW=1`);
- `droidloomd` and `droidloom-supervisor` no longer contain
  `--forward-signals`; `droidloom-package-helper` contains
  `/usr/sbin/runuser`.

## 3. The packages

```sh
tools/eb-build-debs --host ~/work-droidloom-runtime/out eb2 droidloom-runtime
```

The packages to make follow the revision (default: all three); only
`droidloom-runtime` needs `--host`. Environment: `PKGS` (upstream archives,
downloaded when missing; about 1 GB), `OUT` (the `.deb`s, their `.sha256`
and `droidloom-<version>.SHA256SUMS`), `WORK` (staging on ext4),
`THREADS` (zstd threads, default 4). They default to
`~/work-droidloom-debs/{pkgs,out,stage}`.

For every package the tool checks the upstream archive against the SHA-256
in `upstream.lock` and its `.PKGINFO` (version, architecture, runtime →
image → base), refuses archives it cannot repack faithfully as an ordinary
user (xattrs, hard links, devices, setuid, owners other than root), moves
upstream's files into the package, replaces pacman's hooks with maintainer
scripts, and checks the resulting `.deb` file by file against upstream's
`.MTREE` (SHA-256, mode, link target; nothing extra but `copyright`,
`changelog.Debian.gz`, `README.Debian` and the files of the package's
`added/` tree, which the check names). Files keep upstream's times,
clamped to upstream's build date, or for the runtime to the commit date of
its programs, so a rebuild gives the same archive.

For `droidloom-runtime` it also:

- requires the programs in `--host` to be exactly upstream's ELF files, by
  path, and puts them in with upstream's modes; `.MTREE` verification then
  accepts these ten files with our content and nothing else;
- refuses if the package helper does not look for `/usr/sbin/runuser` or a
  program passes `--forward-signals`;
- sets `libc6 (>= …)` to the newest GLIBC version upstream's or our programs
  need (2.39 for release 49, as before) and checks that every library a
  program links comes from a package `Depends` names;
- names the fork's commit and the commits since upstream's in the package
  description, `changelog.Debian.gz`, `copyright` and `README.Debian`;
- adds the files of `debian/droidloom-runtime/added/`, now
  `/usr/lib/udev/rules.d/60-droidloom-loop.rules` and
  `/usr/share/applications/droidloom-install-apk.desktop` (below), with
  mode 644. A file at a path upstream ships is refused. `check-archive.py
  verify --added` requires exactly these files with exactly this content
  beside upstream's, and its result ends with "2 added by this packaging:"
  and their paths.

Maintainer scripts:

| Upstream (pacman) | Here | Action |
|---|---|---|
| `60-droidloom-upgrade.hook` | runtime `prerm upgrade`; image and base `preinst upgrade` | `droidloom-package-helper before-upgrade`: stop Droidloom or abort |
| `60-droidloom-remove.hook` | runtime `prerm remove` | `before-remove`: stop, remove launchers, polkit rule, `runtime.env` |
| `90-droidloom-installed.hook` | runtime `postinst configure` and file triggers on `/usr/lib/droidloom/{images,runtime,storage}` | `daemon-reload`, `after-install` |
| — | runtime `postinst configure` | when udev runs (`/run/udev`): `udevadm control --reload` and `udevadm trigger --subsystem-match=block --sysname-match='loop*' --action=change`, so the loop rule applies to a running Android; their failures are ignored, and without udev (a chroot during an image build) nothing is run |
| — | runtime `postrm purge` | remove `/etc/droidloom/*`; Android's data in `/var/lib/droidloom` is never removed |

Checked on 22.11.2026: `tools/eb-build-debs eb1 droidloom-image` reproduces
`droidloom-image_0.1.0-49+eb1_arm64.deb`, made before this packaging was in
the fork, byte for byte (SHA-256 `49a4985b…`), and the recipe as first
imported (commit `78f23ce`) reproduced `droidloom-runtime_0.1.0-49+eb1`
byte for byte (`640a2824…`).

### The loop-device rule

Android's system images and APEX packages are mounted through loop devices.
On the tablet a running Android had 49 of them, udisks offered each as a
volume, and the file manager's sidebar filled with "959 MB Volume", "30 MB
Volume" and so on. `60-droidloom-loop.rules` sets `UDISKS_IGNORE=1` on a loop
device whose `loop/backing_file` is one of:

| Pattern | What is mounted from there | Known from |
|---|---|---|
| `/usr/lib/droidloom/*` | the images of `droidloom-image` and `droidloom-base`, the GApps add-on | seen on the tablet |
| `/system/apex/*`, `/system_ext/apex/*`, `/vendor/apex/*`, `/product/apex/*`, `/apex/*`, `/data/apex/*` | APEX packages, by apexd in the cell; the path is the one in the cell's mount namespace | seen on the tablet |
| `/var/lib/droidloom/*` | the images of a source installation (`/var/lib/droidloom/images/current`, `packaging/cell-spec-arm64.json`) and the old storage images `data.img` and `metadata.img`, which `migrate-storage` mounts with `-o loop` (`storage_migration.rs`) | the fork's source |
| `/data/app-staging/*` | an APEX update that apexd mounts to check it before staging it | AOSP's apexd; not seen here |

With the first two rows, in `/etc/udev/rules.d` on the tablet, the volumes
offered went from 49 to 0, all 49 loop devices got the mark and snapd's loop
device stayed untouched. The rule as shipped was checked with udev 259 on
the build machine (`udevadm verify`, then loaded from `/run/udev/rules.d`):
a loop device on a file below `/var/lib/droidloom` gets the mark, one on a
file in `/tmp` does not, and a device loses the mark when it is detached or
set up for another file. Nothing else tells these devices apart for udev:
the mark goes by the path alone, so a loop device a person sets up on a file
below one of these paths is hidden from file managers too.

### The APK desktop entry

Opening an `.apk` in a file manager did nothing useful on the tablet: the
one application for `application/vnd.android.package-archive` there was
File Roller. `droidloom-install-apk.desktop` ("Install in Android",
`Name[ru]` too; `NoDisplay`, so it is no launcher entry of its own) runs
`droidloomctl install --notify %f`. The notification says "Android is
starting." or, where Droidloom was never set up, "Setting up Android
first: enter your password when asked." (polkit's agent asks), then
"Installing the app.", and ends with "Installed NAME", "Updated NAME" or
"Installed FILE" (no launcher entry changed) for 10 seconds, or with
"Could not install FILE" and the reason until it is dismissed. The texts
are English, as all of Droidloom's.

Which application opens APK files by default is the desktop's choice: the
erhai distribution names this entry in `denial-mimeapps.list` (package
`erhai-denial`). `desktop-file-validate` 0.28 accepts the entry.

### 0.1.0-49+eb2

`droidloom-runtime_0.1.0-49+eb2_arm64.deb`, 22.11.2026: programs from
`5380af354f4a1d027d365f58c26a481bf2676064` (upstream `572e1c5` + `201000a` +
`5380af3`), everything else upstream's release 49. Against
`0.1.0-49+eb1`: the same 640 entries with the same modes and owners, the
same maintainer scripts and `Depends`; only the ten programs, `control`,
`md5sums`, `copyright`, `changelog.Debian.gz` and `README.Debian` differ. It
installs with `droidloom-image` and `droidloom-base` `0.1.0-49+eb1`. Not yet
run on a device.

### 0.1.0-49+eb4

`droidloom-runtime_0.1.0-49+eb4_arm64.deb`, 22.11.2026, SHA-256
`62517e257e4033d9d6e1beb125b63f612fe3ae8c18e6ca82465b8eeb9ec4b484`: programs
from `9add222f805912b2d28a9c29258920d1419b5428` (`eb3`'s `2712d37` +
`e20c4cb` + `1805815`; `8f37ec6` and `9add222` change no program source, and
the programs built from `1805815` and from `9add222` are bit-identical).
Against `0.1.0-49+eb3`:

- 643 entries instead of 640: `/usr/lib/udev/`, `/usr/lib/udev/rules.d/` and
  `60-droidloom-loop.rules` (644, root); every other entry has the same mode
  and owner;
- five programs differ, the ones built with the supervisor's library or the
  catalog: `droidloomd`, `droidloomctl`, `droidloom-supervisor`,
  `droidloom-applications`, `droidloom-package-helper`; the other five are
  the same files;
- `README.Debian`, `copyright`, `changelog.Debian.gz`, `control` (version,
  size, commit, description), `md5sums` and `postinst` (the udev lines)
  differ; `prerm`, `postrm`, `triggers` and `Depends` are the same.

It installs with `droidloom-image` and `droidloom-base` `0.1.0-49+eb1`. On
the tablet a launch with Droidloom stopped started it (one start of the unit
for two launches made at once), but Files (Android) launched that way failed
3 of 3 with "Activity start failed: -92", and right after `droidloomctl
start` it failed at +0.01 s and opened at +0.6 s; `eb5` fixes that.

### 0.1.0-49+eb5

`droidloom-runtime_0.1.0-49+eb5_arm64.deb`, 22.11.2026, SHA-256
`5da8c23693ebfc90cc5b1bea02681c2bc99cc11b77d31559e15eefe1d2e9d37d`: programs
from `cb9abafe1abacdf3bacb8e687c0632d0eae992d0` (`eb4`'s programs +
`a856d4d`; `cb9abaf` changes the package texts only). Against
`0.1.0-49+eb4`:

- the same 643 entries with the same modes and owners;
- the same five programs differ as from `eb3` to `eb4`, the ones built with
  the supervisor's library: `droidloomd`, `droidloomctl`,
  `droidloom-supervisor`, `droidloom-applications`,
  `droidloom-package-helper`; the other five are the same files;
- `README.Debian`, `copyright`, `changelog.Debian.gz`, `control` (version,
  size, commit, description) and `md5sums` differ; `postinst`, `prerm`,
  `postrm`, `triggers` and `Depends` are the same.

It installs with `droidloom-image` and `droidloom-base` `0.1.0-49+eb1`. Not
yet run on a device.

### 0.1.0-49+eb6

`droidloom-runtime_0.1.0-49+eb6_arm64.deb`, 23.11.2026, SHA-256
`f08d5c191e5cde565c2e9b4c3fea09448c5ad83126a818841f748e8a4ccd00af`: programs
from `2822b9c576928967e515147f2ce182fea3a02a40` (`eb5`'s programs +
`4c651ec` + `1019dad`; `2822b9c` adds the desktop entry and changes the
package texts). Against `0.1.0-49+eb5`:

- 645 entries instead of 643: `/usr/share/applications/` and
  `droidloom-install-apk.desktop` (644, root); every other entry has the
  same mode and owner;
- the same five programs differ as from `eb4` to `eb5`, the ones built with
  the supervisor's library: `droidloomd`, `droidloomctl`,
  `droidloom-supervisor`, `droidloom-applications`,
  `droidloom-package-helper`; the other five are the same files;
- `README.Debian`, `copyright`, `changelog.Debian.gz`, `control` (version,
  size, commit, description) and `md5sums` differ; `postinst`, `prerm`,
  `postrm`, `triggers` and `Depends` are the same.

`eb-build-debs` verified it ("386 files and links identical to
droidloom-runtime upstream, 10 replaced …, 2 added by this packaging").
Under qemu `droidloomctl install --help` shows `--notify`; an install of a
file that is no APK is refused before anything starts, and on a machine
without Droidloom an install of an APK tries the start and ends with
"start failed; see journalctl --user -u droidloom.service". It installs
with `droidloom-image` and `droidloom-base` `0.1.0-49+eb1`. Not yet run on
a device.

## Another upstream release

1. Merge the release's commit into the fix branches and this one, and check
   that the fixes are still needed (`git log upstream/main`).
2. Update `packaging/eb-ubuntu/upstream.lock`: versions, commit, SHA-256
   from `gh release view packages -R denialwm/droidloom --json assets`.
3. Build the programs from the merged commit; compare their libraries,
   imports and `--help` with the release's, as above.
4. `tools/eb-build-debs --host … eb1` for all three packages (the revision
   starts again at `eb1` for a new upstream version). If upstream did not
   publish a new base, the image names the old one and the tool checks it.
