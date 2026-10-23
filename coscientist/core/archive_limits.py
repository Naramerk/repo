"""Streaming archive inspection for user inputs (DESIGN_M1 §9 / PR19 review).

Never trusts ZipInfo declared sizes. Streams member bytes with a running
counter and hard cutoff (total + unpack ratio). Rejects path traversal,
symlinks, nested archives, and applies the same budget to gzip/tar/bz2/xz.

Tar: **one** reader — stdlib ``tarfile`` in ``r|`` over a byte-counting
wrapper, with ``TarInfo`` hooks that reject oversized longname/pax, pax
``size`` / ``GNU.sparse.*``, and typeflag ``S`` before ``read()``. No
parallel header skipper (those desync on DIR/V7-dir bodies). Ustarseize
fields including POSIX base-256 are interpreted only by ``tarfile``;
hostile encodings become ``HeaderError``/``ReadError`` → ``INPUT_INVALID``.

EOF matches stdlib ``tarfile``: a zero block ends the archive (a non-zero
tail after a single zero block is ignored the same way). Callers must
unpack accepted archives only via stdlib ``tarfile`` (or treat gate reject
as authoritative) — do not hand-roll parsers that continue past one zero
block. Nested meta headers (typeflag ``x``/``L``/``K``/``g``) recurse in
``tarfile``; we cap consecutive depth at ``MAX_TAR_META_CHAIN`` and map
any unexpected ``Exception`` (incl. ``RecursionError``) to ``INPUT_INVALID``.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import struct
import tarfile
import unicodedata
import zipfile
import zlib
from typing import IO, Any, Literal, cast

from coscientist.core.reasons import RefusalKind

# (kind, reason) — same shape as input_limits.InputLimitViolation
ArchiveViolation = tuple[RefusalKind, str]

_CHUNK = 64 * 1024
# Cap GNU longname / longlink / pax extended headers (DoS).
MAX_TAR_XATTR_BYTES = 64 * 1024
# Cap nested meta recursion (``_proc_pax`` / ``_proc_gnulong`` → ``fromtarfile``).
MAX_TAR_META_CHAIN = 8
# Real decompress / format failures only — never mask internal bugs.
_DECOMPRESS_ERRORS: tuple[type[BaseException], ...] = (
    zipfile.BadZipFile,
    tarfile.ReadError,
    tarfile.CompressionError,
    tarfile.StreamError,
    tarfile.HeaderError,
    zlib.error,
    lzma.LZMAError,
    EOFError,
)
# Zip member open/read only — corrupt bzip2 member, encryption, method 99.
_ZIP_MEMBER_ERRORS: tuple[type[BaseException], ...] = (
    zipfile.BadZipFile,
    OSError,
    RuntimeError,
    NotImplementedError,
    zlib.error,
    EOFError,
    struct.error,
)

# Unicode slashes / invisibles that bypass naive ``/`` + ``..`` checks.
_UNSAFE_PATH_CHARS = (
    "\x00",
    "\u2215",  # division slash
    "\u2044",  # fraction slash
    "\uff0f",  # fullwidth solidus
    "\u200b",  # zero width space
    "\u200c",
    "\u200d",
    "\ufeff",
)


def _too_large(msg: str) -> ArchiveViolation:
    return RefusalKind.INPUT_TOO_LARGE, msg


def _invalid(msg: str) -> ArchiveViolation:
    return RefusalKind.INPUT_INVALID, msg


def _is_unsafe_path(name: str) -> bool:
    """Reject traversal, absolute, drive letters, NUL, unicode slash/ZWSP, NFKC."""
    if any(c in name for c in _UNSAFE_PATH_CHARS):
        return True
    n = unicodedata.normalize("NFKC", name)
    for slash in ("\u2215", "\u2044", "\uff0f", "\\"):
        n = n.replace(slash, "/")
    for zw in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        n = n.replace(zw, "")
    if "\x00" in n:
        return True
    if n.startswith("/") or n.startswith("~"):
        return True
    # Windows drive / UNC-ish: ``C:…`` or ``//server``
    if len(n) >= 2 and n[1] == ":" and n[0].isalpha():
        return True
    if n.startswith("//"):
        return True
    for p in n.split("/"):
        if p == "..":
            return True
        if p in (".", ""):
            continue
        # Fullwidth full stop sequences that NFKC may not fully collapse
        if p.replace("\uff0e", ".") == "..":
            return True
    return False


def _unix_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _head_is_tar(head: bytes) -> bool:
    return len(head) >= 262 and head[257:262] == b"ustar"


def _head_is_archive(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return True
    if head.startswith(b"\x1f\x8b"):
        return True
    if head.startswith(b"BZh"):
        return True
    if head.startswith(b"\xfd7zXZ\x00"):
        return True
    if _head_is_tar(head):
        return True
    return False


def _looks_like_nested_archive(head: bytes, member_name: str) -> bool:
    """True when member content or member basename is itself an archive.

    Top-level archive names must not trip the basename check — only embedded
    members (``outer.zip:inner.gz``) do. Tar is detected via ``ustar`` at 257,
    not the first 8 bytes alone.
    """
    low = member_name.lower().replace("\\", "/")
    base = low.rsplit(":", 1)[-1]
    if ":" in member_name.replace("\\", "/"):
        if base.endswith(
            (
                ".zip",
                ".gz",
                ".tgz",
                ".tar",
                ".tar.gz",
                ".tar.bz2",
                ".tar.xz",
                ".tbz2",
                ".tbz",
                ".txz",
                ".bz2",
                ".xz",
                ".7z",
            )
        ):
            return True
    return _head_is_archive(head)


def _stream_read_capped(
    fh: IO[bytes] | Any,
    *,
    name: str,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    running: list[int],
    compressed_total: int,
    allow_nested_probe: bool = True,
    count_bytes: bool = True,
) -> ArchiveViolation | None:
    """Drain ``fh``. When ``count_bytes`` is False, budget is owned elsewhere
    (e.g. ``_CountingReader`` under ``tarfile`` ``r|``) — only nested-probe."""
    first = b""
    while True:
        chunk = fh.read(_CHUNK)
        if not chunk:
            break
        if allow_nested_probe and len(first) < 512:
            need = 512 - len(first)
            first += chunk[:need]
            if len(first) >= 512 or len(chunk) < _CHUNK:
                if _looks_like_nested_archive(first, name):
                    return _invalid(f"nested archive forbidden ({name})")
        if not count_bytes:
            continue
        running[0] += len(chunk)
        if max_bytes is not None and running[0] > max_bytes:
            return _too_large(
                f"uncompressed {running[0]} > max_bytes {max_bytes} ({name})"
            )
        if max_unpack_ratio is not None:
            denom = max(1, compressed_total)
            if running[0] / float(denom) > float(max_unpack_ratio):
                return _too_large(
                    f"unpack ratio {running[0] / float(denom):.1f} > "
                    f"max_unpack_ratio {max_unpack_ratio} ({name})"
                )
    return None


def inspect_zip_streaming(
    data: bytes,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    max_zip_members: int | None,
    name: str = "archive.zip",
) -> ArchiveViolation | None:
    """Stream-decompress zip; forged/understated headers cannot bypass."""
    if not data:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return _invalid(f"corrupt zip ({name})")
    infos = zf.infolist()
    if max_zip_members is not None and len(infos) > max_zip_members:
        return _too_large(
            f"zip members {len(infos)} > max_zip_members {max_zip_members} ({name})"
        )
    compressed_total = max(1, len(data))
    running = [0]
    for info in infos:
        if info.is_dir():
            continue
        if _is_unsafe_path(info.filename):
            return _invalid(f"unsafe zip path {info.filename!r} ({name})")
        if _unix_is_symlink(info):
            return _invalid(f"zip symlink forbidden ({info.filename})")
        try:
            with zf.open(info, "r") as fh:
                bad = _stream_read_capped(
                    fh,
                    name=f"{name}:{info.filename}",
                    max_bytes=max_bytes,
                    max_unpack_ratio=max_unpack_ratio,
                    running=running,
                    compressed_total=compressed_total,
                )
                if bad is not None:
                    return bad
        except _ZIP_MEMBER_ERRORS as e:
            return _invalid(f"zip read error:{type(e).__name__} ({name})")
    return None


def _ratio_after_ok(
    running: int,
    compressed_total: int,
    max_unpack_ratio: float | None,
    name: str,
) -> ArchiveViolation | None:
    """Apply unpack-ratio only after a clean full decompress (not on truncation)."""
    if max_unpack_ratio is None:
        return None
    denom = max(1, compressed_total)
    if running / float(denom) > float(max_unpack_ratio):
        return _too_large(
            f"unpack ratio {running / float(denom):.1f} > "
            f"max_unpack_ratio {max_unpack_ratio} ({name})"
        )
    return None


def inspect_gzip_streaming(
    data: bytes,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    name: str = "archive.gz",
) -> ArchiveViolation | None:
    if not data:
        return None
    compressed_total = max(1, len(data))
    running = [0]
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gh:
            # Defer ratio until stream completes — truncated → corrupt, not ratio.
            bad = _stream_read_capped(
                gh,
                name=name,
                max_bytes=max_bytes,
                max_unpack_ratio=None,
                running=running,
                compressed_total=compressed_total,
            )
            if bad is not None:
                return bad
    except (gzip.BadGzipFile, EOFError, zlib.error, OSError, struct.error) as e:
        return _invalid(f"corrupt gzip:{type(e).__name__} ({name})")
    return _ratio_after_ok(running[0], compressed_total, max_unpack_ratio, name)


def inspect_bz2_streaming(
    data: bytes,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    name: str = "archive.bz2",
) -> ArchiveViolation | None:
    if not data:
        return None
    compressed_total = max(1, len(data))
    running = [0]
    try:
        with bz2.open(io.BytesIO(data), "rb") as bh:
            bad = _stream_read_capped(
                bh,
                name=name,
                max_bytes=max_bytes,
                max_unpack_ratio=None,
                running=running,
                compressed_total=compressed_total,
            )
            if bad is not None:
                return bad
    except (OSError, EOFError) as e:
        return _invalid(f"corrupt bz2:{type(e).__name__} ({name})")
    return _ratio_after_ok(running[0], compressed_total, max_unpack_ratio, name)


def inspect_xz_streaming(
    data: bytes,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    name: str = "archive.xz",
) -> ArchiveViolation | None:
    if not data:
        return None
    compressed_total = max(1, len(data))
    running = [0]
    try:
        with lzma.open(io.BytesIO(data), "rb") as xh:
            bad = _stream_read_capped(
                xh,
                name=name,
                max_bytes=max_bytes,
                max_unpack_ratio=None,
                running=running,
                compressed_total=compressed_total,
            )
            if bad is not None:
                return bad
    except (lzma.LZMAError, EOFError, OSError) as e:
        return _invalid(f"corrupt xz:{type(e).__name__} ({name})")
    return _ratio_after_ok(running[0], compressed_total, max_unpack_ratio, name)


def _parse_pax_records(payload: bytes) -> dict[str, str]:
    """Parse POSIX pax extended header records (``len keyword=value\\n``)."""
    out: dict[str, str] = {}
    i = 0
    n = len(payload)
    while i < n:
        if payload[i : i + 1] in (b"\x00", b""):
            break
        sp = payload.find(b" ", i)
        if sp < 0:
            break
        try:
            rec_len = int(payload[i:sp].decode("ascii", "replace"))
        except ValueError:
            break
        if rec_len <= 0 or i + rec_len > n:
            break
        record = payload[sp + 1 : i + rec_len]
        i += rec_len
        if record.endswith(b"\n"):
            record = record[:-1]
        eq = record.find(b"=")
        if eq < 0:
            continue
        key = record[:eq].decode("utf-8", "replace")
        val = record[eq + 1 :].decode("utf-8", "replace")
        out[key] = val
    return out


class _TarGateReject(Exception):
    """Fail-closed signal from ``_GuardedTarInfo`` / ``_CountingReader``."""

    def __init__(self, violation: ArchiveViolation) -> None:
        self.violation = violation
        super().__init__(violation[1])


class _CountingReader:
    """Byte-counting wrapper around a decompressed tar stream.

    ``tarfile`` in ``r|`` mode is the sole header/body reader; every
    ``read`` (headers, longname/pax, file data) counts toward ``max_bytes``
    / unpack ratio.
    """

    def __init__(
        self,
        raw: IO[bytes] | Any,
        *,
        running: list[int],
        max_bytes: int | None,
        max_unpack_ratio: float | None,
        compressed_total: int,
        name: str,
    ) -> None:
        self._raw = raw
        self.running = running
        self.max_bytes = max_bytes
        self.max_unpack_ratio = max_unpack_ratio
        self.compressed_total = compressed_total
        self.name = name

    def read(self, n: int = -1) -> bytes:
        if n == 0:
            return b""
        chunk = self._raw.read(n if n > 0 else -1)
        if chunk:
            self.running[0] += len(chunk)
            self._check_budget()
        return chunk if chunk else b""

    def _check_budget(self) -> None:
        if self.max_bytes is not None and self.running[0] > self.max_bytes:
            raise _TarGateReject(
                _too_large(
                    f"uncompressed {self.running[0]} > max_bytes "
                    f"{self.max_bytes} ({self.name})"
                )
            )
        if self.max_unpack_ratio is not None:
            denom = max(1, self.compressed_total)
            if self.running[0] / float(denom) > float(self.max_unpack_ratio):
                raise _TarGateReject(
                    _too_large(
                        f"unpack ratio {self.running[0] / float(denom):.1f} > "
                        f"max_unpack_ratio {self.max_unpack_ratio} ({self.name})"
                    )
                )

    def close(self) -> None:
        close = getattr(self._raw, "close", None)
        if close is not None:
            close()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        # ``tarfile`` may probe tell(); stream position is best-effort.
        tell = getattr(self._raw, "tell", None)
        if tell is not None:
            try:
                return int(tell())
            except Exception:  # noqa: BLE001
                pass
        return self.running[0]


class _PrefixFileobj:
    """Unread ``prefix`` in front of an existing binary fileobj.

    Used so ``_proc_pax`` can inspect pax bytes then let stdlib
    ``TarInfo._proc_pax`` re-read them without poking ``tarfile._Stream``
    private ``buf``/``pos``.
    """

    def __init__(self, prefix: bytes, inner: Any) -> None:
        self._prefix = prefix
        self._inner = inner

    def read(self, n: int = -1) -> bytes:
        if n == 0:
            return b""
        if self._prefix:
            if n < 0 or n >= len(self._prefix):
                take = self._prefix
                self._prefix = b""
                if n < 0:
                    return take + (self._inner.read(-1) or b"")
                need = n - len(take)
                if need > 0:
                    return take + (self._inner.read(need) or b"")
                return take
            take = self._prefix[:n]
            self._prefix = self._prefix[n:]
            return take
        return self._inner.read(n) or b""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _tar_block_size(size: int) -> int:
    """Round up to 512 like ``TarInfo._block`` (typeshed omits the method)."""
    blocks, rem = divmod(size, 512)
    if rem:
        blocks += 1
    return blocks * 512


class _GuardedTarInfo(tarfile.TarInfo):
    """``TarInfo`` that rejects DoS headers **before** ``fileobj.read``.

    Sole parser path: stdlib ``tarfile`` drives member iteration. We only
    gate longname/pax/sparse. Size fields (including POSIX base-256) are
    interpreted by ``tarfile``; hostile encodings surface as
    ``HeaderError`` / ``ReadError`` → ``INPUT_INVALID`` (safe reject).

    Consecutive typeflag ``x``/``g``/``L``/``K`` recurse via
    ``_proc_pax``/``_proc_gnulong`` → ``fromtarfile``; ``_meta_chain``
    caps that depth at ``MAX_TAR_META_CHAIN``.
    """

    archive_name: str = "archive.tar"
    # Per-open mutable depth — ``inspect_tar_streaming`` subclasses with a fresh list.
    _meta_chain: list[int] = []

    def _enter_meta(self) -> None:
        chain = type(self)._meta_chain
        chain[0] += 1
        if chain[0] > MAX_TAR_META_CHAIN:
            raise _TarGateReject(
                _invalid(
                    f"tar meta chain {chain[0]} > {MAX_TAR_META_CHAIN} "
                    f"({self.archive_name})"
                )
            )

    def _leave_meta(self) -> None:
        chain = type(self)._meta_chain
        if chain[0] > 0:
            chain[0] -= 1

    def _proc_gnulong(self, tarf: tarfile.TarFile) -> tarfile.TarInfo:
        self._enter_meta()
        try:
            if self.size is None or self.size < 0 or self.size > MAX_TAR_XATTR_BYTES:
                raise _TarGateReject(
                    _invalid(
                        f"tar longname/pax {self.size} > {MAX_TAR_XATTR_BYTES} "
                        f"({self.archive_name})"
                    )
                )
            # typeshed hides private ``_proc_*``; call the real CPython impl.
            return cast(Any, tarfile.TarInfo)._proc_gnulong(self, tarf)
        finally:
            self._leave_meta()

    def _proc_sparse(self, tarf: tarfile.TarFile) -> tarfile.TarInfo:
        raise _TarGateReject(
            _invalid(f"tar gnu sparse forbidden ({self.archive_name})")
        )

    def _proc_pax(self, tarf: tarfile.TarFile) -> tarfile.TarInfo:
        self._enter_meta()
        try:
            if self.size is None or self.size < 0 or self.size > MAX_TAR_XATTR_BYTES:
                raise _TarGateReject(
                    _invalid(
                        f"tar longname/pax {self.size} > {MAX_TAR_XATTR_BYTES} "
                        f"({self.archive_name})"
                    )
                )
            n = _tar_block_size(self.size)
            fobj = tarf.fileobj
            if fobj is None:
                raise _TarGateReject(
                    _invalid(f"tar pax missing fileobj ({self.archive_name})")
                )
            buf = fobj.read(n)
            if len(buf) < n:
                raise tarfile.HeaderError("invalid header")
            records = _parse_pax_records(buf)
            an = self.archive_name
            if "size" in records:
                raise _TarGateReject(_invalid(f"tar pax size forbidden ({an})"))
            if any(k.startswith("GNU.sparse.") for k in records):
                raise _TarGateReject(_invalid(f"tar pax GNU.sparse forbidden ({an})"))
            if self.size < len(buf) and any(buf[self.size :]):
                raise _TarGateReject(_invalid(f"tar pax non-zero padding ({an})"))
            # Re-present inspected bytes to stdlib without touching ``_Stream``.
            tarf.fileobj = _PrefixFileobj(buf, fobj)
            try:
                return cast(Any, tarfile.TarInfo)._proc_pax(self, tarf)
            finally:
                tarf.fileobj = fobj
        finally:
            self._leave_meta()


def _tar_member_over_budget(
    m: tarfile.TarInfo,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    running: int,
    compressed_total: int,
    name: str,
) -> ArchiveViolation | None:
    """Early-exit on declared size before extractfile / content stream."""
    if not m.isfile() or m.size is None or m.size < 0:
        return None
    if max_bytes is not None and m.size > max_bytes:
        return _too_large(
            f"tar member declared {m.size} > max_bytes {max_bytes} ({name}:{m.name})"
        )
    if max_bytes is not None and running + m.size > max_bytes:
        return _too_large(
            f"tar declared total {running + m.size} > max_bytes {max_bytes} "
            f"({name}:{m.name})"
        )
    if max_unpack_ratio is not None:
        denom = max(1, compressed_total)
        if (running + m.size) / float(denom) > float(max_unpack_ratio):
            return _too_large(
                f"tar declared unpack ratio {(running + m.size) / float(denom):.1f} > "
                f"max_unpack_ratio {max_unpack_ratio} ({name}:{m.name})"
            )
    return None


def _open_decompressed_tar(
    data: bytes, mode: Literal["r:*", "r:", "r:gz", "r:bz2", "r:xz"]
) -> IO[bytes] | Any:
    bio = io.BytesIO(data)
    if mode == "r:gz":
        return gzip.GzipFile(fileobj=bio, mode="rb")
    if mode == "r:bz2":
        return bz2.BZ2File(bio, mode="rb")
    if mode == "r:xz":
        return lzma.LZMAFile(bio, mode="rb")
    return bio


def inspect_tar_streaming(
    data: bytes,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    max_zip_members: int | None,
    name: str = "archive.tar",
    mode: Literal["r:*", "r:", "r:gz", "r:bz2", "r:xz"] = "r:*",
) -> ArchiveViolation | None:
    """Single-source tar inspect: ``tarfile`` ``r|`` + capped reader + guarded TarInfo.

    No parallel header parser — ``tarfile._proc_builtin`` decides which
    typeflags skip a data body (DIR / V7-dir / specials do **not**), so a
    DIR ``size>0`` body is parsed as subsequent headers (catching smuggled
    L / pax / sparse) rather than skipped. ``_GuardedTarInfo`` rejects
    oversized longname/pax, pax ``size`` / ``GNU.sparse.*``, typeflag
    ``S``, and meta chains longer than ``MAX_TAR_META_CHAIN`` before the
    corresponding ``read()``. Unexpected stream errors (including
    ``RecursionError``) map to ``INPUT_INVALID``. EOF follows stdlib
    ``tarfile`` (see module docstring).
    """
    if not data:
        return None
    compressed_total = max(1, len(data))
    running = [0]
    raw: IO[bytes] | Any | None = None
    counting: _CountingReader | None = None
    try:
        raw = _open_decompressed_tar(data, mode)
        counting = _CountingReader(
            raw,
            running=running,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            compressed_total=compressed_total,
            name=name,
        )
        guarded = type(
            "_GuardedTarInfoNamed",
            (_GuardedTarInfo,),
            {"archive_name": name, "_meta_chain": [0]},
        )
        # bufsize=512: avoid _Stream read-ahead past tar EOF (blocking
        # factor padding would inflate unpack-ratio on tiny archives).
        with tarfile.open(
            name=None,
            mode="r|",
            fileobj=cast(Any, counting),
            tarinfo=guarded,
            bufsize=512,
        ) as tf:
            member_count = 0
            for m in tf:
                member_count += 1
                if max_zip_members is not None and member_count > max_zip_members:
                    return _too_large(
                        f"tar members {member_count} > max_zip_members "
                        f"{max_zip_members} ({name})"
                    )
                if m.issym() or m.islnk() or m.isfifo() or m.isblk() or m.ischr():
                    return _invalid(f"tar special member forbidden ({m.name})")
                if _is_unsafe_path(m.name):
                    return _invalid(f"unsafe tar path {m.name!r} ({name})")
                link = m.linkname or ""
                if link and (m.issym() or m.islnk()) and _is_unsafe_path(link):
                    return _invalid(f"unsafe tar link {link!r} ({name})")
                if m.isdir():
                    continue
                if not m.isfile():
                    return _invalid(f"tar non-file member forbidden ({m.name})")
                early = _tar_member_over_budget(
                    m,
                    max_bytes=max_bytes,
                    max_unpack_ratio=max_unpack_ratio,
                    running=running[0],
                    compressed_total=compressed_total,
                    name=name,
                )
                if early is not None:
                    return early
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                with fh:
                    # Body bytes already counted by ``_CountingReader``.
                    bad = _stream_read_capped(
                        fh,
                        name=f"{name}:{m.name}",
                        max_bytes=max_bytes,
                        max_unpack_ratio=max_unpack_ratio,
                        running=running,
                        compressed_total=compressed_total,
                        allow_nested_probe=True,
                        count_bytes=False,
                    )
                    if bad is not None:
                        return bad
    except _TarGateReject as e:
        return e.violation
    except Exception as e:  # noqa: BLE001 — fail closed (RecursionError, etc.)
        return _invalid(f"corrupt tar:{type(e).__name__} ({name})")
    finally:
        if counting is not None:
            try:
                counting.close()
            except Exception:  # noqa: BLE001
                pass
        elif raw is not None:
            close = getattr(raw, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
    return None


def _gzip_payload_looks_like_tar(data: bytes) -> bool:
    """True when gunzipping ``data`` yields a ustar header (tar.gz / tgz)."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gh:
            head = gh.read(512)
    except (gzip.BadGzipFile, EOFError, zlib.error, OSError, struct.error):
        return False
    return _head_is_tar(head)


def _bz2_payload_looks_like_tar(data: bytes) -> bool:
    try:
        with bz2.open(io.BytesIO(data), "rb") as bh:
            head = bh.read(512)
    except (OSError, EOFError):
        return False
    return _head_is_tar(head)


def _xz_payload_looks_like_tar(data: bytes) -> bool:
    try:
        with lzma.open(io.BytesIO(data), "rb") as xh:
            head = xh.read(512)
    except (lzma.LZMAError, EOFError, OSError):
        return False
    return _head_is_tar(head)


def detect_archive_kind(data: bytes, media_type: str, name: str = "") -> str | None:
    mt = (media_type or "").lower().split(";", 1)[0].strip()
    low_name = (name or "").lower().replace("\\", "/")
    # Magic first. Avoid ``"zip" in "application/gzip"`` false positive.
    if len(data) >= 2 and data[:2] == b"PK":
        return "zip"
    if len(data) >= 2 and data[:2] == b"\x1f\x8b":
        # tar.gz / tgz: gzip wrapper around ustar — inspect as tar (r:gz).
        if low_name.endswith((".tgz", ".tar.gz")) or _gzip_payload_looks_like_tar(
            data
        ):
            return "tar.gz"
        return "gzip"
    if len(data) >= 3 and data[:3] == b"BZh":
        if low_name.endswith((".tar.bz2", ".tbz2", ".tbz")) or _bz2_payload_looks_like_tar(
            data
        ):
            return "tar.bz2"
        return "bz2"
    if len(data) >= 6 and data[:6] == b"\xfd7zXZ\x00":
        if low_name.endswith((".tar.xz", ".txz")) or _xz_payload_looks_like_tar(data):
            return "tar.xz"
        return "xz"
    if len(data) >= 265 and data[257:262] == b"ustar":
        return "tar"
    if mt in (
        "application/zip",
        "application/x-zip-compressed",
        "multipart/x-zip",
    ):
        return "zip"
    if mt in ("application/x-tar", "application/tar") or low_name.endswith(".tar"):
        return "tar"
    if mt in ("application/gzip", "application/x-gzip") or mt.endswith("+gzip"):
        if low_name.endswith((".tgz", ".tar.gz")) or _gzip_payload_looks_like_tar(data):
            return "tar.gz"
        return "gzip"
    if mt in ("application/x-bzip2", "application/bzip2") or low_name.endswith(".bz2"):
        if low_name.endswith((".tar.bz2", ".tbz2", ".tbz")) or _bz2_payload_looks_like_tar(
            data
        ):
            return "tar.bz2"
        return "bz2"
    if mt in ("application/x-xz", "application/xz") or low_name.endswith(".xz"):
        if low_name.endswith((".tar.xz", ".txz")) or _xz_payload_looks_like_tar(data):
            return "tar.xz"
        return "xz"
    return None


def inspect_archive(
    data: bytes,
    *,
    max_bytes: int | None,
    max_unpack_ratio: float | None,
    max_zip_members: int | None,
    name: str,
    media_type: str = "",
) -> ArchiveViolation | None:
    kind = detect_archive_kind(data, media_type, name=name)
    if kind == "zip":
        return inspect_zip_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            max_zip_members=max_zip_members,
            name=name,
        )
    if kind == "gzip":
        return inspect_gzip_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            name=name,
        )
    if kind == "bz2":
        return inspect_bz2_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            name=name,
        )
    if kind == "xz":
        return inspect_xz_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            name=name,
        )
    if kind == "tar.gz":
        return inspect_tar_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            max_zip_members=max_zip_members,
            name=name,
            mode="r:gz",
        )
    if kind == "tar.bz2":
        return inspect_tar_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            max_zip_members=max_zip_members,
            name=name,
            mode="r:bz2",
        )
    if kind == "tar.xz":
        return inspect_tar_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            max_zip_members=max_zip_members,
            name=name,
            mode="r:xz",
        )
    if kind == "tar":
        return inspect_tar_streaming(
            data,
            max_bytes=max_bytes,
            max_unpack_ratio=max_unpack_ratio,
            max_zip_members=max_zip_members,
            name=name,
            mode="r:*",
        )
    return None
