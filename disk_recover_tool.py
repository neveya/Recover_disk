#!/usr/bin/env python3
"""
disk_recover_tool.py - NTFS / FAT12·16·32 / exFAT 디스크 이미지·드라이브에서 파일 목록(폴더 구조 포함)을 추출하고 복구하는 읽기 전용 도구

- 입력: raw 이미지(.dd/.img/.raw/.001 분할 포함), E01(pyewf 설치 시), 또는 장치(\\\\.\\PhysicalDrive1, E:, /dev/sdb: 읽기 전용)
- FAT/exFAT: 삭제된 파일/폴더 목록과 복구(연속 저장 가정), 체인 끊김/교차 연결/덮어쓰임 판정, --mode scan 으로 유실 폴더 탐색

- 입력: raw 이미지(.dd/.img/.raw/.001 분할 포함), 또는 E01(pyewf 설치 시)
- 읽기 방식
    기본(auto): $MFT 를 끝까지(확장 레코드로 이어진 구간 포함) 읽는다. 읽을 수 없으면 스캔으로 전환.
    --mode scan: 볼륨 전체를 훑어 'FILE'(MFT 레코드)와 'INDX'(폴더 인덱스) 블록을 수집한다(오래 걸림).
    --cache 파일: 스캔 결과를 저장/재사용. 파일이 있으면 기본(auto) 실행에서도 그 안의 폴더 인덱스를 이용한다.
- 폴더 경로는 레코드 번호뿐 아니라 '순번(sequence)'까지 비교해 판정한다.
  (MFT 에서 폴더 레코드가 덮어쓰였어도 폴더 인덱스(INDX)에 이름이 남아 있으면 복원한다.)
- 파일별 건강도(정상/의심/손상추정)를 메타데이터와 $Bitmap 으로 판정, --check-data 는 파일 앞부분도 확인
- 이 스크립트는 이미지를 절대 수정하지 않는다 (읽기 전용).

사용 예:
    python disk_recover_tool.py disk.001                                  # 요약 + CSV 자동 저장
    python disk_recover_tool.py disk.001 --cache scan.pkl                 # 기존 스캔 결과(폴더 인덱스) 재사용
    python disk_recover_tool.py disk.001 --mode scan --cache scan.pkl     # 새로 스캔하고 저장
    python disk_recover_tool.py disk.001 --cache scan.pkl --check-data --only-issues
    python disk_recover_tool.py disk.001 --head 0                         # 콘솔에 전체 목록 출력
    python disk_recover_tool.py --list-drives                             # 연결된 디스크/볼륨 나열 (관리자 권한)
    python disk_recover_tool.py E: --tree --browse                        # 사용 중인 드라이브를 읽기 전용으로 열어 복구
    python disk_recover_tool.py \\\\.\\PhysicalDrive1 --partition 1 --browse
    python disk_recover_tool.py usb.img --browse                          # FAT32/exFAT 이미지
"""
import argparse
import csv
import dataclasses
import datetime
import os
import pickle
import re
import struct
import sys
import time
import uuid
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

SECTOR = 512
ROOT_RECNO = 5
BITMAP_RECNO = 6
BASIC_DATA_GUID = uuid.UUID("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7").bytes_le
GIB = 1024 ** 3
CACHE_VERSION = 4
SYSTEM_NAMES = ["$MFT", "$MFTMirr", "$LogFile", "$Volume", "$AttrDef", ".", "$Bitmap", "$Boot",
                "$BadClus", "$Secure", "$UpCase", "$Extend"]


# --------------------------------------------------------------------------
# 이미지 접근 계층
# --------------------------------------------------------------------------
class RawImage:
    """raw 이미지. 'xxx.001', 'xxx.002' 처럼 분할된 파일은 자동으로 이어 붙인다."""

    def __init__(self, path: str):
        paths = [path]
        m = re.match(r"^(.*)\.(\d{3})$", path)
        if m:
            paths = []
            n = int(m.group(2))
            while os.path.exists(f"{m.group(1)}.{n:03d}"):
                paths.append(f"{m.group(1)}.{n:03d}")
                n += 1
        self.segments = []  # (시작 오프셋, 크기, 파일객체)
        pos = 0
        for p in paths:
            fh = open(p, "rb")  # 읽기 전용
            fh.seek(0, 2)
            size = fh.tell()
            self.segments.append((pos, size, fh))
            pos += size
        self.size = pos
        self.read_errors = 0

    def read(self, offset: int, size: int) -> bytes:
        out = bytearray()
        if offset < 0 or size <= 0:
            return b""
        for start, seg_size, fh in self.segments:
            if offset >= start + seg_size:
                continue
            if offset + size <= start:
                break
            local = max(offset, start) - start
            want = min(offset + size, start + seg_size) - (start + local)
            try:
                fh.seek(local)
                out += fh.read(want)
            except OSError:
                self.read_errors += 1
                out += b"\x00" * want
        return bytes(out)


class EwfImage:
    """E01 이미지 (pip install libewf-python 필요). 미검증 어댑터."""

    def __init__(self, path: str):
        import pyewf  # type: ignore

        self.h = pyewf.handle()
        self.h.open(pyewf.glob(path))
        self.size = self.h.get_media_size()
        self.read_errors = 0

    def read(self, offset: int, size: int) -> bytes:
        try:
            self.h.seek_offset(offset)
            return self.h.read_buffer(size)
        except OSError:
            self.read_errors += 1
            return b"\x00" * size


# === DEV-BEGIN ===
# --------------------------------------------------------------------------
# 물리 드라이브 / 볼륨 직접 읽기 (읽기 전용)
#   Windows: \\.\PhysicalDrive1, \\.\E:   (관리자 권한 cmd 필요)   Linux/macOS: /dev/sdb, /dev/nvme0n1p1 ...
#   원본에는 어떤 쓰기도 하지 않는다. 장치는 섹터 단위로 정렬해서 읽어야 하므로 정렬 읽기 계층을 둔다.
# --------------------------------------------------------------------------
def is_device_path(p: str) -> bool:
    return p.startswith("\\\\.\\") or p.startswith("\\\\?\\") or p.startswith("/dev/")


def normalize_source(s: str) -> str:
    """'E:' -> \\\\.\\E:, 'PhysicalDrive1' -> \\\\.\\PhysicalDrive1"""
    if os.name == "nt":
        if re.fullmatch(r"[A-Za-z]:\\?", s):
            return "\\\\.\\" + s[:2].upper()
        if re.fullmatch(r"(?i)physicaldrive\d+", s):
            return "\\\\.\\" + s
    return s


class DeviceImage:
    """장치(물리 디스크/볼륨)를 읽기 전용, 섹터 정렬로 읽는다. 읽기 오류 구간은 0 으로 채우고 read_errors 를 센다."""

    CACHE = 1024 * 1024

    def __init__(self, path: str, _file=None, _size: Optional[int] = None, _sector: Optional[int] = None):
        self.path = path
        self.fh = _file if _file is not None else open(path, "rb", buffering=0)   # 읽기 전용
        self.read_errors = 0
        self.bad_ranges: List[Tuple[int, int]] = []
        self.sector = _sector or self._query_sector()
        self.size = _size if _size is not None else self._query_size()
        self._c_off = -1
        self._c_data = b""

    # ---- 장치 정보 조회 ----
    def _query_size(self) -> int:
        if os.name == "nt":
            try:
                import ctypes
                import msvcrt
                h = msvcrt.get_osfhandle(self.fh.fileno())
                buf = ctypes.create_string_buffer(8)
                ret = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.DeviceIoControl(  # type: ignore[attr-defined]
                    ctypes.c_void_p(h), 0x7405C, None, 0, buf, 8, ctypes.byref(ret), None)   # IOCTL_DISK_GET_LENGTH_INFO
                if ok:
                    return int.from_bytes(buf.raw, "little")
            except Exception:  # noqa: BLE001
                pass
        try:
            return self.fh.seek(0, 2)
        except OSError:
            return 0

    def _query_sector(self) -> int:
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                h = msvcrt.get_osfhandle(self.fh.fileno())
                buf = ctypes.create_string_buffer(24)
                ret = ctypes.c_ulong(0)
                if ctypes.windll.kernel32.DeviceIoControl(  # type: ignore[attr-defined]
                        ctypes.c_void_p(h), 0x70000, None, 0, buf, 24, ctypes.byref(ret), None):   # IOCTL_DISK_GET_DRIVE_GEOMETRY
                    v = int.from_bytes(buf.raw[20:24], "little")
                    if v in (512, 1024, 2048, 4096):
                        return v
            else:
                import fcntl
                import struct as _st
                b = fcntl.ioctl(self.fh.fileno(), 0x1268, b"\0\0\0\0")   # BLKSSZGET
                v = _st.unpack("I", b)[0]
                if v in (512, 1024, 2048, 4096):
                    return v
        except Exception:  # noqa: BLE001
            pass
        return 512

    # ---- 읽기 ----
    def _raw(self, a: int, n: int) -> bytes:
        """a, n 모두 섹터 배수. 오류가 나면 섹터 단위로 나눠 읽고 실패한 섹터는 0."""
        ss = self.sector
        try:
            self.fh.seek(a)
            out = bytearray()
            while len(out) < n:
                part = self.fh.read(n - len(out))
                if not part:
                    break
                out += part
            return bytes(out) + b"\0" * (n - len(out)) if len(out) < n and a + len(out) < self.size else bytes(out)
        except OSError:
            out = bytearray()
            for k in range(0, n, ss):
                try:
                    self.fh.seek(a + k)
                    d = self.fh.read(ss)
                    out += d if len(d) == ss else d + b"\0" * (ss - len(d))
                except OSError:
                    self.read_errors += 1
                    self.bad_ranges.append((a + k, ss))
                    out += b"\0" * ss
            return bytes(out)

    def read(self, offset: int, size: int) -> bytes:
        if size <= 0 or offset < 0 or offset >= self.size:
            return b""
        size = min(size, self.size - offset)
        ss = self.sector
        if size <= 256 * 1024:                       # 작은 읽기는 1MiB 정렬 블록을 캐시해서 처리
            c0 = offset - offset % self.CACHE
            if (offset + size) <= c0 + self.CACHE:
                if self._c_off != c0:
                    self._c_data = self._raw(c0, min(self.CACHE, (self.size - c0) // ss * ss or ss))
                    self._c_off = c0
                lo = offset - c0
                return self._c_data[lo:lo + size]
        a = offset // ss * ss
        b = -(-(offset + size) // ss) * ss
        data = self._raw(a, b - a)
        return data[offset - a:offset - a + size]


def win_disk_number(path: str) -> Optional[int]:
    """Windows: 경로(드라이브 문자)가 있는 물리 디스크 번호. 알 수 없으면 None"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if len(drive) != 2:
            return None
        h = ctypes.windll.kernel32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)  # type: ignore[attr-defined]
        if h in (-1, 0xFFFFFFFFFFFFFFFF):
            return None
        try:
            buf = ctypes.create_string_buffer(12)
            ret = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.DeviceIoControl(  # type: ignore[attr-defined]
                ctypes.c_void_p(h), 0x2D1080, None, 0, buf, 12, ctypes.byref(ret), None)   # IOCTL_STORAGE_GET_DEVICE_NUMBER
            return int.from_bytes(buf.raw[4:8], "little") if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def dest_conflicts_with_source(src: str, dest_abs: str) -> Optional[str]:
    """저장 위치가 읽는 원본과 같은 디스크면 사유 문자열을 돌려준다(원본 위에 쓰면 삭제된 데이터가 덮어쓰임)."""
    if not is_device_path(src):
        return None
    if os.name == "nt":
        d_letter = os.path.splitdrive(dest_abs)[0].upper()
        m = re.fullmatch(r"\\\\\.\\([A-Za-z]:)", src)
        if m and m.group(1).upper() == d_letter:
            return f"저장 위치({d_letter})가 읽고 있는 원본 드라이브와 같습니다. 다른 디스크를 지정하세요."
        m = re.fullmatch(r"(?i)\\\\\.\\physicaldrive(\d+)", src)
        if m:
            dn = win_disk_number(dest_abs)
            if dn is not None and dn == int(m.group(1)):
                return f"저장 위치({d_letter})가 읽고 있는 원본 디스크(PhysicalDrive{dn}) 위에 있습니다. 다른 디스크를 지정하세요."
            if dn is None:
                sys.stderr.write("[주의] 저장 위치가 원본 디스크와 같은지 확인하지 못했습니다. 반드시 다른 디스크를 지정하세요.\n")
    else:
        try:
            import subprocess
            out = subprocess.run(["df", "--output=source", dest_abs], capture_output=True, text=True, timeout=10).stdout.split("\n")
            dev = out[1].strip() if len(out) > 1 else ""
            if dev and (src == dev or (dev.startswith(src) and dev[len(src):].isdigit()) or (src.startswith(dev) and src[len(dev):].isdigit())
                        or (re.sub(r"p?\d+$", "", dev) == re.sub(r"p?\d+$", "", src) and dev.startswith("/dev/"))):
                return f"저장 위치({dev})가 읽고 있는 원본 장치({src})와 같은 디스크입니다. 다른 디스크를 지정하세요."
        except Exception:  # noqa: BLE001
            pass
    return None


def out_blocked(args, pt, path: str, what: str) -> bool:
    """읽고 있는 장치와 같은 디스크에 파일을 쓰려는 경우 막는다(삭제된 데이터를 덮어쓸 수 있음)."""
    why = dest_conflicts_with_source(args.image, os.path.abspath(path))
    if why:
        print(pt.red(f"[!] {what} 저장을 건너뜁니다: {why}"))
    return bool(why)


def volume_letters() -> dict:
    """{(디스크 식별자, 시작 오프셋): (드라이브 문자/마운트 지점, 이름)}. Windows 는 볼륨 디스크 범위 IOCTL, Linux 는 /sys 와 /proc/mounts 로 찾는다."""
    res: dict = {}
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k.CreateFileW.restype = ctypes.c_void_p
            k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            k.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            mask = k.GetLogicalDrives()
            for i in range(26):
                if not mask & (1 << i):
                    continue
                letter = f"{chr(65 + i)}:"
                if k.GetDriveTypeW(letter + "\\") not in (2, 3):
                    continue
                label = ctypes.create_unicode_buffer(261)
                k.GetVolumeInformationW(letter + "\\", label, 261, None, None, None, None, 0)
                h = k.CreateFileW("\\\\.\\" + letter, 0, 3, None, 3, 0, None)  # 접근 권한 0: 정보 조회만, 데이터는 읽지 않음
                if h in (None, ctypes.c_void_p(-1).value):
                    continue
                try:
                    buf = ctypes.create_string_buffer(8 + 24 * 16)
                    ret = wintypes.DWORD(0)
                    if k.DeviceIoControl(h, 0x560000, None, 0, buf, len(buf), ctypes.byref(ret), None):
                        n = int.from_bytes(buf.raw[0:4], "little")
                        for e in range(min(n, 16)):
                            o = 8 + 24 * e
                            disk = int.from_bytes(buf.raw[o:o + 4], "little")
                            off = int.from_bytes(buf.raw[o + 8:o + 16], "little")
                            res[(disk, off)] = (letter, label.value)
                finally:
                    k.CloseHandle(h)
        else:
            mounts = {}
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    parts = ln.split()
                    if len(parts) > 1 and parts[0].startswith("/dev/"):
                        mounts.setdefault(os.path.realpath(parts[0]), parts[1].replace("\\040", " "))
            for dev, mp in mounts.items():
                nm = os.path.basename(dev)
                st = f"/sys/class/block/{nm}/start"
                if os.path.exists(st):
                    parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{nm}")))
                    with open(st) as f:
                        res[("/dev/" + parent, int(f.read()) * 512)] = (mp, "")
                else:
                    res[(dev, 0)] = (mp, "")
    except Exception:  # noqa: BLE001
        pass
    return res


def list_drives() -> int:
    """연결된 물리 디스크와 그 안의 볼륨 후보를 나열한다(읽기 전용)."""
    print("[물리 디스크 목록]  (Windows 는 관리자 권한 cmd 가 필요합니다)")
    found = 0
    denied = 0
    if os.name == "nt":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore[attr-defined]
                print("  * 지금은 관리자 권한이 아닙니다. 디스크를 열 수 없으니 '관리자 권한으로 실행'한 창에서 다시 실행하세요.")
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        cands = [f"\\\\.\\PhysicalDrive{i}" for i in range(32)]
    else:
        cands = []
        try:
            for nm in sorted(os.listdir("/sys/block")):
                if not nm.startswith(("loop", "ram", "zram", "dm-", "sr")):
                    cands.append("/dev/" + nm)
        except OSError:
            pass
    letters = volume_letters()
    named: List[str] = []
    for p in cands:
        try:
            im = DeviceImage(p)
        except PermissionError:
            print(f"  {p}: 접근 거부 - 관리자 권한이 필요합니다")
            denied += 1
            continue
        except OSError:
            continue
        found += 1
        try:
            m = re.search(r"(?i)physicaldrive(\d+)$", p)
            dkey = int(m.group(1)) if m else p
            title = f"디스크 {m.group(1)}  ({p})" if m else p
            print(f"\n  {title}  크기 {fmt_size(im.size)}  섹터 {im.sector}B")
            vols = discover_volumes(im)
            for k, v in enumerate(vols, 1):
                hit = letters.get((dkey, v.offset))
                if hit is None:  # 오프셋이 한두 섹터 어긋나는 경우 대비
                    for (d2, o2), val in letters.items():
                        if d2 == dkey and abs(o2 - v.offset) < 1 << 20:
                            hit = val
                            break
                if hit:
                    named.append(hit[0])
                    where = f"{hit[0]}" + (f" [{hit[1]}]" if hit[1] else "")
                else:
                    where = "(문자 없음)"
                print(f"      볼륨 {k}: {where:<16} 시작 {fmt_size(v.offset)} / 크기 {fmt_size(v.size)} / {v.fs} / {v.note}")
        except Exception as ex:  # noqa: BLE001
            print(f"      (볼륨 확인 실패: {ex})")
    if not found and denied:
        print("  디스크가 있지만 권한이 없어 열지 못했습니다. 관리자 권한으로 실행하세요.")
    elif not found:
        print("  접근 가능한 디스크를 찾지 못했습니다.")
    if named:
        print("\n드라이브 문자/마운트 지점으로 바로 열 수 있는 볼륨: " + ", ".join(named) + "   (문자가 없는 볼륨은 디스크 번호로 열고 볼륨 번호를 고르세요)")
    print("\n사용: python disk_recover_tool.py \\\\.\\PhysicalDrive1 --tree   또는   python disk_recover_tool.py E:   (읽기 전용으로 엽니다)")
    return 0
# === DEV-END ===


def open_image(path: str):
    path = normalize_source(path)
    if is_device_path(path):
        return DeviceImage(path)
    if path.lower().endswith((".e01", ".ex01", ".s01")):
        return EwfImage(path)
    return RawImage(path)


# --------------------------------------------------------------------------
# 진행률 표시
# --------------------------------------------------------------------------
def fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_eta(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}시간 {m:02d}분" if h else f"{m}분 {s:02d}초"


class Progress:
    """1초마다 갱신되는 진행률 (퍼센트 소수점, 속도, 남은 시간)."""

    def __init__(self, total: int, label: str, as_bytes: bool = True):
        self.total = max(total, 1)
        self.label = label
        self.as_bytes = as_bytes
        self.t0 = time.monotonic()
        self.last = 0.0

    def update(self, done: int, extra: str = "", force: bool = False):
        now = time.monotonic()
        if not force and now - self.last < 1.0:
            return
        self.last = now
        el = max(now - self.t0, 1e-6)
        rate = done / el
        eta = (self.total - done) / rate if rate > 0 else 0
        if self.as_bytes:
            amount = f"{fmt_size(done)}/{fmt_size(self.total)} | {rate / 1e6:5.0f} MB/s"
        else:
            amount = f"{done}/{self.total}개 | {rate:6.0f}개/초"
        tot_est = el + eta
        line = (f"{self.label} {done * 100 / self.total:6.2f}% | {amount} | 경과 {fmt_eta(el)} / 예상 전체 {fmt_eta(tot_est)}"
                f" | 남은 시간 {fmt_eta(eta)} | {extra}")
        w = term_cols()          # 창 너비에 맞춰 한 줄로 유지(줄바꿈되면 \r 갱신이 깨짐)
        print("\r" + padw(fit(line, w), w), end="", file=sys.stderr, flush=True)

    def done(self, extra: str = ""):
        self.update(self.total, extra, force=True)
        print("", file=sys.stderr)


# --------------------------------------------------------------------------
# 파티션 / 볼륨 탐색
# --------------------------------------------------------------------------
@dataclass
class Volume:
    offset: int
    size: int
    note: str
    boot_ok: bool
    fs: str = "NTFS"


def is_ntfs_boot(sec: bytes) -> bool:
    return len(sec) >= 512 and sec[3:11] == b"NTFS    " and sec[510:512] == b"\x55\xaa"


def _candidate(img, start_lba: int, count: int, note: str, ntfs_hint: bool) -> Optional[Volume]:
    off = start_lba * SECTOR
    size = count * SECTOR
    boot = img.read(off, SECTOR)
    k = detect_fs(boot)
    if k == "NTFS":
        return Volume(off, size, note, True)
    if k == "exFAT":
        return Volume(off, size, note + " (exFAT)", True, "exFAT")
    if k == "FAT":
        return Volume(off, size, note + " (FAT)", True, "FAT")
    if ntfs_hint:  # 파티션 타입은 NTFS 계열인데 부트섹터가 깨진 경우
        return Volume(off, size, note + " (부트섹터 손상 의심)", False)
    return None


def _gpt_entries(img, hdr: bytes):
    if hdr[:8] != b"EFI PART":
        return None
    entries_lba, n, esz = struct.unpack_from("<QII", hdr, 0x48)
    n = min(n, 256)
    if esz < 128 or esz > 1024:
        return None
    return img.read(entries_lba * SECTOR, n * esz), n, esz


def discover_volumes(img) -> List[Volume]:
    vols: List[Volume] = []
    s0 = img.read(0, SECTOR)
    if is_ntfs_boot(s0):
        return [Volume(0, img.size, "이미지 전체가 NTFS 볼륨", True)]
    k0 = detect_fs(s0)
    if k0 in ("FAT", "exFAT"):
        return [Volume(0, img.size, f"이미지 전체가 {k0} 볼륨", True, k0)]

    if len(s0) == SECTOR and s0[510:512] == b"\x55\xaa":
        is_gpt = False
        for i in range(4):
            _, _, ptype, _, lba, cnt = struct.unpack_from("<B3sB3sII", s0, 446 + 16 * i)
            if ptype == 0xEE:
                is_gpt = True
            elif ptype in (0x07, 0x0B, 0x0C, 0x01, 0x04, 0x06, 0x0E) and cnt:
                v = _candidate(img, lba, cnt, f"MBR 파티션 {i + 1}", ptype == 0x07)
                if v:
                    vols.append(v)
            elif ptype in (0x05, 0x0F):
                pass  # 확장 파티션은 아직 미지원
        if is_gpt:
            got = _gpt_entries(img, img.read(SECTOR, SECTOR))
            if got is None:  # 주 헤더 손상 -> 백업 헤더 시도
                got = _gpt_entries(img, img.read(img.size - SECTOR, SECTOR))
                src = "GPT(백업 헤더)"
            else:
                src = "GPT"
            if got:
                data, n, esz = got
                for i in range(n):
                    e = data[i * esz:(i + 1) * esz]
                    if len(e) < 128 or e[:16] == b"\x00" * 16:
                        continue
                    first, last = struct.unpack_from("<QQ", e, 0x20)
                    name = e[0x38:0x80].decode("utf-16-le", "replace").rstrip("\x00")
                    v = _candidate(img, first, last - first + 1,
                                   f"{src} 파티션 {len(vols) + 1} '{name}'",
                                   e[:16] == BASIC_DATA_GUID)
                    if v:
                        vols.append(v)
    if vols:
        return vols
    vols = scan_for_boot_sectors(img)
    if vols:
        return vols
    for lba in (63, 2048, 4096, 8192, 32768, 65536):      # 파티션 표가 없을 때 흔한 시작 위치의 FAT/exFAT
        v = _candidate(img, lba, max((img.size - lba * SECTOR) // SECTOR, 0), f"위치 추정 LBA {lba}", False)
        if v and v.fs != "NTFS":
            vols.append(v)
    if vols:
        return vols
    # 부트섹터(주/백업)도 파티션 정보도 없음 -> 이미지 전체를 대상으로 레코드 스캔
    return [Volume(0, img.size, "NTFS 구조를 찾지 못함 - 이미지 전체를 대상으로 MFT 레코드 스캔", False)]


def scan_for_boot_sectors(img) -> List[Volume]:
    """파티션 테이블이 없거나 깨졌을 때 NTFS 부트섹터 시그니처를 직접 찾는다.

    볼륨 끝의 백업 부트섹터만 남아 있는 경우에는 (total_sectors 로) 볼륨 시작 위치를 역산한다.
    """
    found: List[Volume] = []
    chunk = 16 * 1024 * 1024
    pos = 0
    while pos < img.size and len(found) < 16:
        data = img.read(pos, chunk + SECTOR)
        i = data.find(b"NTFS    ")
        while i != -1:
            sec_start = i - 3
            if sec_start >= 0 and sec_start % SECTOR == 0 and sec_start < chunk:
                sec = data[sec_start:sec_start + SECTOR]
                if is_ntfs_boot(sec):
                    hit = pos + sec_start
                    bps = struct.unpack_from("<H", sec, 0x0B)[0]
                    total = struct.unpack_from("<Q", sec, 0x28)[0]
                    start = hit - total * bps
                    if total == 0 or bps not in (512, 1024, 2048, 4096):
                        pass
                    elif start >= 0 and start != hit and start % SECTOR == 0 \
                            and is_ntfs_boot(img.read(start, SECTOR)):
                        pass  # 이미 찾았거나 찾을 정상 볼륨의 백업 부트섹터
                    elif start >= 0 and start % SECTOR == 0 \
                            and img.read(hit + total * bps, SECTOR) != sec:
                        found.append(Volume(start, (total + 1) * bps,
                                            "백업 부트섹터로부터 위치 역산 (주 부트섹터 손상)", False))
                    else:
                        found.append(Volume(hit, (total + 1) * bps,
                                            "부트섹터 시그니처 검색 결과", True))
            i = data.find(b"NTFS    ", i + 1)
        pos += chunk
    return found


# --------------------------------------------------------------------------
# NTFS 부트섹터
# --------------------------------------------------------------------------
@dataclass
class Boot:
    bps: int = 512
    spc: int = 8
    total_sectors: int = 0
    mft_lcn: Optional[int] = None
    mirr_lcn: Optional[int] = None
    record_size: int = 1024
    index_size: int = 4096
    ok: bool = False
    from_backup: bool = False
    problems: List[str] = field(default_factory=list)

    @property
    def cluster(self) -> int:
        return self.bps * self.spc


def parse_boot(sec: bytes, vol_size: int) -> Boot:
    b = Boot()
    if len(sec) < 512:
        b.problems.append("부트섹터를 읽을 수 없음")
        return b
    if sec[3:11] != b"NTFS    ":
        b.problems.append("OEM ID 가 'NTFS' 가 아님")
    if sec[510:512] != b"\x55\xaa":
        b.problems.append("부트 시그니처(55AA) 없음")
    bps, spc_raw = struct.unpack_from("<HB", sec, 0x0B)
    total, mft_lcn, mirr_lcn = struct.unpack_from("<QQQ", sec, 0x28)
    cpr = struct.unpack_from("<b", sec, 0x40)[0]
    ipr = struct.unpack_from("<b", sec, 0x44)[0]
    spc = spc_raw if spc_raw < 0x80 else 1 << (256 - spc_raw)

    if bps not in (512, 1024, 2048, 4096):
        b.problems.append(f"비정상 bytes/sector: {bps}")
    if spc == 0 or spc & (spc - 1):
        b.problems.append(f"비정상 sectors/cluster: {spc_raw}")
    if not b.problems:
        b.bps, b.spc, b.total_sectors = bps, spc, total
        cluster = bps * spc
        rs = cpr * cluster if cpr > 0 else 1 << (-cpr)
        if rs not in (512, 1024, 2048, 4096):
            b.problems.append(f"비정상 MFT 레코드 크기: {rs}")
        else:
            b.record_size = rs
        isz = ipr * cluster if ipr > 0 else 1 << (-ipr)
        if isz in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
            b.index_size = isz
        if total * bps > vol_size + 4096:
            b.problems.append("부트섹터의 총 섹터 수가 실제 볼륨 크기보다 큼")
        if mft_lcn * cluster >= vol_size:
            b.problems.append("$MFT 위치가 볼륨 범위를 벗어남")
        else:
            b.mft_lcn = mft_lcn
        if mirr_lcn * cluster < vol_size and mirr_lcn:
            b.mirr_lcn = mirr_lcn
    b.ok = not b.problems
    return b


def load_boot(img, vol: Volume) -> Boot:
    boot = parse_boot(img.read(vol.offset, SECTOR), vol.size)
    if boot.ok:
        return boot
    # 볼륨의 마지막 섹터에 있는 백업 부트섹터 시도
    backup = parse_boot(img.read(vol.offset + vol.size - SECTOR, SECTOR), vol.size)
    if backup.ok:
        backup.from_backup = True
        backup.problems = ["주 부트섹터 손상 -> 백업 부트섹터 사용: " + "; ".join(boot.problems)]
        return backup
    boot.problems.append("백업 부트섹터도 사용할 수 없음")
    return boot


# --------------------------------------------------------------------------
# MFT 레코드 / 폴더 인덱스(INDX) 파싱
# --------------------------------------------------------------------------
Runs = List[Tuple[Optional[int], int]]


@dataclass
class Entry:
    recno: int
    seq: int
    lsn: int
    in_use: bool
    is_dir: bool
    base: int  # 확장 레코드면 기본 레코드 번호(하위 48비트)
    torn: bool = False  # fixup 불일치 (섹터 일부 손상)
    parent: Optional[int] = None
    parent_seq: Optional[int] = None   # 부모 참조에 기록된 순번
    name: Optional[str] = None
    name_ns: int = 0
    size: int = 0
    mtime: Optional[datetime.datetime] = None
    mft_time: Optional[datetime.datetime] = None   # $STANDARD_INFORMATION 의 'MFT 변경 시각'(삭제하면 보통 갱신됨 -> 삭제 추정 시각)
    runs: Runs = field(default_factory=list)
    data_nonresident: bool = False
    data_size_known: bool = False
    has_data_attr: bool = False
    data_flags: int = 0       # 0x0001 압축, 0x4000 암호화(EFS), 0x8000 sparse
    attrlist: bool = False    # $ATTRIBUTE_LIST 보유 (속성 일부가 확장 레코드에 있음)
    source: str = "MFT"       # MFT / SCAN / INDEX (+INDEX)
    issues: List[str] = field(default_factory=list)
    risk: int = 0             # 0 정상, 1 의심, 2 손상 추정
    base_seq: int = 0         # 확장 레코드가 기록한 기본 레코드 순번
    is_ext: bool = False      # 다른 레코드의 확장 레코드인가
    data_frags: List[Tuple[int, Runs]] = field(default_factory=list)  # (시작 VCN, run 목록)
    attrlist_ents: Optional[list] = None   # 상주 $ATTRIBUTE_LIST 항목
    attrlist_nr: Optional[tuple] = None    # 비상주 $ATTRIBUTE_LIST (runs, size)
    merged_ext: int = 0       # 병합한 확장 레코드 수
    offset: Optional[int] = None  # 스캔으로 찾은 위치(볼륨 시작 기준 바이트)
    outside_mft: bool = False     # 현재 $MFT 영역 밖에서 발견됨
    pos_recno: Optional[int] = None  # 레코드가 놓인 위치 번호 (레코드 내장 번호와 다를 때만 기록)
    mft_cont: bool = False        # 선언된 $MFT 밖이지만 같은 MFT 의 연장 구간(번호가 이어짐)에서 발견됨
    iroot: Optional[list] = None  # 폴더의 $INDEX_ROOT(상주 인덱스)에 든 자식 항목 [IdxEnt]. None=확인 안 함
    ov_risk: int = 0              # 클러스터 겹침에서 온 위험도
    base_risk: int = 0            # 겹침을 제외한 위험도
    sig: str = ""                 # --check-data 시그니처 판정: ok / bad / unk
    xovl: Optional[float] = None  # 이전 세대 파일: 현재 세대 파일이 사용 중인 클러스터 비율(0~1)


@dataclass
class IdxEnt:
    recno: int
    seq: int
    parent: int
    name: str
    ns: int
    is_dir: bool
    size: int
    mtime: Optional[datetime.datetime]
    lsn: int
    parent_seq: Optional[int] = None


def upgrade_entry(e: Entry):
    """구버전 캐시에서 불러온 Entry 에 새 필드를 채운다."""
    for f in dataclasses.fields(Entry):
        if not hasattr(e, f.name):
            if f.default is not dataclasses.MISSING:
                setattr(e, f.name, f.default)
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                setattr(e, f.name, f.default_factory())  # type: ignore[misc]
    if not e.data_frags and e.runs:
        e.data_frags = [(0, list(e.runs))]


def filetime(ft: int) -> Optional[datetime.datetime]:
    if not ft:
        return None
    try:
        return datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)
    except OverflowError:
        return None


def decode_runs(b: bytes) -> Runs:
    runs = []
    i, lcn = 0, 0
    while i < len(b) and b[i]:
        h = b[i]
        ln, of = h & 0x0F, h >> 4
        i += 1
        if ln == 0 or i + ln + of > len(b):
            break
        length = int.from_bytes(b[i:i + ln], "little")
        i += ln
        if of == 0:
            runs.append((None, length))  # sparse
        else:
            lcn += int.from_bytes(b[i:i + of], "little", signed=True)
            runs.append((lcn, length))
        i += of
    return runs


def chain_runs(frags: List[Tuple[int, Runs]]) -> Runs:
    """시작 VCN 순으로 이어 붙인다. 중간 조각이 없으면 거기서 멈춘다."""
    runs: Runs = []
    vcn = 0
    for sv, rr in sorted(frags, key=lambda f: f[0]):
        if sv > vcn:
            break
        if sv < vcn:
            continue
        runs.extend(rr)
        vcn += sum(ln for _, ln in rr)
    return runs


def parse_attrlist(b: bytes) -> list:
    """$ATTRIBUTE_LIST 내용 -> [(속성 종류, 시작 VCN, 레코드 번호, 레코드 순번, 이름 길이)]"""
    out = []
    i = 0
    while i + 0x1A <= len(b):
        atype, rl, nl, _no = struct.unpack_from("<IHBB", b, i)
        if rl < 0x1A or i + rl > len(b):
            break
        svcn, ref = struct.unpack_from("<QQ", b, i + 8)
        out.append((atype, svcn, ref & 0xFFFFFFFFFFFF, ref >> 48, nl))
        i += rl
    return out


_NS_PRIORITY = {1: 3, 3: 3, 0: 2, 2: 1}  # Win32, Win32+DOS > POSIX > DOS


def apply_fixups(buf: bytearray, usa_off: int, usa_cnt: int) -> bool:
    """update sequence 복원. 모두 일치하면 True, 하나라도 어긋나면 False (일치하는 것만 복원)."""
    n = len(buf)
    usn = bytes(buf[usa_off:usa_off + 2])
    ok = True
    for i in range(1, usa_cnt):
        end = i * 512
        if end > n:
            break
        if bytes(buf[end - 2:end]) == usn:
            buf[end - 2:end] = buf[usa_off + 2 * i:usa_off + 2 * i + 2]
        else:
            ok = False
    return ok


def parse_record(raw: bytes, recno_hint: Optional[int]) -> Optional[Entry]:
    """유효한 FILE 레코드면 Entry, 아니면 None. 섹터 일부가 깨져도 가능한 만큼 읽는다."""
    n = len(raw)
    if n < 512 or raw[:4] != b"FILE":
        return None
    usa_off, usa_cnt = struct.unpack_from("<HH", raw, 4)
    if not (0x2A <= usa_off <= 0x40) or usa_cnt != n // 512 + 1:
        return None
    lsn, seq, _links, first_attr, flags, used, alloc, base_raw = struct.unpack_from("<QHHHHIIQ", raw, 8)
    if first_attr < 0x18 or first_attr >= n or used > n or used < first_attr or alloc > n:
        return None
    recno = struct.unpack_from("<I", raw, 0x2C)[0] if usa_off >= 0x30 else recno_hint
    if recno is None:
        return None

    buf = bytearray(raw)
    torn = not apply_fixups(buf, usa_off, usa_cnt)

    e = Entry(recno=recno, seq=seq, lsn=lsn, in_use=bool(flags & 1), is_dir=bool(flags & 2),
              base=base_raw & 0xFFFFFFFFFFFF, torn=torn, base_seq=base_raw >> 48, is_ext=base_raw != 0)
    best_prio = 0
    si_mtime = None
    off = first_attr
    limit = min(used, n)
    while off + 16 <= limit:
        atype, alen = struct.unpack_from("<II", buf, off)
        if atype == 0xFFFFFFFF:
            break
        if alen < 16 or alen % 8 or off + alen > n:
            e.torn = True
            break
        nonres = buf[off + 8]
        nlen = buf[off + 9]
        if atype == 0x20:
            e.attrlist = True
        if not nonres:
            csize, coff = struct.unpack_from("<IH", buf, off + 0x10)
            c = off + coff
            if atype == 0x10 and c + 0x10 <= n:  # $STANDARD_INFORMATION
                si_mtime = filetime(struct.unpack_from("<Q", buf, c + 8)[0])
                if c + 0x18 <= n:
                    e.mft_time = filetime(struct.unpack_from("<Q", buf, c + 0x10)[0])
            elif atype == 0x20 and c + csize <= n:  # $ATTRIBUTE_LIST (상주)
                e.attrlist_ents = parse_attrlist(bytes(buf[c:c + csize]))
            elif atype == 0x30 and c + 0x42 <= n:  # $FILE_NAME
                parent_ref = struct.unpack_from("<Q", buf, c)[0]
                mtime_raw = struct.unpack_from("<Q", buf, c + 0x10)[0]
                real = struct.unpack_from("<Q", buf, c + 0x30)[0]
                fnlen, ns = buf[c + 0x40], buf[c + 0x41]
                prio = _NS_PRIORITY.get(ns, 0)
                if prio > best_prio and c + 0x42 + fnlen * 2 <= n:
                    best_prio = prio
                    e.parent = parent_ref & 0xFFFFFFFFFFFF
                    e.parent_seq = parent_ref >> 48
                    e.name = buf[c + 0x42:c + 0x42 + fnlen * 2].decode("utf-16-le", "replace")
                    e.name_ns = ns
                    if not e.data_size_known:
                        e.size = real
                    if si_mtime is None:
                        e.mtime = filetime(mtime_raw)
            elif atype == 0x90 and csize >= 0x20 and c + csize <= n:  # $INDEX_ROOT (상주 인덱스)
                node = c + 16
                eo, tot = struct.unpack_from("<II", buf, node)
                e.iroot = _parse_index_entries(buf, node + eo, min(node + tot, c + csize), n, lsn)
            elif atype == 0x80 and nlen == 0:  # 이름 없는 $DATA (resident)
                e.size, e.data_size_known, e.has_data_attr = csize, True, True
                e.data_flags = struct.unpack_from("<H", buf, off + 12)[0]
        else:
            if atype == 0x80 and nlen == 0:
                start_vcn = struct.unpack_from("<Q", buf, off + 0x10)[0]
                runoff = struct.unpack_from("<H", buf, off + 0x20)[0]
                real = struct.unpack_from("<Q", buf, off + 0x30)[0]
                e.data_nonresident = True
                e.has_data_attr = True
                e.data_flags = struct.unpack_from("<H", buf, off + 12)[0]
                e.data_frags.append((start_vcn, decode_runs(bytes(buf[off + runoff:off + alen]))))
                if start_vcn == 0:  # 크기 필드는 첫 조각에만 유효하다
                    e.size, e.data_size_known = real, True
            elif atype == 0x20:  # $ATTRIBUTE_LIST (비상주)
                runoff = struct.unpack_from("<H", buf, off + 0x20)[0]
                real = struct.unpack_from("<Q", buf, off + 0x30)[0]
                e.attrlist_nr = (decode_runs(bytes(buf[off + runoff:off + alen])), real)
        off += alen
    if si_mtime is not None:
        e.mtime = si_mtime
    e.runs = chain_runs(e.data_frags)
    return e


def _parse_index_entries(buf, off: int, end: int, limit: int, lsn: int) -> List[IdxEnt]:
    out: List[IdxEnt] = []
    while off + 16 <= end:
        ref, elen, klen, eflags = struct.unpack_from("<QHHH", buf, off)
        if elen < 16 or elen % 8 or off + elen > end:
            break
        if eflags & 2:  # 마지막(빈) 항목
            break
        k = off + 16
        if klen >= 0x42 and k + klen <= limit:
            fnlen, ns = buf[k + 0x40], buf[k + 0x41]
            if fnlen and ns <= 3 and 0x42 + fnlen * 2 <= klen:
                parent_ref = struct.unpack_from("<Q", buf, k)[0]
                mtime_raw = struct.unpack_from("<Q", buf, k + 0x10)[0]
                real = struct.unpack_from("<Q", buf, k + 0x30)[0]
                fflags = struct.unpack_from("<I", buf, k + 0x38)[0]
                name = bytes(buf[k + 0x42:k + 0x42 + fnlen * 2]).decode("utf-16-le", "replace")
                out.append(IdxEnt(recno=ref & 0xFFFFFFFFFFFF, seq=ref >> 48,
                                  parent=parent_ref & 0xFFFFFFFFFFFF, name=name, ns=ns,
                                  is_dir=bool(fflags & 0x10000000), size=real,
                                  mtime=filetime(mtime_raw), lsn=lsn, parent_seq=parent_ref >> 48))
        off += elen
    return out


def parse_indx(raw: bytes) -> Optional[Tuple[int, List[IdxEnt]]]:
    """폴더 인덱스 블록(INDX). 각 항목은 자식의 $FILE_NAME 사본(이름/부모/폴더 여부/크기)을 담고 있다."""
    n = len(raw)
    if n < 512 or raw[:4] != b"INDX":
        return None
    usa_off, usa_cnt = struct.unpack_from("<HH", raw, 4)
    if not (0x28 <= usa_off <= 0x40) or usa_cnt != n // 512 + 1:
        return None
    lsn = struct.unpack_from("<Q", raw, 8)[0]
    ent_off, idx_len = struct.unpack_from("<II", raw, 0x18)
    if ent_off < 0x10 or 0x18 + ent_off >= n or 0x18 + idx_len > n or idx_len < ent_off:
        return None
    buf = bytearray(raw)
    apply_fixups(buf, usa_off, usa_cnt)
    out: List[IdxEnt] = []
    off = 0x18 + ent_off
    end = 0x18 + idx_len
    while off + 16 <= end:
        ref, elen, klen, eflags = struct.unpack_from("<QHHH", buf, off)
        if elen < 16 or elen % 8 or off + elen > end:
            break
        if eflags & 2:  # 마지막(빈) 항목
            break
        k = off + 16
        if klen >= 0x42 and k + klen <= n:
            fnlen, ns = buf[k + 0x40], buf[k + 0x41]
            if fnlen and ns <= 3 and 0x42 + fnlen * 2 <= klen:
                parent_ref = struct.unpack_from("<Q", buf, k)[0]
                mtime_raw = struct.unpack_from("<Q", buf, k + 0x10)[0]
                real = struct.unpack_from("<Q", buf, k + 0x30)[0]
                fflags = struct.unpack_from("<I", buf, k + 0x38)[0]
                name = bytes(buf[k + 0x42:k + 0x42 + fnlen * 2]).decode("utf-16-le", "replace")
                out.append(IdxEnt(recno=ref & 0xFFFFFFFFFFFF, seq=ref >> 48,
                                  parent=parent_ref & 0xFFFFFFFFFFFF, name=name, ns=ns,
                                  is_dir=bool(fflags & 0x10000000), size=real,
                                  mtime=filetime(mtime_raw), lsn=lsn, parent_seq=parent_ref >> 48))
        off += elen
    return lsn, out


# --------------------------------------------------------------------------
# 레코드 수집: (1) $MFT 를 끝까지 읽기  (2) 볼륨 전체 스캔 (FILE + INDX)
# --------------------------------------------------------------------------
@dataclass
class Stats:
    mode: str = ""
    mft_total: int = 0
    valid: int = 0
    empty: int = 0
    bad: int = 0  # 시그니처 불일치/BAAD/검증 실패 (0 으로만 채워진 것 제외)
    torn: int = 0
    ext_records: int = 0
    index_blocks: int = 0
    index_found: int = 0
    index_added: int = 0
    index_filled: int = 0
    dup_records: int = 0          # 스캔에서 같은 번호의 레코드가 이미 있었던 횟수
    mft_expected: int = 0         # $MFT 가 선언한 레코드 수
    mft_extents: int = 0          # 확인한 data run 개수
    mft_ext_used: int = 0         # $ATTRIBUTE_LIST 로 따라간 확장 레코드 수
    mft_runs: list = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    empty_set: Set[int] = field(default_factory=set)
    bad_set: Set[int] = field(default_factory=set)
    pos_mismatch: int = 0                                   # 내장 번호 != 위치 인 레코드 수
    mismatch_samples: list = field(default_factory=list)    # (위치, 내장 번호, 이름)
    mismatch_deltas: dict = field(default_factory=dict)     # (내장 - 위치) -> 개수
    probe: Optional[dict] = None                            # 선언된 $MFT 끝 이후 탐사 결과
    tail: dict = field(default_factory=dict)                # 탐사에서 발견한 레코드 {번호: Entry}
    extras: list = field(default_factory=list)              # 스캔에서 같은 번호가 다른 위치(다른 세대)에도 있던 레코드


def read_by_runs(img, vol: Volume, boot: Boot, runs, total_size: int, step: int):
    """data run 을 따라 (논리 오프셋, 데이터) 청크를 순서대로 내보낸다."""
    cluster = boot.cluster
    remaining, base_pos = total_size, 0
    for lcn, length in runs:
        take = min(length * cluster, remaining)
        pos = 0
        while pos < take:
            chunk = min(step, take - pos)
            if lcn is None:
                data = b"\x00" * chunk
            else:
                data = img.read(vol.offset + lcn * cluster + pos, chunk)
            yield base_pos + pos, data
            pos += chunk
        base_pos += take
        remaining -= take
        if remaining <= 0:
            return


def read_mft_via_runs(img, vol: Volume, boot: Boot, runs, total_size: int):
    rs = boot.record_size
    step = 4 * 1024 * 1024 // rs * rs
    for lpos, data in read_by_runs(img, vol, boot, runs, total_size, step):
        for k in range(0, len(data) - rs + 1, rs):
            yield (lpos + k) // rs, data[k:k + rs]


def locate(runs: Runs, off: int, cluster: int) -> Optional[int]:
    """논리 바이트 위치 -> 볼륨 안의 실제 위치. run 밖이거나 sparse 면 None."""
    pos = 0
    for lcn, ln in runs:
        span = ln * cluster
        if off < pos + span:
            return None if lcn is None else lcn * cluster + (off - pos)
        pos += span
    return None


def get_mft_layout(img, vol: Volume, boot: Boot):
    """레코드 0 에서 $MFT 의 data run 전체를 얻는다. 조각난 MFT 는 $ATTRIBUTE_LIST 가 가리키는
    확장 레코드에 나머지 run 이 들어 있으므로 그것까지 따라간다."""
    notes: List[str] = []
    e0 = None
    for which, lcn in (("main", boot.mft_lcn), ("mirr", boot.mirr_lcn)):
        if lcn is None:
            continue
        raw = img.read(vol.offset + lcn * boot.cluster, boot.record_size)
        e = parse_record(raw, 0)
        if e is not None and e.name == "$MFT" and e.data_frags:
            e0 = e
            if which == "mirr":
                notes.append("$MFT 레코드 0 이 손상되어 $MFTMirr 의 사본으로 data run 을 얻음"
                             " (사본에는 첫 구간만 있을 수 있음)")
            break
    if e0 is None:
        raise RuntimeError("$MFT 레코드 0 을 읽을 수 없음 ($MFTMirr 도 실패)")

    cluster, rs = boot.cluster, boot.record_size
    frags: Dict[int, Runs] = {}
    for sv, rr in e0.data_frags:
        frags.setdefault(sv, rr)

    ents = e0.attrlist_ents
    if ents is None and e0.attrlist_nr:
        ar, asz = e0.attrlist_nr
        try:
            data = b"".join(d for _, d in read_by_runs(img, vol, boot, ar, asz, 1 << 20))[:asz]
            ents = parse_attrlist(data)
        except Exception:  # noqa: BLE001
            notes.append("$MFT 의 $ATTRIBUTE_LIST 를 읽지 못함")
    used = 0
    if ents:
        pending = {rn for atype, _sv, rn, _s, nl in ents if atype == 0x80 and nl == 0 and rn != 0}
        progress = True
        while pending and progress:
            progress = False
            cur = chain_runs(list(frags.items()))
            for rn in sorted(pending):
                pos = locate(cur, rn * rs, cluster)
                if pos is None:
                    continue
                pending.discard(rn)
                progress = True
                x = parse_record(img.read(vol.offset + pos, rs), rn)
                if x is None or not x.is_ext or x.base != 0:
                    notes.append(f"$MFT 확장 레코드 #{rn} 가 손상되어 읽지 못함")
                    continue
                used += 1
                for sv, rr in x.data_frags:
                    frags.setdefault(sv, rr)
        for rn in sorted(pending):
            notes.append(f"$MFT 확장 레코드 #{rn} 의 위치가 확인된 구간 밖이라 읽지 못함")
    runs = chain_runs(list(frags.items()))
    return runs, e0.size, notes, used


def merge_extensions(entries: Dict[int, Entry], exts: Dict[int, List[Entry]]) -> int:
    """확장 레코드에 들어 있는 $DATA 조각(과 빠진 이름)을 기본 레코드에 합친다."""
    merged = 0
    for base_rec, lst in exts.items():
        b = entries.get(base_rec)
        if b is None:
            continue
        for x in lst:
            if x.base_seq not in (0, b.seq) and x.base_seq + 1 != b.seq:
                continue  # 다른 세대의 확장 레코드
            if x.data_frags:
                b.data_frags.extend(x.data_frags)
                b.data_nonresident = b.has_data_attr = True
                if x.data_size_known and not b.data_size_known:
                    b.size, b.data_size_known = x.size, True
                    b.data_flags = x.data_flags
            if b.name is None and x.name:
                b.name, b.parent, b.parent_seq, b.name_ns = x.name, x.parent, x.parent_seq, x.name_ns
                b.mtime = b.mtime or x.mtime
                b.mft_time = b.mft_time or x.mft_time
            b.merged_ext += 1
            merged += 1
        b.runs = chain_runs(b.data_frags)
    return merged


def probe_beyond(img, vol: Volume, boot: Boot, runs: Runs, declared: int, st: Stats, known: Dict[int, Entry]):
    """선언된 $MFT 끝 바로 뒤(물리적으로 이어지는 영역)에 FILE 레코드가 계속되는지 확인한다.
    계속된다면 레코드 0 의 크기가 오래된 값이거나, 이전 MFT 의 꼬리가 남아 있는 것이다."""
    rs, cl = boot.record_size, boot.cluster
    last = None
    for l, ln in runs:
        if l is not None:
            last = (l + ln) * cl
    if last is None:
        return
    vend = min(vol.size, max(0, img.size - vol.offset))
    chunk = 4 * 1024 * 1024 // rs * rs
    cap = 3_000_000  # 최대 탐사 레코드 수
    slot = sum(ln for _, ln in runs) * cl // rs  # run 끝에 해당하는 레코드 번호
    declared = slot
    pos, empty_chunks = last, 0
    info = {"valid": 0, "contig": 0, "min": None, "max": None, "deltas": {}}
    tail: Dict[int, Entry] = {}
    while pos + rs <= vend and slot - declared < cap:
        data = img.read(vol.offset + pos, min(chunk, vend - pos))
        if not data:
            break
        found = 0
        for k in range(0, len(data) - rs + 1, rs):
            if data[k:k + 4] != b"FILE":
                continue
            here = slot + k // rs
            e = parse_record(data[k:k + rs], here)
            if e is None or e.is_ext:
                continue
            found += 1
            info["valid"] += 1
            if e.recno == here:
                info["contig"] += 1
            else:
                d = e.recno - here
                info["deltas"][d] = info["deltas"].get(d, 0) + 1
                e.pos_recno = here
            info["min"] = e.recno if info["min"] is None else min(info["min"], e.recno)
            info["max"] = e.recno if info["max"] is None else max(info["max"], e.recno)
            e.source, e.offset, e.outside_mft = "TAIL", pos + k, True
            if e.recno not in known:
                old = tail.get(e.recno)
                if old is None or e.lsn > old.lsn:
                    tail[e.recno] = e
        empty_chunks = empty_chunks + 1 if found == 0 else 0
        if empty_chunks >= 2:  # 연속 8MiB 동안 레코드가 없으면 끝난 것으로 본다
            break
        pos += len(data)
        slot += len(data) // rs
    st.probe, st.tail = info, tail


def load_via_mft(img, vol: Volume, boot: Boot, probe: bool = True) -> Tuple[Dict[int, Entry], Stats]:
    st = Stats(mode="mft")
    if boot.mft_lcn is None:
        raise RuntimeError("부트섹터에서 $MFT 위치를 알 수 없음")
    runs, size, notes, used = get_mft_layout(img, vol, boot)
    rs = boot.record_size
    covered = sum(ln for _, ln in runs) * boot.cluster
    st.notes += notes
    st.mft_runs, st.mft_extents, st.mft_ext_used = runs, len(runs), used
    st.mft_expected = size // rs
    if covered < size:
        st.notes.append(f"$MFT 선언 크기({size // rs}개 레코드)보다 확인된 구간({covered // rs}개)이 짧음"
                        " -> 나머지 구간은 읽지 못함")
    entries: Dict[int, Entry] = {}
    exts: Dict[int, List[Entry]] = {}
    prog = Progress(min(size, covered), "MFT 읽기")
    for recno, raw in read_mft_via_runs(img, vol, boot, runs, size):
        st.mft_total += 1
        if st.mft_total % 8192 == 0:
            prog.update(st.mft_total * rs, f"유효 {st.valid}")
        if raw.count(0) == len(raw):
            st.empty += 1
            if len(st.empty_set) < 200000:
                st.empty_set.add(recno)
            continue
        e = parse_record(raw, recno)
        if e is None:
            st.bad += 1
            if len(st.bad_set) < 200000:
                st.bad_set.add(recno)
            continue
        # 레코드가 자기 번호(내장 번호)와 다른 위치에 놓여 있으면 '옮겨진/복사된 레코드'다.
        # 폴더 참조는 번호로 찾아가므로, 이런 레코드는 위치가 아니라 내장 번호로 취급한다.
        if e.recno != recno:
            st.pos_mismatch += 1
            d = e.recno - recno
            st.mismatch_deltas[d] = st.mismatch_deltas.get(d, 0) + 1
            if len(st.mismatch_samples) < 60:
                st.mismatch_samples.append((recno, e.recno, e.name))
            e.pos_recno = recno
        st.valid += 1
        st.torn += e.torn
        if e.is_ext:
            st.ext_records += 1
            exts.setdefault(e.base, []).append(e)
            continue
        old = entries.get(e.recno)
        if old is None or (e.pos_recno is None, e.lsn) > (old.pos_recno is None, old.lsn):
            entries[e.recno] = e
    prog.done(f"유효 {st.valid}")
    merge_extensions(entries, exts)
    if probe:
        print("MFT 끝 이후 영역 탐사 중...", file=sys.stderr, flush=True)
        probe_beyond(img, vol, boot, runs, st.mft_expected, st, entries)
    return entries, st


def load_via_scan(img, vol: Volume, boot: Boot, scan_range: Optional[Tuple[int, int]] = None
                  ) -> Tuple[Dict[int, Entry], Dict[Tuple[int, int], IdxEnt], Stats]:
    """볼륨 전체를 읽으며 FILE 레코드와 INDX 블록을 모은다."""
    st = Stats(mode="scan")
    rs = boot.record_size
    isz = boot.index_size
    overlap = max(rs, isz, 4096)
    entries: Dict[int, Entry] = {}
    exts: Dict[int, List[Entry]] = {}
    idx: Dict[Tuple[int, int], IdxEnt] = {}
    chunk = 16 * 1024 * 1024
    vend = min(vol.size, max(0, img.size - vol.offset))
    start, end = 0, vend
    if scan_range:
        start = max(0, scan_range[0]) // SECTOR * SECTOR
        end = min(vend, scan_range[1])
    prog = Progress(end - start, "스캔")
    pos = start
    while pos < end:
        data = img.read(vol.offset + pos, min(chunk + overlap, end - pos))
        limit = chunk if pos + chunk < end else len(data)

        i = data.find(b"FILE")
        while i != -1 and i < limit:
            if (pos + i) % SECTOR == 0 and i + rs <= len(data):
                e = parse_record(data[i:i + rs], None)
                if e is None:
                    st.bad += 1
                else:
                    st.valid += 1
                    st.torn += e.torn
                    e.offset = pos + i
                    if e.is_ext:
                        st.ext_records += 1
                        exts.setdefault(e.base, []).append(e)
                    else:
                        e.source = "SCAN"
                        old = entries.get(e.recno)
                        if old is not None:
                            st.dup_records += 1
                        if old is None or (e.lsn, e.in_use) > (old.lsn, old.in_use):
                            entries[e.recno] = e
                            if old is not None:
                                st.extras.append(old)
                        else:
                            st.extras.append(e)
            i = data.find(b"FILE", i + 1)

        i = data.find(b"INDX")
        while i != -1 and i < limit:
            if (pos + i) % SECTOR == 0 and i + isz <= len(data):
                res = parse_indx(data[i:i + isz])
                if res is not None:
                    st.index_blocks += 1
                    for ie in res[1]:
                        st.index_found += 1
                        key = (ie.recno, ie.seq)
                        old = idx.get(key)
                        if old is None or (_NS_PRIORITY.get(ie.ns, 0), ie.lsn) > (_NS_PRIORITY.get(old.ns, 0), old.lsn):
                            idx[key] = ie
            i = data.find(b"INDX", i + 1)

        pos += chunk
        prog.update(min(pos, end) - start, f"FILE {st.valid} / INDX {st.index_blocks}")
    prog.done(f"FILE {st.valid} / INDX {st.index_blocks}")
    merge_extensions(entries, exts)
    st.mft_total = st.valid + st.bad
    st.notes.append("스캔: 같은 레코드 번호가 여러 위치에 있으면 LSN 이 큰 것을 대표로 쓰고 나머지는 다른 세대 후보로 따로 보관")
    return entries, idx, st


def merge_index(entries: Dict[int, Entry], idx: Dict[Tuple[int, int], IdxEnt], st: Stats):
    """MFT 에 없는(또는 이름이 없는) 레코드를 폴더 인덱스 정보로 채운다."""
    by_rec: Dict[int, List[IdxEnt]] = {}
    for (rec, _seq), ie in idx.items():
        by_rec.setdefault(rec, []).append(ie)
    for rec, lst in by_rec.items():
        if rec == ROOT_RECNO:
            continue
        e = entries.get(rec)
        if e is None:
            ie = max(lst, key=lambda x: (x.seq, x.lsn))
            entries[rec] = Entry(recno=rec, seq=ie.seq, lsn=ie.lsn, in_use=True, is_dir=ie.is_dir, base=0,
                                 parent=ie.parent, parent_seq=ie.parent_seq, name=ie.name, name_ns=ie.ns,
                                 size=ie.size, mtime=ie.mtime, source="INDEX")
            st.index_added += 1
        elif e.name is None:
            match = [x for x in lst if x.seq == e.seq]
            if match:
                ie = max(match, key=lambda x: x.lsn)
                e.name, e.parent, e.parent_seq, e.name_ns = ie.name, ie.parent, ie.parent_seq, ie.ns
                e.mtime = e.mtime or ie.mtime
                if not e.data_size_known:
                    e.size = ie.size
                e.source += "+INDEX"
                st.index_filled += 1


# --------------------------------------------------------------------------
# 경로 재구성 (레코드 번호 + 순번 기반)
# --------------------------------------------------------------------------
_ORDER = {"ok": 0, "fixdir": 1, "prev": 1, "idx": 1, "idxl": 2, "weak": 3, "orphan": 4, "loop": 4}
LOST_REASON_TEXT = {
    "missing": "MFT 에서 해당 레코드를 찾지 못함",
    "empty": "MFT 레코드가 0 으로 비어 있음",
    "bad": "MFT 레코드가 손상됨(시그니처/구조 불일치)",
    "beyond": "읽은 MFT 범위 밖의 번호",
    "reused": "레코드가 다른 항목으로 재사용됨(순번 불일치) - 원래 폴더 레코드는 덮어쓰임",
    "notdir": "레코드가 폴더로 표시되어 있지 않음(순번은 일치) - 레코드 손상/플래그 파손",
    "notdir_noseq": "레코드가 폴더가 아님 - 자식 쪽에 순번 기록이 없어(구버전 캐시) 재사용 여부는 판별 불가",
    "noname": "폴더 레코드에 이름 정보가 없음",
    "loop": "부모 참조가 순환함",
    "othergen": "부모 번호의 레코드가 다른 세대(현재 $MFT)의 것 - 이전 세대의 원래 폴더 레코드는 덮어쓰여 사라짐",
    "gap": "MFT 연장 구간에서도 찾지 못한 번호(누락 구간: 레코드가 덮어쓰였거나 이미지에 없음)",
}


class PathResolver:
    """(레코드 번호, 순번) 참조를 따라 올라가며 경로를 만든다.

    같은 레코드 번호가 서로 다른 세대(옛 MFT 와 현재 MFT)에 있을 수 있으므로, 부모 후보를 모두 모은 뒤
    순번이 맞고 자식과 같은 세대(같은 MFT 구간)인 폴더를 우선 고른다.
    상태: ok(현재 MFT 와 일치) / prev(이전 세대 MFT 의 폴더로 복원) / idx(폴더 인덱스로 복원)
          / idxl(자식에 순번 기록이 없어 번호만으로 폴더 인덱스에서 추정) / weak(번호는 같은 폴더이나 순번 불일치)
          / orphan(복원 실패) / loop
    """

    def __init__(self, entries: Dict[int, Entry], idx: Dict[Tuple[int, int], IdxEnt], st: Optional[Stats],
                 extras: Optional[List[Entry]] = None, rec_size: int = 1024,
                 cur_ranges: Optional[List[Tuple[int, int]]] = None,
                 cont_bases: Optional[set] = None, gaps: Optional[List[Tuple[int, int]]] = None):
        self.entries, self.idx, self.st = entries, idx, st
        self.cont = cont_bases or set()
        self.gaps = gaps or []
        self.implied: Set[int] = set()
        self.cache: Dict[tuple, tuple] = {}
        self._byrec: Optional[Dict[int, List[IdxEnt]]] = None
        self.rs = rec_size
        self.cur_ranges = sorted(cur_ranges or [])
        self._starts = [r[0] for r in self.cur_ranges]
        self.dirs: Dict[int, List[Entry]] = {}
        for e in list(entries.values()) + list(extras or []):
            if e.is_dir and e.name:
                self.dirs.setdefault(e.recno, []).append(e)

    def grp(self, e: Entry):
        """레코드가 속한 세대/구간: 'CUR'(선언된 현재 $MFT), 'OLD'(번호가 이어지는 MFT 연장 구간),
        정수(그 밖의 흩어진 구간), None(알 수 없음)"""
        if e.offset is None:
            return "CUR" if e.source.startswith(("MFT", "TAIL")) else None
        if self._starts and in_ranges(self._starts, self.cur_ranges, e.offset):
            return "CUR"
        base = e.offset - e.recno * self.rs
        return "OLD" if base in self.cont else base

    def _idx_by_rec(self) -> Dict[int, List[IdxEnt]]:
        if self._byrec is None:
            d: Dict[int, List[IdxEnt]] = {}
            for (rec, _s), ie in self.idx.items():
                if ie.is_dir:
                    d.setdefault(rec, []).append(ie)
            self._byrec = d
        return self._byrec

    @staticmethod
    def seq_ok(e: Entry, ps: int) -> bool:
        # 삭제되면 순번이 1 증가하므로, 삭제된 폴더는 (참조 순번 + 1) 도 같은 세대로 본다
        return e.seq == ps or (not e.in_use and e.seq == ps + 1)

    @staticmethod
    def _compat(g, hint) -> bool:
        return hint is None or g is None or g == hint

    def lookup(self, key):
        n, ps, hint = key
        e = self.entries.get(n)
        cands = [c for c in self.dirs.get(n, []) if self._compat(self.grp(c), hint)]
        best = None
        if cands:
            best = max(cands, key=lambda c: (ps is None or self.seq_ok(c, ps), hint is not None and self.grp(c) == hint,
                                             self.grp(c) == "CUR", c.lsn))
        isdir = best is not None
        if isdir and (ps is None or self.seq_ok(best, ps)):
            g = self.grp(best)
            q = "prev" if isinstance(g, int) else "ok"
            return (q, best.name, (best.parent if best.parent is not None else ROOT_RECNO, best.parent_seq, g))
        if ps is not None:
            ie = self.idx.get((n, ps))
            if ie is not None and ie.is_dir:
                return ("idx", ie.name, (ie.parent, getattr(ie, "parent_seq", None), hint))
        else:  # 순번을 모르면 번호만으로 폴더 인덱스에서 찾는다(추정)
            ics = self._idx_by_rec().get(n)
            if ics:
                ie = max(ics, key=lambda x: (x.lsn, x.seq))
                return ("idxl", ie.name, (ie.parent, getattr(ie, "parent_seq", None), hint))
        if isdir:
            return ("weak", best.name, (best.parent if best.parent is not None else ROOT_RECNO, best.parent_seq,
                                        self.grp(best)))
        if e is not None and not self._compat(self.grp(e), hint):
            # 같은 번호의 폴더 레코드가 있고 순번도 맞는데 세대 표시만 어긋난 경우: 자식이 이전 세대(연장 구간)가
            # 아니라면 같은 폴더로 보고 약한 연결로 잇는다(순번 일치 + 세대 힌트 불일치).
            if (hint != "OLD" and e.is_dir and e.name and (ps is None or self.seq_ok(e, ps))):
                return ("weak", e.name, (e.parent if e.parent is not None else ROOT_RECNO, e.parent_seq, self.grp(e)))
            return ("lost", "othergen")   # 같은 번호의 레코드가 있지만 다른 세대의 것
        if (e is not None and e.name and not e.is_dir and ps is not None and n >= 16 and self.seq_ok(e, ps)
                and e.base == 0 and not e.is_ext):
            # 자식이 이 레코드(같은 순번)를 부모로 기록했으므로 폴더였던 레코드: 폴더 플래그만 지워진 것으로 본다
            self.implied.add(n)
            return ("fixdir", e.name, (e.parent if e.parent is not None else ROOT_RECNO, e.parent_seq, self.grp(e)))
        if e is None:
            st = self.st
            if st is not None:
                if any(a <= n <= b for a, b in self.gaps):
                    return ("lost", "gap")
                if n in st.bad_set:
                    return ("lost", "bad")
                if n in st.empty_set:
                    return ("lost", "empty")
                if st.mft_total and n >= st.mft_total:
                    return ("lost", "beyond")
            return ("lost", "missing")
        if not e.is_dir:
            if ps is None:
                return ("lost", "notdir_noseq")
            if not self.seq_ok(e, ps):
                return ("lost", "reused")
            return ("lost", "notdir")
        return ("lost", "noname")

    def resolve_dir(self, key) -> tuple:
        if key[0] == ROOT_RECNO:
            return ("", "ok", None, "")
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        chain, seen, cur = [], set(), key
        while True:
            if cur[0] == ROOT_RECNO:
                base = ("", "ok", None, "")
                break
            hit = self.cache.get(cur)
            if hit is not None:
                base = hit
                break
            if cur in seen or len(chain) > 256:
                base = (f"[순환참조_{cur[0]}]", "loop", cur, "loop")
                break
            seen.add(cur)
            info = self.lookup(cur)
            if info[0] == "lost":
                base = (f"[유실된폴더_{cur[0]}]", "orphan", cur, info[1])
                self.cache[cur] = base
                break
            chain.append((cur, info[1], info[0]))
            cur = info[2]
        path, stat, bn, rs = base
        for k, name, q in reversed(chain):
            path = f"{path}/{name}" if path else name
            if _ORDER[q] > _ORDER[stat]:
                stat = q
            self.cache[k] = (path, stat, bn, rs)
        return self.cache.get(key, base)

    def path_of(self, e: Entry) -> tuple:
        pn = e.parent if e.parent is not None else ROOT_RECNO
        ppath, pstat, bn, reason = self.resolve_dir((pn, e.parent_seq, self.grp(e)))
        return (f"{ppath}/{e.name}" if ppath else e.name), pstat, bn, reason

    def describe_lost(self, key, reason: str) -> str:
        n, ps = key[0], key[1]
        text = LOST_REASON_TEXT.get(reason, reason)
        e = self.entries.get(n)
        if e is not None and reason in ("reused", "notdir", "notdir_noseq", "othergen"):
            kind = "폴더" if e.is_dir else "파일"
            state = "사용 중" if e.in_use else "삭제됨"
            text += (f" -> 지금 이 번호는 '{e.name}' ({kind}, {state}), 순번 {e.seq}"
                     f" / 자식이 기록한 순번 {'없음' if ps is None else ps}"
                     f" / 폴더 레코드 세대 {self.grp(e)}, 출처 {e.source}")
        return text


# --------------------------------------------------------------------------
# 파일별 건강도 분석
# --------------------------------------------------------------------------
MAGIC: Dict[str, List[Tuple[int, bytes]]] = {}


def _reg(exts: str, *sigs: Tuple[int, bytes]):
    for x in exts.split():
        MAGIC[x] = list(sigs)


_reg("jpg jpeg jpe", (0, b"\xff\xd8\xff"))
_reg("png", (0, b"\x89PNG"))
_reg("gif", (0, b"GIF8"))
_reg("bmp", (0, b"BM"))
_reg("pdf", (0, b"%PDF"))
_reg("zip docx xlsx pptx hwpx jar apk odt ods odp epub", (0, b"PK\x03\x04"), (0, b"PK\x05\x06"))
_reg("7z", (0, b"7z\xbc\xaf"))
_reg("rar", (0, b"Rar!"))
_reg("exe dll sys scr", (0, b"MZ"))
_reg("doc xls ppt msg", (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))
_reg("hwp", (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), (0, b"HWP Document"))
_reg("mp4 mov m4a m4v 3gp heic", (4, b"ftyp"))
_reg("mp3", (0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2"), (0, b"\xff\xfa"))
_reg("avi wav webp", (0, b"RIFF"))
_reg("psd", (0, b"8BPS"))
_reg("tif tiff", (0, b"II*\x00"), (0, b"MM\x00*"))
_reg("mkv webm", (0, b"\x1a\x45\xdf\xa3"))
_reg("sqlite", (0, b"SQLite format 3"))


def count_set_bits(bm: bytes, a: int, b: int) -> int:
    """비트맵에서 클러스터 [a, b) 중 할당(1)된 개수. 비트맵 범위 밖은 미할당으로 본다."""
    b = min(b, len(bm) * 8)
    if a >= b:
        return 0
    s, e = a >> 3, ((b - 1) >> 3) + 1
    x = int.from_bytes(bm[s:e], "little") >> (a & 7)
    x &= (1 << (b - a)) - 1
    return x.bit_count() if hasattr(x, "bit_count") else bin(x).count("1")


def load_bitmap(img, vol: Volume, boot: Boot, entries: Dict[int, Entry]) -> Tuple[Optional[bytes], str]:
    e = entries.get(BITMAP_RECNO)
    if e is None:
        return None, "레코드 #6 이 없음"
    if e.name != "$Bitmap":
        return None, f"레코드 #6 의 이름이 '{e.name}' ($Bitmap 이 아님)"
    if not e.runs or not e.size:
        return None, "$Bitmap 의 데이터 위치(run) 를 알 수 없음"
    want = min(e.size, 512 * 1024 * 1024)
    out = bytearray()
    for _, data in read_by_runs(img, vol, boot, e.runs, want, 8 * 1024 * 1024):
        out += data
    if len(out) < min(want, 8):
        return None, "$Bitmap 내용을 읽지 못함"
    return bytes(out), ""


def _add(e: Entry, msg: str, risk: int, ovl: bool = False):
    e.issues.append(msg)
    e.risk = max(e.risk, risk)
    if ovl:
        e.ov_risk = max(e.ov_risk, risk)
    else:
        e.base_risk = max(e.base_risk, risk)


def analyze_health(entries: Dict[int, Entry], vol: Volume, boot: Boot, img, bitmap: Optional[bytes],
                   bitmap_why: str, check_data: bool, pstatus: Dict[int, str], have_mft: bool,
                   check_filter=None) -> List[str]:
    """각 Entry 의 issues/risk 를 채운다. 반환값은 전체 분석에 대한 안내문."""
    notes: List[str] = []
    geometry_ok = boot.ok
    cluster = boot.cluster
    total_clusters = vol.size // cluster if geometry_ok else 0
    if not geometry_ok:
        notes.append("부트섹터를 복구하지 못해 클러스터 크기를 알 수 없음 -> 데이터 위치 기반 검사 생략")
    if geometry_ok and bitmap is None:
        notes.append(f"$Bitmap 을 읽지 못해 클러스터 사용 여부(덮어쓰기 판정)를 생략함: {bitmap_why}")

    def good(r: int) -> bool:
        return pstatus.get(r, "orphan") in ("ok", "fixdir", "prev", "idx")

    # 1) 클러스터 겹침 검사 (교차 연결 / 삭제 후 덮어쓰기)
    hits: Dict[int, Set[int]] = {}
    split_gen = any(e.mft_cont for e in entries.values())   # 현재 세대 / 이전 세대(연장 구간)를 따로 본다

    def is_old(e: Entry) -> bool:
        return split_gen and (e.mft_cont or (have_mft and e.source.startswith("SCAN") and e.recno >= 4))

    xg_cnt: Counter = Counter()
    if geometry_ok:
        ivs: Dict[bool, list] = {False: [], True: []}
        for e in entries.values():
            if e.is_dir or not e.runs:
                continue
            for lcn, ln in e.runs:
                if lcn is not None and ln > 0:
                    ivs[is_old(e)].append((lcn, lcn + ln, e.recno))
        for old_flag, iv in ivs.items():   # 같은 세대끼리만 교차 연결을 따진다
            iv.sort()
            cur_end, cur_owner = -1, None
            for s, en, rec in iv:
                if s < cur_end and cur_owner is not None and cur_owner != rec:
                    hits.setdefault(rec, set()).add(cur_owner)
                    hits.setdefault(cur_owner, set()).add(rec)
                if en > cur_end:
                    cur_end, cur_owner = en, rec
        if split_gen:
            # 현재 세대 파일(사용 중)이 차지한 클러스터 구간을 합친 뒤, 이전 세대 파일이 얼마나 겹치는지 계산
            cur_iv = sorted((a, b) for a, b, _r in ivs[False] if entries[_r].in_use)
            merged: List[List[int]] = []
            for a, b in cur_iv:
                if merged and a <= merged[-1][1]:
                    if b > merged[-1][1]:
                        merged[-1][1] = b
                else:
                    merged.append([a, b])
            m_starts = [m[0] for m in merged]
            for e in entries.values():
                if e.is_dir or not e.runs or not is_old(e):
                    continue
                tot = ov = 0
                for lcn, ln in e.runs:
                    if lcn is None or ln <= 0:
                        continue
                    tot += ln
                    i = max(bisect_right(m_starts, lcn) - 1, 0)
                    while i < len(merged) and merged[i][0] < lcn + ln:
                        ov += max(0, min(merged[i][1], lcn + ln) - max(merged[i][0], lcn))
                        i += 1
                if tot:
                    e.xovl = ov / tot
                    xg_cnt["none" if ov == 0 else ("full" if ov >= 0.99 * tot else "part")] += 1
            if xg_cnt:
                notes.append(f"이전 세대(연장 구간) 파일 {sum(xg_cnt.values())}개 중 현재 세대 파일과 클러스터가 겹치지 않음 {xg_cnt['none']}개 / "
                             f"일부만 겹침 {xg_cnt['part']}개 / 거의 전부 겹침(현재 파일이 덮어씀) {xg_cnt['full']}개")

    if hits:
        mat: Counter = Counter()
        for r1, others_ in hits.items():
            a1 = entries.get(r1)
            if a1 is None or not a1.in_use:
                continue
            for r2 in others_:
                a2 = entries.get(r2)
                if a2 is None or not a2.in_use or r2 <= r1:
                    continue
                k1, k2 = ("연장" if a1.mft_cont else "현재"), ("연장" if a2.mft_cont else "현재")
                mat[tuple(sorted((k1, k2)))] += 1
        if mat:
            notes.append("클러스터가 겹치는 사용 중 파일 쌍(중복 제외): " + ", ".join(
                f"{a}-{b} {c}쌍" for (a, b), c in mat.most_common()) + "  ('현재'=선언된 $MFT 안, '연장'=연장 구간)")
    bm_stat: Dict[int, Tuple[int, int]] = {}
    bm_unreliable = False
    cov = len(bitmap) * 8 if bitmap is not None else 0
    if bitmap is not None and geometry_ok:
        n_use = n_bad = 0
        for rec, e in entries.items():
            if e.is_dir or not e.runs or e.source == "INDEX":
                continue
            tot = alloc = 0
            for lcn, ln in e.runs:
                if lcn is None or lcn >= cov:
                    continue                      # 비트맵이 다루지 않는 범위는 판정에서 제외
                ln = min(ln, cov - lcn)
                tot += ln
                alloc += count_set_bits(bitmap, lcn, lcn + ln)
            if tot:
                bm_stat[rec] = (tot, alloc)
                if e.in_use:
                    n_use += 1
                    n_bad += alloc < tot
        if n_use and n_bad / n_use > 0.2:
            bm_unreliable = True
            notes.append(f"비트맵이 다루는 범위의 사용 중인 파일 {n_use}개 중 {n_bad}개({n_bad * 100 / n_use:.0f}%)가 비트맵상 미할당 -> "
                         "$Bitmap 이 현재 파일시스템 상태를 반영하지 못하는 것으로 보고 비트맵 기반 판정은 하지 않음 "
                         "([비트맵 점검] 참고). 비트맵상 미할당인 영역은 새 데이터가 덮어쓸 수 있으니 원본 디스크에 쓰기 금지")

    for rec, e in entries.items():
        if e.name is None and e.source == "MFT":
            continue
        # 인덱스에서만 복원된 항목
        if e.source == "INDEX":
            if e.is_dir:
                _add(e, "(참고) 폴더 이름만 인덱스에서 복원됨", 0)
            else:
                _add(e, "MFT 레코드 없음: 이름/크기만 인덱스에서 복원, 데이터 위치를 알 수 없어 복사 불가", 2)
            continue
        if e.is_dir:
            if e.torn:
                _add(e, "폴더 레코드 일부 손상", 1)
            continue

        if have_mft and e.source.startswith("TAIL"):
            _add(e, "선언된 $MFT 끝 뒤에 이어진 영역에서 발견됨(레코드 0 의 크기가 오래된 값이거나 이전 MFT 의 꼬리일 수 있음)", 1)
        elif have_mft and e.source.startswith("SCAN") and e.mft_cont:
            _add(e, "(참고) 이전 세대 레코드: 선언된 $MFT 밖의 연장 구간에서 복원(현재 세대와 별도로 할당된 파일시스템 상태)", 0)
        elif have_mft and e.source.startswith("SCAN"):
            if e.outside_mft:
                _add(e, "현재 $MFT 에 없고 $MFT 영역 밖에서 발견됨(이전 MFT/파일시스템의 잔재일 가능성)", 1)
            else:
                _add(e, "현재 $MFT 에 없고 디스크 스캔에서만 발견됨(이전 파일시스템의 잔재일 수 있음)", 1)
        if e.pos_recno is not None:
            _add(e, f"레코드가 자기 번호(#{e.recno})와 다른 위치(#{e.pos_recno})에 놓여 있음", 1)
        if e.torn:
            _add(e, "MFT 레코드 일부 손상(섹터 검증 불일치)", 1)
        if e.data_flags & 0x4000:
            _add(e, "EFS 암호화 파일(키 없이는 내용 복구 불가)", 1)

        if e.data_nonresident:
            if not e.runs:
                if e.size > 0:
                    if e.attrlist and not e.merged_ext:
                        _add(e, "데이터 위치가 확장 레코드에 있으나 그 레코드를 찾지 못함", 1)
                    else:
                        _add(e, "데이터 위치(런리스트) 없음 또는 손상", 2)
            elif geometry_ok:
                clusters = sum(ln for _, ln in e.runs)
                need = -(-e.size // cluster) if e.size else 0
                if clusters < need and not (e.data_flags & 0x8001):
                    if e.attrlist and not e.merged_ext:
                        _add(e, "데이터 위치 일부가 확장 레코드에 있으나 그 레코드를 찾지 못함", 1)
                    else:
                        _add(e, "데이터 위치 정보가 파일 크기보다 짧음(일부 유실)", 2)
                if any(l is not None and (l + ln) > total_clusters for l, ln in e.runs):
                    _add(e, "데이터 위치가 볼륨 범위를 벗어남", 2)
        elif not e.has_data_attr and e.size > 0:
            if e.attrlist and not e.merged_ext:
                _add(e, "데이터 속성이 확장 레코드에 있으나 그 레코드를 찾지 못함", 1)
            else:
                _add(e, "$DATA 속성을 찾을 수 없음", 1)

        if e.xovl is not None:
            if e.xovl == 0:
                _add(e, "(참고) 이전 세대 파일: 현재 세대 파일과 클러스터가 겹치지 않음(내용이 남아 있을 수 있으나 비트맵을 쓸 수 없어 확신은 못 함)", 1)
            else:
                _add(e, f"이전 세대 파일의 클러스터 {e.xovl * 100:.0f}%를 현재 세대 파일이 사용 중(덮어쓰여짐)",
                     2 if e.xovl >= 0.5 else 1, True)
        others = hits.get(rec)
        if others:
            oth_use = [o for o in others if o in entries and entries[o].in_use]
            if e.in_use and oth_use:
                e_good = good(rec)
                o_good = any(good(o) for o in oth_use)
                if e_good and o_good:
                    _add(e, "다른 파일과 같은 클러스터를 사용(교차 연결)", 2, True)
                elif e_good:
                    _add(e, "클러스터가 경로 복원 불가 항목과 겹침(상대가 이전 세대의 레코드일 가능성)", 1, True)
                elif o_good:
                    _add(e, "경로 복원 불가 항목인데 같은 클러스터를 더 일관된 파일이 사용 중(덮어쓰였을 가능성 높음)", 2, True)
                else:
                    _add(e, "경로 복원 불가 항목끼리 클러스터가 겹침(이전 세대 혼재 의심)", 1, True)
            elif not e.in_use and oth_use:
                _add(e, "삭제 후 다른 파일이 같은 클러스터를 사용(덮어쓰기됨)", 2)

        if bitmap is not None and e.runs and geometry_ok and rec in bm_stat:
            tot, alloc = bm_stat[rec]
            if tot:
                if e.in_use and alloc < tot:
                    if bm_unreliable:
                        continue
                    pct = (tot - alloc) * 100 / tot
                    _add(e, f"사용 중인 파일인데 클러스터 {pct:.0f}%가 비트맵상 미할당(불일치)", 1)
                elif not e.in_use:
                    pct = alloc * 100 / tot
                    if alloc:
                        _add(e, f"삭제 후 클러스터 {pct:.0f}%가 다른 용도로 재할당됨(복구 시 손상 가능)",
                             2 if pct >= 50 else 1)
                    elif not others:
                        _add(e, "(참고) 삭제됨 - 클러스터 재사용 흔적 없음(복구 가능성 높음)", 0)

    # 2) 파일 앞부분 확인 (선택)
    if check_data and geometry_ok:
        targets = [e for e in entries.values()
                   if not e.is_dir and e.runs and e.runs[0][0] is not None and e.size > 0
                   and not (e.data_flags & 0x8001) and e.source != "INDEX"
                   and (check_filter is None or check_filter(e))]
        targets.sort(key=lambda e: e.runs[0][0])  # 디스크 순서대로 읽어 탐색 시간을 줄인다
        prog = Progress(len(targets), "내용 확인", as_bytes=False)
        bad = 0
        for n, e in enumerate(targets, 1):
            head = img.read(vol.offset + e.runs[0][0] * cluster, 16)
            ext = e.name.rsplit(".", 1)[-1].lower() if e.name and "." in e.name else ""
            if len(head) < 16:
                e.sig = "bad"
                _add(e, "데이터 시작 위치를 읽을 수 없음(이미지 범위 밖)", 2)
            elif head == b"\x00" * 16:
                e.sig = "bad"
                _add(e, "파일 시작 부분이 0으로 채워져 있음(내용 유실 가능)", 2)
            elif ext in MAGIC:
                if any(head[o:o + len(s)] == s for o, s in MAGIC[ext]):
                    e.sig = "ok"
                else:
                    e.sig = "bad"
                    _add(e, f"파일 시그니처가 .{ext} 형식과 다름(덮어써졌거나 손상 가능)", 2)
            else:
                e.sig = "unk"
            if e.sig == "ok" and e.ov_risk:
                # 클러스터가 겹쳐도 시작 부분이 확장자와 맞으면 이 파일의 내용일 가능성이 높다
                e.risk = max(e.base_risk, 1)
                e.issues.append("(참고) 클러스터 겹침이 있으나 파일 시작 시그니처는 일치(앞부분만 확인한 것)")
            if e.risk == 2:
                bad += 1
            if n % 2000 == 0:
                prog.update(n, f"손상추정 {bad}건")
        prog.done("완료")
    return notes


RISK_LABEL = {0: "정상", 1: "의심", 2: "손상추정"}


# --------------------------------------------------------------------------
# 디스크 상태 판정
# --------------------------------------------------------------------------
def judge(boot: Boot, st: Stats, mft_failed: bool, orphans: int, total_named: int,
          recovered: int = 0, extra: Optional[List[Tuple[int, str]]] = None) -> Tuple[str, List[str]]:
    reasons = []
    level = 0  # 0 정상, 1 경미, 2 심각
    if not boot.ok:
        level = 2
        reasons += ["부트섹터: " + p for p in boot.problems]
    elif boot.from_backup:
        level = max(level, 1)
        reasons += boot.problems
    if mft_failed:
        level = 2
        reasons.append("$MFT 를 정상적으로 읽을 수 없어 스캔 모드로 전환함")
    if st.mode == "mft" and st.mft_total:
        bad_ratio = st.bad / max(st.valid + st.bad, 1)
        if st.bad:
            level = max(level, 2 if bad_ratio > 0.05 else 1)
            reasons.append(f"MFT 레코드 {st.bad}개가 유효하지 않음 ({bad_ratio:.3%})")
    if st.torn:
        level = max(level, 1)
        reasons.append(f"fixup 불일치(섹터 일부 손상) 레코드 {st.torn}개")
    for lv, text in (extra or []):
        level = max(level, lv)
        reasons.append(text)
    if total_named and recovered:
        ratio = recovered / total_named
        level = max(level, 2 if ratio > 0.05 else 1)
        reasons.append(f"MFT 에서 유실되어 폴더 인덱스로만 복원한 항목 {recovered}개 ({ratio:.3%})")
    if total_named and orphans:
        ratio = orphans / total_named
        level = max(level, 2 if ratio > 0.2 else 1)
        reasons.append(f"상위 폴더 경로를 복원하지 못한 항목 {orphans}개 ({ratio:.1%})")
    if not reasons:
        reasons.append("검사한 범위에서 이상 없음")
    return ["정상", "경미한 손상", "심각한 손상"][level], reasons


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 복사(복구): 폴더 단위 선택, 폴더 구조 유지, 손상 표시
# --------------------------------------------------------------------------
import calendar
import shutil

COPY_CHUNK = 4 * 1024 * 1024
_BAD_CH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
DMG_TAG = "[손상추정]"


# ---- 화면 색상 설정 (ANSI 코드; 여기만 고치면 전체에 적용됩니다) ----------------------
# 코드 예: 31=빨강 32=초록 33=노랑 36=시안, ;1 을 붙이면 굵게. 256색은 38;5;번호 (42=에메랄드 계열).
# 에메랄드가 안 보이거나 색이 이상하면 COLOR_FILE 을 "32" (기본 초록)로 바꾸세요.
COLOR_SUSPECT = "33;1"        # 의심: 노랑
COLOR_DAMAGED = "31;1"        # 손상 / 손상추정: 빨강
COLOR_FILE = "38;5;244"        # 정상 파일: 에메랄드
COLOR_FOLDER = "38;5;51"         # 폴더: 시안
# --------------------------------------------------------------------------------


class Paint:
    """ANSI 색상. 의심=노랑, 손상/손상추정=빨강, 파일=에메랄드, 폴더=시안 (위 COLOR_* 상수)."""

    def __init__(self, on: bool):
        self.on = on

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def red(self, t): return self(t, "31;1")
    def yel(self, t): return self(t, "33;1")
    def dim(self, t): return self(t, "2")
    def grn(self, t): return self(t, "32")
    def cyan(self, t): return self(t, "36;1")
    def sus(self, t): return self(t, COLOR_SUSPECT)
    def bad(self, t): return self(t, COLOR_DAMAGED)
    def file(self, t): return self(t, COLOR_FILE)
    def folder(self, t): return self(t, COLOR_FOLDER)


def enable_color(disabled: bool) -> bool:
    if disabled or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":   # cmd 에서 ANSI 색상(VT) 켜기
        try:
            import ctypes
            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = k.GetStdHandle(-11)
            m = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(m)):
                return False
            return bool(k.SetConsoleMode(h, m.value | 0x0004))
        except Exception:  # noqa: BLE001
            return False
    return True


def safe_component(name: str) -> str:
    n = _BAD_CH.sub("_", name).rstrip(" .")
    if not n:
        n = "_"
    if n.split(".")[0].upper() in _RESERVED:
        n = "_" + n
    if len(n) > 200:
        base, ext = os.path.splitext(n)
        n = base[:max(1, 200 - len(ext))] + ext
    return n


def tag_name(name: str, tag: str) -> str:
    base, ext = os.path.splitext(name)
    return f"{base} {tag}{ext}" if base else f"{tag}{ext}"


def winpath(p: str) -> str:
    """Windows 의 260자 경로 제한을 피하기 위한 \\\\?\\ 접두."""
    p = os.path.abspath(p)
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def record_phys(e: Entry, st_mft: Optional[Stats], boot: Boot) -> Optional[int]:
    """MFT 레코드가 놓인 볼륨 안의 위치(바이트)."""
    if e.offset is not None:
        return e.offset
    if st_mft is None or not st_mft.mft_runs:
        return None
    pos = e.pos_recno if e.pos_recno is not None else e.recno
    return locate(st_mft.mft_runs, pos * boot.record_size, boot.cluster)


def extract_resident(raw: bytes, recno: int, seq: int) -> Tuple[Optional[bytes], str]:
    """MFT 레코드에서 이름 없는 상주 $DATA 의 내용을 꺼낸다. (내용, 실패 사유)"""
    n = len(raw)
    if n < 512 or raw[:4] != b"FILE":
        return None, "레코드를 읽을 수 없음(FILE 시그니처 없음)"
    usa_off, usa_cnt = struct.unpack_from("<HH", raw, 4)
    if not (0x2A <= usa_off <= 0x40) or usa_cnt != n // 512 + 1:
        return None, "레코드 헤더 손상"
    rec_seq = struct.unpack_from("<H", raw, 0x10)[0]
    if usa_off >= 0x30 and struct.unpack_from("<I", raw, 0x2C)[0] != recno:
        return None, "그 위치의 레코드 번호가 다름"
    if rec_seq != seq:
        return None, "그 위치의 레코드가 다른 파일로 바뀜(순번 불일치)"
    buf = bytearray(raw)
    apply_fixups(buf, usa_off, usa_cnt)
    first = struct.unpack_from("<H", buf, 0x14)[0]
    used = min(struct.unpack_from("<I", buf, 0x18)[0], n)
    off = first
    while off + 16 <= used:
        atype, alen = struct.unpack_from("<II", buf, off)
        if atype == 0xFFFFFFFF or alen < 16 or alen % 8 or off + alen > n:
            break
        if atype == 0x80 and buf[off + 8] == 0 and buf[off + 9] == 0:
            csize, coff = struct.unpack_from("<IH", buf, off + 0x10)
            if off + coff + csize <= n:
                return bytes(buf[off + coff:off + coff + csize]), ""
            return None, "상주 데이터 길이가 레코드를 벗어남"
        off += alen
    return None, "상주 $DATA 속성을 찾지 못함"


class CopyResult:
    __slots__ = ("status", "written", "errs", "head", "note")

    def __init__(self, status="", written=0, errs=0, head=b"", note=""):
        self.status, self.written, self.errs, self.head, self.note = status, written, errs, head, note


def copy_one(img, vol: Volume, boot: Boot, e: Entry, st_mft: Optional[Stats], tmp_path: str,
             on_bytes=None) -> CopyResult:
    """파일 하나의 내용을 tmp_path 에 쓴다. 이미지는 읽기만 한다.
    status: ok / partial(읽기 오류·run 부족 있음) / fail:사유"""
    if e.source == "INDEX":
        return CopyResult("fail", note="MFT 레코드가 없어 데이터 위치를 알 수 없음")
    if e.data_flags & 0x0001:
        return CopyResult("fail", note="NTFS 압축 파일(미지원)")
    if e.data_flags & 0x4000:
        return CopyResult("fail", note="EFS 암호화 파일(복호화 불가)")
    cl = boot.cluster
    dbase = getattr(boot, "data_base", 0)
    errs0 = img.read_errors
    short = 0
    head = b""
    note = ""
    try:
        with open(winpath(tmp_path), "wb") as f:
            if not e.has_data_attr and e.size == 0:
                pass
            elif not e.data_nonresident:
                pos = record_phys(e, st_mft, boot)
                if pos is None:
                    return CopyResult("fail", note="레코드 위치를 알 수 없음")
                data, why = extract_resident(img.read(vol.offset + pos, boot.record_size), e.recno, e.seq)
                if data is None:
                    return CopyResult("fail", note=why)
                data = data[:e.size] if e.size else data
                f.write(data)
                head = data[:32]
            else:
                if not e.runs:
                    if e.size == 0:
                        return CopyResult("ok", 0, 0, b"", "")
                    return CopyResult("fail", note="데이터 위치(run)가 없음")
                remaining = e.size
                for lcn, ln in e.runs:
                    if remaining <= 0:
                        break
                    span = min(ln * cl, remaining)
                    pos = 0
                    while pos < span:
                        n = min(COPY_CHUNK, span - pos)
                        if lcn is None:           # sparse: 구멍으로 둔다
                            f.seek(n, 1)
                            if on_bytes:
                                on_bytes(n)
                            if len(head) < 32:
                                head += bytes(min(n, 32 - len(head)))
                        else:
                            data = img.read(vol.offset + dbase + lcn * cl + pos, n)
                            if len(data) < n:
                                short += n - len(data)
                                data += bytes(n - len(data))
                            f.write(data)
                            if on_bytes:
                                on_bytes(n)
                            if len(head) < 32:
                                head += data[:32 - len(head)]
                        pos += n
                    remaining -= span
                if remaining > 0:
                    note = f"data run 이 기록된 크기보다 {fmt_size(remaining)} 짧아 그만큼은 복사하지 못함"
                f.truncate(f.tell())
            written = f.tell()
    except OSError as ex:
        return CopyResult("fail", note=f"쓰기 실패: {ex}")
    errs = (img.read_errors - errs0) + (1 if short else 0)
    if errs:
        note = (note + " / " if note else "") + f"이미지 읽기 오류 구간이 있어 0으로 채움({fmt_size(short)} 이상)"
    return CopyResult("partial" if (errs or note) else "ok", written, errs, head, note)


def sig_check(name: str, head: bytes, size: int) -> str:
    """파일 앞부분과 확장자 시그니처 대조: ok / bad / unk / '' (확인 불가)"""
    if size <= 0 or not head:
        return ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if len(head) >= 16 and head[:16] == b"\x00" * 16:
        return "bad"
    if ext in MAGIC:
        return "ok" if any(head[o:o + len(s)] == s for o, s in MAGIC[ext]) else "bad"
    return "unk"


# ---- --root (특정 경로만 보기/복구) ----
def root_key(r: Optional[str]) -> str:
    return norm_path(r).lower() if r else ""


def under_root(path: str, root: str) -> bool:
    """path 가 root(소문자 정규화) 자신이거나 그 아래인가. root 가 비어 있으면 항상 True"""
    if not root:
        return True
    p = norm_path(path).lower()
    return p == root or p.startswith(root + "/")


def root_hint(paths, root: str, limit: int = 12) -> List[str]:
    """--root 경로가 없을 때, 실제로 존재하는 가장 깊은 상위 경로와 그 하위 이름을 알려 준다."""
    parts = [x for x in root.split("/") if x]
    names: Dict[str, str] = {}
    for p in paths:
        names.setdefault(norm_path(p).lower(), norm_path(p))
    best = ""
    for i in range(len(parts), -1, -1):
        cand = "/".join(parts[:i])
        if cand == "" or any(k == cand or k.startswith(cand + "/") for k in names):
            best = cand
            break
    kids: Dict[str, str] = {}
    pref = best + "/" if best else ""
    for k, orig in names.items():
        if k.startswith(pref) and len(k) > len(pref):
            first = orig[len(pref):].split("/", 1)[0]
            kids.setdefault(first.lower(), first)
    shown_best = ""
    if best:
        for k, orig in names.items():
            if k == best or k.startswith(best + "/"):
                shown_best = "/".join(orig.split("/")[:len(best.split("/"))])
                break
    out = [f"가장 가까이 존재하는 상위 경로: /{shown_best}"]
    names_l = sorted(kids.values(), key=str.lower)[:limit]
    if names_l:
        out.append("  그 아래 항목 예: " + ", ".join(names_l) + (" ..." if len(kids) > limit else ""))
    return out


def print_root_scope(args, rows, pt, hint_paths=None) -> None:
    """--root 지정 시 범위 요약(없으면 안내)"""
    rk = root_key(args.root)
    if not rk:
        return
    sub = [r for r in rows if under_root(r[0], rk)]
    files = [r[1] for r in sub if not r[1].is_dir]
    c = Counter(e.risk for e in files)
    print(f"\n[범위] --root /{norm_path(args.root)} : 폴더 {sum(1 for r in sub if r[1].is_dir)}개 / 파일 {len(files)}개 "
          f"(삭제 {sum(1 for e in files if not e.in_use)}) / 정상 {c[0]} 의심 {c[1]} 손상추정 {c[2]}")
    if not sub:
        print(pt.red("  지정한 경로를 찾지 못했습니다. 경로 철자와 폴더 구조를 확인하세요(대소문자는 구분하지 않음)."))
        for ln in root_hint(hint_paths if hint_paths else [r[0] for r in rows], rk):
            print("  " + ln)
        print("  * 폴더가 삭제됐거나 상위 폴더 정보가 손상됐다면 [유실된폴더_번호] 아래에 있을 수 있습니다: --browse 의 find 로 이름을 검색해 보세요.")


class Node:
    __slots__ = ("name", "path", "kids", "files", "nfiles", "ndel", "nbad", "nsus", "size", "parent")

    def __init__(self, name: str, path: str, parent: Optional["Node"]):
        self.name, self.path, self.parent = name, path, parent
        self.kids: Dict[str, "Node"] = {}
        self.files: List[tuple] = []     # (path, Entry)
        self.nfiles = self.ndel = self.nbad = self.nsus = 0
        self.size = 0


def norm_path(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def build_tree(rows) -> Node:
    root = Node("", "", None)
    for path, e, _st in rows:
        parts = [x for x in norm_path(path).split("/") if x]
        if not parts:
            continue
        chain = [root]
        cur = root
        dirs_part = parts if e.is_dir else parts[:-1]
        for i, nm in enumerate(dirs_part):
            k = nm.lower()
            nxt = cur.kids.get(k)
            if nxt is None:
                nxt = cur.kids[k] = Node(nm, "/".join(parts[:i + 1]), cur)
            cur = nxt
            chain.append(cur)
        if e.is_dir:
            continue
        cur.files.append(("/".join(parts), e))
        for nd in chain:
            nd.nfiles += 1
            nd.size += e.size
            nd.ndel += 0 if e.in_use else 1
            nd.nbad += 1 if e.risk >= 2 else 0
            nd.nsus += 1 if e.risk == 1 else 0
    return root


def find_node(root: Node, path: str) -> Optional[Node]:
    cur = root
    for nm in [x for x in norm_path(path).split("/") if x]:
        cur = cur.kids.get(nm.lower())
        if cur is None:
            return None
    return cur


class Selection:
    def __init__(self):
        self.dirs: Dict[str, str] = {}     # 소문자 경로 -> 표시용 경로
        self.files: Dict[str, str] = {}
        self.excl: Dict[str, str] = {}

    def add_dir(self, p: str):
        p = norm_path(p)
        self.dirs[p.lower()] = p

    def add_file(self, p: str):
        p = norm_path(p)
        self.files[p.lower()] = p

    def add_excl(self, p: str):
        p = norm_path(p)
        self.excl[p.lower()] = p

    def empty(self) -> bool:
        return not self.dirs and not self.files

    def match(self, path: str) -> bool:
        low = norm_path(path).lower()
        parts = low.split("/")
        pref = ""
        hit = low in self.files
        ex = low in self.excl
        for i, nm in enumerate(parts[:-1]):
            pref = f"{pref}/{nm}" if pref else nm
            if pref in self.excl:
                ex = True
            if pref in self.dirs:
                hit = True
        if "" in self.dirs:       # 루트 전체
            hit = True
        return hit and not ex


import unicodedata
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cw(ch: str) -> int:
    """문자 하나의 화면 칸 수(한글·전각은 2칸)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s: str) -> int:
    return sum(cw(c) for c in _ANSI.sub("", s))


def padw(s: str, w: int) -> str:
    """화면 칸 수 기준으로 오른쪽을 공백으로 채운다(한글 정렬용)."""
    return s + " " * max(0, w - disp_width(s))


def term_cols() -> int:
    try:
        return max(40, shutil.get_terminal_size((120, 24)).columns - 1)
    except Exception:  # noqa: BLE001
        return 119


def fit(line: str, width: Optional[int] = None) -> str:
    """한 줄이 창 너비보다 길면 줄바꿈으로 표가 깨지지 않도록 …로 줄인다(색상 코드는 유지)."""
    width = width or term_cols()
    if disp_width(line) <= width:
        return line
    out = []
    used = 0
    i = 0
    n = len(line)
    while i < n:
        m = _ANSI.match(line, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        w = cw(line[i])
        if used + w > width - 1:
            break
        out.append(line[i])
        used += w
        i += 1
    return "".join(out) + "…" + ("\033[0m" if "\033[" in line else "")


def fmt_node_line(n: Node, pt: Paint, num: Optional[int] = None, indent: int = 0) -> str:
    # 폴더 이름과 설명은 폴더 색, 의심/손상추정 개수만 각각 노랑/빨강으로 구분한다
    segs = [(f"파일 {n.nfiles}", "")]
    if n.ndel:
        segs.append((f"삭제 {n.ndel}", ""))
    if n.nsus:
        segs.append((f"의심 {n.nsus}", "s"))
    if n.nbad:
        segs.append((f"손상추정 {n.nbad}", "b"))
    out = pt.folder(f"{'  ' * indent}{'' if num is None else f'{num:>4}. '}{n.name}/  (")
    for i, (t, k) in enumerate(segs):
        if i:
            out += pt.folder(", ")
        out += pt.sus(t) if k == "s" else (pt.bad(t) if k == "b" else pt.folder(t))
    return out + pt.folder(f", {fmt_size(n.size)})")


def color_row(txt: str, e: Entry, pt: Paint) -> str:
    """콘솔 목록 한 줄 색칠: 폴더=시안, 손상추정=빨강, 의심=노랑, 정상 파일=에메랄드."""
    if e.is_dir:
        return pt.folder(txt)
    return pt.bad(txt) if e.risk >= 2 else (pt.sus(txt) if e.risk == 1 else pt.file(txt))


def fmt_file_line(e: Entry, name: str, pt: Paint, num: Optional[int] = None, indent: int = 0) -> str:
    st = "x" if not e.in_use else " "
    txt = (f"{'  ' * indent}{'' if num is None else f'{num:>4}. '}{st} {padw(RISK_LABEL[e.risk], 8)} {fmt_size(e.size):>10}  {name}")
    return pt.bad(txt) if e.risk >= 2 else (pt.sus(txt) if e.risk == 1 else pt.file(txt))


def print_tree(n: Node, depth: int, pt: Paint, indent: int = 0, limit: int = 60):
    kids = sorted(n.kids.values(), key=lambda k: k.name.lower())
    for k in kids[:limit]:
        print(fit(fmt_node_line(k, pt, None, indent)))
        if depth > 1:
            print_tree(k, depth - 1, pt, indent + 1, limit)
    if len(kids) > limit:
        print(f"{'  ' * indent}... 외 폴더 {len(kids) - limit}개")


def iter_files(n: Node):
    stack = [n]
    while stack:
        cur = stack.pop()
        yield from cur.files
        stack.extend(cur.kids.values())


def plan_copy(items: List[tuple], sel: Selection, skip_deleted: bool, skip_damaged: bool) -> List[tuple]:
    out = []
    for path, e in items:
        if not sel.match(path):
            continue
        if skip_deleted and not e.in_use:
            continue
        if skip_damaged and e.risk >= 2:
            continue
        out.append((path, e))
    return out


def run_copy(img, vol, boot, st_mft, plan: List[tuple], dest: str, tag_damaged: bool, overwrite: bool,
             force: bool, report_path: str, pt: Paint) -> int:
    if not plan:
        print("복사할 파일이 없습니다(선택 결과가 비어 있음).")
        return 1
    dest = os.path.abspath(dest)
    os.makedirs(winpath(dest), exist_ok=True)
    need = sum(e.size for _p, e in plan)
    try:
        free = shutil.disk_usage(dest).free
    except OSError:
        free = None
    print(f"복사 대상 {len(plan)}개 / 약 {fmt_size(need)} / 저장 위치 {dest}"
          + (f" (여유 공간 {fmt_size(free)})" if free is not None else ""))
    if free is not None and need + 256 * 1024 * 1024 > free and not force:
        print(pt.red("저장 위치의 여유 공간이 부족합니다. 선택을 줄이거나 다른 저장소를 쓰세요. (무시하려면 --force)"))
        return 2

    # 디스크 순서대로 읽어 탐색 시간을 줄인다 (상주 데이터는 앞쪽)
    def first_lcn(e: Entry) -> int:
        return next((l for l, _ in e.runs if l is not None), -1) if e.runs else -1
    plan = sorted(plan, key=lambda pe: first_lcn(pe[1]))
    used: Set[str] = set()
    prog = Progress(max(need, 1), "복사", as_bytes=True)
    t_start = time.monotonic()
    rows_out: List[list] = []
    folder_stat: Dict[str, List[int]] = {}   # 폴더 -> [파일, 손상추정, 의심, 삭제, 실패]
    done_bytes, shown_problem = 0, 0
    counts: Counter = Counter()
    for n_, (path, e) in enumerate(plan, 1):
        parts = [safe_component(x) for x in norm_path(path).split("/") if x]
        folder_rel = "/".join(parts[:-1])
        base_name = parts[-1]
        tmp_dir = os.path.join(dest, *parts[:-1]) if len(parts) > 1 else dest
        os.makedirs(winpath(tmp_dir), exist_ok=True)
        tmp = os.path.join(tmp_dir, f".{n_}.part")
        base_bytes = done_bytes
        cur_done = [0]

        def _tick(n, _b=base_bytes, _c=cur_done, _i=n_):
            _c[0] += n
            prog.update(_b + _c[0], f"{_i}/{len(plan)}개 {os.path.basename(path)[:30]}")
        res = copy_one(img, vol, boot, e, st_mft, tmp, _tick)
        notes = [m for m in e.issues if not m.startswith("(참고)")]
        if res.note:
            notes.append(res.note)
        sig = ""
        final_risk = e.risk
        if res.status.startswith("fail") or res.status == "fail":
            final = "복사불가"
            outp = ""
            try:
                os.remove(winpath(tmp))
            except OSError:
                pass
            final_risk = 2
        else:
            sig = sig_check(base_name, res.head, res.written)
            if sig == "bad" and e.risk < 2:
                final_risk = 2
                notes.append("복사한 내용의 앞부분이 확장자 시그니처와 다르거나 0으로 채워져 있음")
            if res.errs:
                final_risk = 2
            final = RISK_LABEL[final_risk]
            name = tag_name(base_name, DMG_TAG) if (tag_damaged and final_risk >= 2) else base_name
            rel = f"{folder_rel}/{name}" if folder_rel else name
            if rel.lower() in used:
                name = tag_name(name, f"#{e.recno}") if False else f"{os.path.splitext(name)[0]} (#{e.recno}){os.path.splitext(name)[1]}"
                rel = f"{folder_rel}/{name}" if folder_rel else name
            used.add(rel.lower())
            outp = os.path.join(dest, *rel.split("/"))
            try:
                if os.path.exists(winpath(outp)) and not overwrite:
                    if os.path.getsize(winpath(outp)) == res.written:
                        os.remove(winpath(tmp))
                        res.status = "ok" if res.status == "ok" else res.status
                        notes.append("이미 같은 크기의 파일이 있어 건너뜀")
                    else:
                        outp2 = os.path.join(dest, *(f"{os.path.splitext(rel)[0]} (#{e.recno}){os.path.splitext(rel)[1]}").split("/"))
                        os.replace(winpath(tmp), winpath(outp2))
                        outp = outp2
                else:
                    os.replace(winpath(tmp), winpath(outp))
                if e.mtime and os.path.exists(winpath(outp)):
                    try:
                        ts = calendar.timegm(e.mtime.timetuple())
                        os.utime(winpath(outp), (ts, ts))
                    except (OSError, ValueError, OverflowError):
                        pass
            except OSError as ex:
                final, final_risk = "복사불가", 2
                notes.append(f"저장 실패: {ex}")
                res.status = "fail"
        result = {"ok": "복사됨", "partial": "일부(오류있음)"}.get(res.status, "복사불가")
        counts[(result, final)] += 1
        done_bytes = base_bytes + max(e.size if res.status != "fail" else 0, 0)
        # 폴더 집계 (모든 상위 폴더에 합산)
        chain = [""] + ["/".join(parts[:i + 1]) for i in range(len(parts) - 1)]
        for c in chain:
            fs = folder_stat.setdefault(c, [0, 0, 0, 0, 0])
            fs[0] += 1
            fs[1] += 1 if final_risk >= 2 else 0
            fs[2] += 1 if final_risk == 1 else 0
            fs[3] += 0 if e.in_use else 1
            fs[4] += 1 if result == "복사불가" else 0
        rows_out.append(["/" + norm_path(path), outp, result, final, "삭제" if not e.in_use else "정상",
                         e.size, res.written, sig, e.recno, " | ".join(notes)])
        if (final_risk >= 2 or result != "복사됨") and shown_problem < 100:
            shown_problem += 1
            sys.stderr.write("\r" + " " * 100 + "\r")
            print(pt.red(f"  [{final}/{result}] /{norm_path(path)}" + (f"  - {notes[-1]}" if notes else "")), flush=True)
            if shown_problem == 100:
                print("  (이후 문제 파일은 화면에 표시하지 않고 보고서에만 기록합니다)")
        prog.update(done_bytes, f"{n_}/{len(plan)}개")
    prog.done(f"{len(plan)}/{len(plan)}개")

    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["원래경로", "저장경로", "복사결과", "최종판정", "삭제여부", "기록된크기", "복사된크기", "시그니처", "MFT번호", "메모"])
        w.writerows(rows_out)
    fpath = os.path.splitext(report_path)[0] + "_folders.csv"
    with open(fpath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["폴더", "복사한 파일", "손상추정", "의심", "삭제됨", "복사불가", "표시"])
        for k in sorted(folder_stat, key=str.lower):
            fs = folder_stat[k]
            w.writerow(["/" + k, *fs, "노랑(손상 파일 포함)" if fs[1] else ""])

    print("\n[복사 결과]")
    print(f"  걸린 시간 {fmt_eta(time.monotonic() - t_start)} / 복사 용량 {fmt_size(done_bytes)}")
    tot = len(plan)
    ok_clean = sum(v for (r, fin), v in counts.items() if r == "복사됨" and fin == "정상")
    sus = sum(v for (r, fin), v in counts.items() if fin == "의심" and r != "복사불가")
    dmg = sum(v for (r, fin), v in counts.items() if fin == "손상추정" and r != "복사불가")
    fail = sum(v for (r, fin), v in counts.items() if r == "복사불가")
    print(f"  전체 {tot}개 / 정상 복사 {ok_clean} / 의심 {sus} / " + pt.red(f"손상추정 {dmg}") + " / " + pt.red(f"복사불가 {fail}"))
    if dmg and tag_damaged:
        print(f"  * 손상추정 파일은 파일명에 '{DMG_TAG}' 를 붙여 저장했습니다(정상 파일로 오해하지 않도록).")
    yel = [(k, fs) for k, fs in folder_stat.items() if fs[1] and k]
    if yel:
        print(pt.yel("  손상추정 파일을 포함한 폴더(노랑):"))
        for k, fs in sorted(yel, key=lambda kv: -kv[1][1])[:15]:
            print(pt.yel(f"    /{k}  (손상추정 {fs[1]}/{fs[0]}개)"))
        if len(yel) > 15:
            print(f"    ... 외 {len(yel) - 15}개 폴더 (보고서 참고)")
    print(f"  보고서: {os.path.abspath(report_path)}")
    print(f"  폴더별 표시: {os.path.abspath(fpath)}")
    return 0


def _result_parent(item: tuple) -> str:
    k, v = item
    return (v.path if k == "d" else v[0]).rpartition("/")[0]


def sort_results(items: List[tuple]) -> List[tuple]:
    """결과를 폴더 경로 순으로 정렬한다(같은 폴더 안에서는 폴더 -> 파일, 이름순)."""
    def key(it):
        k, v = it
        name = v.name if k == "d" else v[0].rpartition("/")[2]
        return (_result_parent(it).lower(), 0 if k == "d" else 1, name.lower())
    return sorted(items, key=key)


def show_results(items: List[tuple], pt: Paint, title: str, size: int = 40, flat_time: bool = False) -> None:
    """검색/목록 결과를 폴더 경로별로 묶어 쪽 단위로 보여 준다. items 는 sort_results 를 거친 목록(번호 r1.. 이 순서대로 붙음)."""
    rows: List[tuple] = []          # (헤더 여부, 출력 문자열, 소속 폴더 헤더)
    last = None
    hdr = ""
    for i, it in enumerate(items, 1):
        if flat_time:           # 시간순 목록: 폴더로 묶지 않고 전체 경로 표시
            e_ = it[1][1]
            t_ = e_.mft_time or e_.mtime
            ts = t_.strftime("%Y-%m-%d %H:%M:%S") if t_ else "-" * 19
            rows.append((False, f"   r{i:<5}{ts}  " + fmt_file_line(e_, "/" + it[1][0], pt), ""))
            continue
        par = _result_parent(it)
        if par != last:
            last = par
            hdr = pt.cyan("  /" + (par + "/" if par else ""))
            rows.append((True, hdr, hdr))
        if it[0] == "d":
            rows.append((False, f"   r{i:<5}" + fmt_node_line(it[1], pt), hdr))
        else:
            rows.append((False, f"   r{i:<5}" + fmt_file_line(it[1][1], it[1][0].rpartition("/")[2], pt), hdr))
    n = len(rows)
    pages = max(1, -(-n // size))
    cur = 0
    nfold = len({_result_parent(it) for it in items})
    fold_txt = "" if flat_time else f" / 폴더 {nfold}개"
    while True:
        print(f"\n{title}: {len(items)}건{fold_txt}" + (f" - {cur + 1}/{pages}쪽" if pages > 1 else ""))
        chunk = rows[cur * size:(cur + 1) * size]
        if chunk and not chunk[0][0]:
            print(chunk[0][2])       # 쪽이 폴더 중간에서 시작하면 소속 폴더를 다시 표시
        for _h, text, _g in chunk:
            print(fit(text))
        if pages == 1:
            break
        try:
            ans = input(f"[Enter=다음 쪽 / p=이전 / 쪽번호(1~{pages}) / q=그만 보기] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if ans in ("q", "x"):
            break
        if ans == "p":
            cur = max(0, cur - 1)
        elif ans.isdigit() and 1 <= int(ans) <= pages:
            cur = int(ans) - 1
        elif cur + 1 < pages:
            cur += 1
        else:
            break
    print("  선택: s r번호 [r번호..] / s r1-r20 (범위) / 다시 보기: rs")


# --------------------------------------------------------------------------
# $UsnJrnl:$J (NTFS 변경 저널) 읽기 -> 삭제 이벤트
# --------------------------------------------------------------------------
USN_REASONS = [(0x1, "데이터덮어씀"), (0x2, "데이터확장"), (0x4, "데이터축소"), (0x100, "생성"), (0x200, "삭제"),
               (0x1000, "이름변경(이전)"), (0x2000, "이름변경(새이름)"), (0x80000000, "닫힘")]
_NONZERO = re.compile(rb"[^\x00]")


@dataclass
class UsnEvent:
    usn: int
    time: Optional[datetime.datetime]
    reason: int
    name: str
    recno: int
    seq: int
    parent: int
    attrs: int


def parse_usn_buffer(buf: bytes, final: bool = True) -> Tuple[List[UsnEvent], int]:
    """USN 레코드(V2/V3)들을 읽는다. (이벤트 목록, 처리한 바이트 수). final=False 면 끝에서 잘린 레코드는 처리하지 않고 남겨 둔다."""
    out: List[UsnEvent] = []
    n = len(buf)
    pos = 0
    while pos + 60 <= n:
        ln = struct.unpack_from("<I", buf, pos)[0]
        if ln == 0:
            m = _NONZERO.search(buf, pos + 4)
            if m is None:
                pos = n
                break
            pos = m.start() & ~7
            continue
        major = struct.unpack_from("<H", buf, pos + 4)[0]
        ok = 60 <= ln <= 0x2000 and ln % 8 == 0 and major in (2, 3)
        if ok and pos + ln > n:
            if not final:
                return out, pos          # 다음 덩어리와 이어서 처리
            ok = False
        if ok:
            try:
                if major == 2:
                    ref, pref, usn, ts, reason, _src, _sec, attrs, nlen, noff = struct.unpack_from("<QQqQIIIIHH", buf, pos + 8)
                    recno, seq, parent = ref & 0xFFFFFFFFFFFF, ref >> 48, pref & 0xFFFFFFFFFFFF
                else:
                    ref = int.from_bytes(buf[pos + 8:pos + 24], "little")
                    pref = int.from_bytes(buf[pos + 24:pos + 40], "little")
                    usn, ts, reason, _src, _sec, attrs, nlen, noff = struct.unpack_from("<qQIIIIHH", buf, pos + 40)
                    recno, seq, parent = ref & 0xFFFFFFFFFFFF, (ref >> 48) & 0xFFFF, pref & 0xFFFFFFFFFFFF
                if noff >= 60 and nlen % 2 == 0 and nlen > 0 and noff + nlen <= ln and reason and 125_000_000_000_000_000 < ts < 160_000_000_000_000_000:
                    name = buf[pos + noff:pos + noff + nlen].decode("utf-16-le", "replace")
                    out.append(UsnEvent(usn, filetime(ts), reason, name, recno, seq, parent, attrs))
                    pos += ln
                    continue
            except struct.error:
                pass
        pos += 8                         # 레코드가 아니면 8바이트씩 건너뛰며 다음 레코드를 찾는다
    return out, pos


def usn_named_runs(raw: bytes, recno: int, stream: str = "$J"):
    """MFT 레코드에서 이름 붙은 $DATA(기본 $J)의 (runs, 논리 크기)를 꺼낸다. 못 찾으면 (None, 사유)."""
    n = len(raw)
    if n < 512 or raw[:4] != b"FILE":
        return None, "$UsnJrnl 레코드를 읽을 수 없음"
    usa_off, usa_cnt = struct.unpack_from("<HH", raw, 4)
    if not (0x2A <= usa_off <= 0x40) or usa_cnt != n // 512 + 1:
        return None, "$UsnJrnl 레코드 헤더 손상"
    buf = bytearray(raw)
    apply_fixups(buf, usa_off, usa_cnt)
    off = struct.unpack_from("<H", buf, 0x14)[0]
    used = min(struct.unpack_from("<I", buf, 0x18)[0], n)
    frags: List[Tuple[int, Runs]] = []
    size = 0
    while off + 16 <= used:
        atype, alen = struct.unpack_from("<II", buf, off)
        if atype == 0xFFFFFFFF or alen < 16 or alen % 8 or off + alen > n:
            break
        nlen = buf[off + 9]
        if atype == 0x80 and nlen and buf[off + 8]:
            noff = struct.unpack_from("<H", buf, off + 10)[0]
            nm = bytes(buf[off + noff:off + noff + nlen * 2]).decode("utf-16-le", "replace")
            if nm == stream:
                svcn = struct.unpack_from("<Q", buf, off + 0x10)[0]
                runoff = struct.unpack_from("<H", buf, off + 0x20)[0]
                if svcn == 0:
                    size = struct.unpack_from("<Q", buf, off + 0x30)[0]
                frags.append((svcn, decode_runs(bytes(buf[off + runoff:off + alen]))))
        elif atype == 0x20:
            return None, "$UsnJrnl 속성이 확장 레코드에 나뉘어 있어 읽지 못함"
        off += alen
    if not frags:
        return None, "$UsnJrnl 에 $J 스트림이 없음(변경 저널이 꺼져 있었을 수 있음)"
    return chain_runs(frags), size


def load_usn(img, vol, boot, st_mft, rows, progress=None) -> Tuple[List[UsnEvent], str]:
    """$Extend\\$UsnJrnl:$J 를 읽어 USN 이벤트 목록(최신순)을 돌려준다. (이벤트, 메시지)"""
    cand = [e for _p, e, *_r in rows if e.name == "$UsnJrnl" and not e.is_dir]
    if not cand:
        return [], "$UsnJrnl 항목을 찾지 못했습니다(변경 저널이 없거나 MFT 읽기 범위 밖). --all 없이도 시스템 파일은 읽습니다."
    e = max(cand, key=lambda x: (x.in_use, x.source == "MFT"))
    pos = record_phys(e, st_mft, boot)
    if pos is None:
        return [], "$UsnJrnl 레코드 위치를 알 수 없습니다."
    runs, size = usn_named_runs(img.read(vol.offset + pos, boot.record_size), e.recno)
    if runs is None:
        return [], size
    cl = boot.cluster
    data_runs = [(lcn, ln) for lcn, ln in runs if lcn is not None]
    total = sum(ln for _l, ln in data_runs) * cl
    if not data_runs:
        return [], "저널에 기록된 데이터가 없습니다(전부 비어 있음)."
    events: List[UsnEvent] = []
    done = 0
    CH = 4 << 20
    errs0 = img.read_errors
    for lcn, ln in data_runs:
        base = vol.offset + lcn * cl
        span = ln * cl
        p = 0
        carry = b""
        while p < span:
            n = min(CH, span - p)
            chunk = carry + img.read(base + p, n)
            last = p + n >= span
            evs, used = parse_usn_buffer(chunk, final=last)
            events.extend(evs)
            carry = chunk[used:] if not last else b""
            p += n
            done += n
            if progress:
                progress(done, total)
    seen = set()
    uniq = []
    for ev in events:
        k = (ev.usn, ev.recno)
        if k not in seen:
            seen.add(k)
            uniq.append(ev)
    uniq.sort(key=lambda ev: ev.usn, reverse=True)
    msg = f"저널 데이터 {fmt_size(total)} 읽음 / 이벤트 {len(uniq)}건"
    if uniq:
        ts_ = [x.time for x in uniq if x.time]
        if ts_:
            msg += f" (기록 범위 {min(ts_):%Y-%m-%d %H:%M} ~ {max(ts_):%Y-%m-%d %H:%M})"
    if img.read_errors > errs0:
        msg += f" / 읽기 오류 {img.read_errors - errs0}곳"
    return uniq, msg


def usn_delete_view(events: List[UsnEvent], rows) -> List[dict]:
    """삭제/휴지통 이동 이벤트를 경로와 복구 후보 항목에 연결한다. 최신순 목록."""
    dirs: Dict[int, str] = {5: ""}          # 5번 레코드 = 루트 폴더
    byrec: Dict[int, List[tuple]] = {}
    for p, e, *_r in rows:
        if e.is_dir:
            dirs.setdefault(e.recno, norm_path(p))
        else:
            byrec.setdefault(e.recno, []).append((norm_path(p), e))

    def ppath(rec: int) -> str:
        return dirs.get(rec, f"(폴더 #{rec})")

    def is_bin(path: str) -> bool:
        return "$recycle.bin" in path.lower() or path.lower().startswith("recycler")

    by_file: Dict[int, List[UsnEvent]] = {}
    for ev in events:                       # events 는 최신순
        by_file.setdefault(ev.recno, []).append(ev)
    out: List[dict] = []
    for ev in events:
        kind = None
        orig_name, orig_dir, new_name = ev.name, ppath(ev.parent), None
        if ev.reason & 0x200:
            if is_bin(ppath(ev.parent)):
                if ev.name.startswith("$I"):
                    continue                # 휴지통 $I 파일 삭제는 잡음
                kind = "휴지통 비움"
            else:
                kind = "삭제"
        elif ev.reason & 0x1000:            # 이름변경(이전): 바로 뒤 이벤트가 휴지통으로의 이동이면 '휴지통으로 이동'
            nxt = [x for x in by_file[ev.recno] if x.usn > ev.usn and x.reason & 0x2000]
            if nxt:
                nx = min(nxt, key=lambda x: x.usn)
                if is_bin(ppath(nx.parent)) and not is_bin(ppath(ev.parent)):
                    kind, new_name = "휴지통으로 이동", nx.name
        if not kind:
            continue
        match = None
        for p, e in byrec.get(ev.recno, []):
            nm = (e.name or "").lower()
            if nm in (ev.name.lower(), (new_name or "").lower()) or not nm:
                match = (p, e)
                if not e.in_use or kind == "휴지통으로 이동":
                    break
        out.append({"ev": ev, "kind": kind, "dir": orig_dir, "name": orig_name, "match": match,
                    "isdir": bool(ev.attrs & 0x10)})
    return out


def show_usn(view: List[dict], pt: Paint, title: str, size: int = 40) -> List[tuple]:
    """USN 삭제 이벤트를 시간 역순으로 쪽 단위 출력. 복구 후보가 있는 항목에 r번호를 붙이고 그 목록(finds)을 돌려준다."""
    finds: List[tuple] = []
    lines: List[str] = []
    seen_rec = {}
    for v in view:
        tag = ""
        if v["match"] is not None and not v["isdir"]:
            key = v["ev"].recno
            if key not in seen_rec:
                finds.append(("f", (v["match"][0], v["match"][1])))
                seen_rec[key] = len(finds)
            tag = f"  -> r{seen_rec[key]}" + ("" if v["match"][1].in_use is False else " (휴지통 안)")
        ts = f"{v['ev'].time:%Y-%m-%d %H:%M:%S}" if v["ev"].time else "-" * 19
        d = "/" + v["dir"].strip("/") if v["dir"].strip("/") else "/"
        row = f"  {ts}  {v['kind']:<8} {'[폴더] ' if v['isdir'] else ''}{d.rstrip('/')}/{v['name']}{tag}"
        lines.append(pt.dim(row) if not tag else row)
    n = len(lines)
    pages = max(1, -(-n // size))
    cur = 0
    while True:
        print(f"\n{title}: {n}건" + (f" - {cur + 1}/{pages}쪽" if pages > 1 else ""))
        for ln in lines[cur * size:(cur + 1) * size]:
            print(fit(ln))
        if pages == 1:
            break
        try:
            ans = input(f"[Enter=다음 쪽 / p=이전 / 쪽번호(1~{pages}) / q=그만 보기] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if ans in ("q", "x"):
            break
        if ans == "p":
            cur = max(0, cur - 1)
        elif ans.isdigit() and 1 <= int(ans) <= pages:
            cur = int(ans) - 1
        elif cur + 1 < pages:
            cur += 1
        else:
            break
    if finds:
        print(f"  '-> r번호' 가 붙은 항목은 현재 MFT 에 남아 복구 후보로 선택할 수 있습니다: s r번호 / s r1-r{len(finds)}")
    print("  (이벤트가 있어도 파일 내용이 덮어쓰였을 수 있습니다. 목록의 건강도 표시를 함께 확인하세요)")
    return finds


def parse_flags(rest: str) -> Tuple[set, str]:
    """명령 인자에서 공통 옵션(-a 전부, -r 하위 포함, -w 볼륨 전체)을 분리한다. 나머지 글자는 그대로 돌려준다. -h 는 예전 호환용(무시)."""
    flags: set = set()
    words = []
    for t in rest.split():
        if re.fullmatch(r"-[arwh]+", t.lower()):
            flags.update(t.lower()[1:])
        else:
            words.append(t)
    return flags, " ".join(words)


def interactive_browse(root: Node, items_all: List[tuple], sel: Selection, pt: Paint,
                       state: Optional[dict] = None) -> bool:
    """간단한 폴더 탐색기. 복사를 시작하려면 True, 종료하면 False. state 로 현재 폴더를 이어서 사용."""
    state = state if state is not None else {}
    cur = state.get("cur", root)
    page_size = 40
    listing: List[tuple] = []
    finds: List[tuple] = []
    last_title = "결과"
    HELP = ("이동·선택: cd N·cd ..·cd /   s N [N..]·s .·s all 선택   u N·u all 해제   x 경로 제외   sel 선택 목록   clear 비우기\n"
            "보기:      ls [쪽]            이 폴더의 항목만\n"
            "           ls -r              하위 폴더 파일까지 폴더별로\n"
            "           find 글자          이름·경로 검색 (이 폴더 아래)\n"
            "           bad                손상추정 파일\n"
            "           recent [일수]      최근 삭제 항목(시간 역순)\n"
            "           usn [일수] [글자]  변경 저널의 삭제·휴지통 이동 이벤트(NTFS)\n"
            "           rs                 직전 결과 다시 보기   (결과는 s r3, s r1-r20 으로 선택)\n"
            "공통 옵션: -a 쪽 나누지 않고 전부 / -r 하위 폴더 포함(ls) / -w 볼륨 전체 기준(ls -r·find·bad·recent·usn)\n"
            "복사·종료: copy 복사 시작(끝나면 이어서 선택)   q 종료")

    def make_listing():
        nonlocal listing
        kids = sorted(cur.kids.values(), key=lambda k: k.name.lower())
        fl = sorted(cur.files, key=lambda pe: pe[0].lower())
        listing = [("d", k) for k in kids] + [("f", pe) for pe in fl]

    def show(page: int = 1, allp: bool = False):
        make_listing()
        total = len(listing)
        pages = max(1, -(-total // page_size))
        page = min(max(page, 1), pages)
        if allp:
            page = 1
        print(f"\n[/{cur.path}]  하위 폴더 {len(cur.kids)}개 / 이 폴더 파일 {len(cur.files)}개 " + ("(전체 표시)" if allp else f"({page}/{pages}쪽)"))
        for i in range(page_size * (page - 1), total if allp else min(total, page_size * page)):
            k, v = listing[i]
            mark = "*" if (k == "d" and v.path.lower() in sel.dirs) or (k == "f" and v[0].lower() in sel.files) else " "
            if k == "d":
                print(fit(mark + fmt_node_line(v, pt, i + 1)))
            else:
                print(fit(mark + fmt_file_line(v[1], v[0].rsplit("/", 1)[-1], pt, i + 1)))

    print(HELP)
    show()
    while True:
        try:
            line = input(f"\n/{cur.path}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        cmd = cmd.lower()
        state["cur"] = cur
        if cmd in ("ll", "la"):          # 예전 이름: ls -r 과 같음
            cmd, rest = "ls", "-r " + rest
        if cmd in ("q", "quit", "exit"):
            return False
        if cmd in ("h", "?", "help"):
            print(HELP)
        elif cmd == "ls":
            fl, wd = parse_flags(rest)
            if "r" in fl:                  # 하위 폴더까지 포함: 폴더별로 묶어서
                scope = root if "w" in fl else cur
                finds = sort_results([("f", (p_, e_)) for p_, e_ in iter_files(scope)])
                if not finds:
                    print("  이 폴더 아래에 파일이 없습니다.")
                    continue
                last_title = f"/{scope.path} 아래 전체 파일(하위 폴더 포함)"
                show_results(finds, pt, last_title, size=10 ** 9 if "a" in fl else 40)
            else:                          # 현재 폴더의 항목만
                show(int(next((x for x in wd.split() if x.isdigit()), 1)), allp="a" in fl)
        elif cmd == "cd":
            if rest in ("..",):
                cur = cur.parent or cur
            elif rest in ("/", ""):
                cur = root
            elif rest.isdigit() and 1 <= int(rest) <= len(listing) and listing[int(rest) - 1][0] == "d":
                cur = listing[int(rest) - 1][1]
            else:
                nd = find_node(cur, rest) or find_node(root, rest)
                if nd is None:
                    print("그런 폴더가 없습니다.")
                    continue
                cur = nd
            show()
        elif cmd in ("s", "u"):
            toks = rest.split()
            if not toks:
                print("대상 번호를 적어 주세요. 예: s 3 5  /  s .  /  s all")
                continue
            make_listing()
            targets: List[tuple] = []
            for t in toks:
                if t == ".":
                    targets.append(("d", cur))
                elif t == "all":
                    targets.extend(listing)
                elif re.fullmatch(r"(?i)r\d+-r?\d+", t):
                    a_, b_ = (int(x) for x in re.findall(r"\d+", t))
                    if a_ > b_:
                        a_, b_ = b_, a_
                    targets.extend(finds[max(a_, 1) - 1:min(b_, len(finds))])
                elif t.lower().startswith("r") and t[1:].isdigit() and 1 <= int(t[1:]) <= len(finds):
                    targets.append(finds[int(t[1:]) - 1])
                elif t.isdigit() and 1 <= int(t) <= len(listing):
                    targets.append(listing[int(t) - 1])
                else:
                    print(f"  '{t}' 는 올바른 번호가 아닙니다.")
            for k, v in targets:
                if k == "d":
                    (sel.add_dir if cmd == "s" else lambda p: sel.dirs.pop(norm_path(p).lower(), None))(v.path)
                else:
                    (sel.add_file if cmd == "s" else lambda p: sel.files.pop(norm_path(p).lower(), None))(v[0])
            if rest.strip() == "all" and cmd == "u":
                sel.dirs.clear()
                sel.files.clear()
            print(f"  현재 선택: 폴더 {len(sel.dirs)}개 / 파일 {len(sel.files)}개 / 제외 {len(sel.excl)}개")
        elif cmd == "sel":
            for p in sorted(sel.dirs.values()):
                nd = find_node(root, p)
                print("  [폴더] /" + p + (f"  (파일 {nd.nfiles}, 손상추정 {nd.nbad})" if nd else ""))
            for p in sorted(sel.files.values()):
                print("  [파일] /" + p)
            for p in sorted(sel.excl.values()):
                print("  [제외] /" + p)
            if sel.empty():
                print("  (선택 없음)")
        elif cmd == "clear":
            sel.dirs.clear(); sel.files.clear(); sel.excl.clear()
            print("  선택을 모두 비웠습니다.")
        elif cmd == "x":
            if rest:
                sel.add_excl(rest)
                print(f"  제외 추가: /{norm_path(rest)}")
        elif cmd in ("find", "f"):
            fl, rest = parse_flags(rest)
            if not rest:
                print("검색할 글자를 적어 주세요. 예: find 보고서  (이 폴더 아래 검색, -w 볼륨 전체, -a 쪽 나눔 없이 전부)")
                continue
            q = rest.lower()
            scope = root if "w" in fl else cur
            base = scope.path.lower()
            finds = []
            for path, e in items_all:
                if q in path.lower() and (not base or norm_path(path).lower().startswith(base + "/")):
                    finds.append(("f", (norm_path(path), e)))
            for ps, nd in _walk_nodes(scope):
                if q in nd.name.lower():
                    finds.append(("d", nd))
            finds = sort_results(finds)
            if not finds:
                print("  검색 결과가 없습니다.")
                continue
            last_title = f"검색 '{rest}'" + (f" (/{scope.path} 아래)" if scope.path else "")
            show_results(finds, pt, last_title, size=10 ** 9 if "a" in fl else 40)
        elif cmd == "bad":
            fl, _w = parse_flags(rest)
            scope = root if "w" in fl else cur
            finds = sort_results([("f", (p_, e_)) for p_, e_ in iter_files(scope) if e_.risk >= 2])
            if not finds:
                print("  손상추정 파일이 없습니다.")
                continue
            last_title = f"/{scope.path} 아래 손상추정 파일"
            show_results(finds, pt, last_title, size=10 ** 9 if "a" in fl else 40)
        elif cmd in ("recent", "rec"):
            fl, wd = parse_flags(rest)
            scope = root if "w" in fl else cur
            allp = "a" in fl
            nums = [t for t in wd.split() if t.isdigit()]
            days = int(nums[0]) if nums else None
            dels = [(p_, e_) for p_, e_ in iter_files(scope) if not e_.in_use]
            if not dels:
                print("  이 폴더 아래에 삭제된 파일이 없습니다.")
                continue
            has_t = [(p_, e_) for p_, e_ in dels if e_.mft_time]
            if has_t:
                keyf = lambda pe: pe[1].mft_time
                basis = "삭제 추정 시각(MFT 변경 시각)"
            else:
                has_t = [(p_, e_) for p_, e_ in dels if e_.mtime]
                keyf = lambda pe: pe[1].mtime
                basis = "수정 시각(이 파일시스템은 삭제 시각이 기록되지 않음 - 삭제 시각 아님)"
            if not has_t:
                print("  시각 정보가 있는 삭제 파일이 없습니다.")
                continue
            has_t.sort(key=keyf, reverse=True)
            n_notime = len(dels) - len(has_t)
            if days:
                newest = keyf(has_t[0])
                has_t = [pe for pe in has_t if (newest - keyf(pe)).total_seconds() <= days * 86400]
            finds = [("f", (norm_path(p_), e_)) for p_, e_ in has_t]
            last_title = f"/{scope.path} 아래 최근 삭제 항목 - 기준: {basis}" + (f", 가장 최근 항목 기준 {days}일 이내" if days else "")
            if n_notime:
                print(f"  (시각 정보가 없는 삭제 항목 {n_notime}개는 제외)")
            show_results(finds, pt, last_title, size=10 ** 9 if allp else 40, flat_time=True)
        elif cmd == "usn":
            prov = state.get("usn_provider")
            if prov is None:
                print("  이 명령은 NTFS 에서만 쓸 수 있습니다(FAT/exFAT 은 변경 저널이 없습니다).")
                continue
            if "view" not in state:
                state["view"], state["usn_msg"] = prov()
                print("  " + state["usn_msg"])
            view = state["view"]
            if not view:
                print("  표시할 삭제 이벤트가 없습니다." + ("" if state["usn_msg"].find("이벤트 0건") < 0 else " (저널이 비어 있음)"))
                continue
            fl, wd = parse_flags(rest)
            allp = "a" in fl
            here = "w" not in fl           # 기본: 이 폴더 아래, -w 면 볼륨 전체
            days = None
            words = []
            for t in wd.split():
                if t.isdigit() and days is None:
                    days = int(t)
                else:
                    words.append(t.lower())
            sub = view
            if days:
                newest = max((v["ev"].time for v in view if v["ev"].time), default=None)
                if newest:
                    sub = [v for v in sub if v["ev"].time and (newest - v["ev"].time).total_seconds() <= days * 86400]
            if here and cur.path:
                base = "/" + cur.path.lower()
                sub = [v for v in sub if ("/" + v["dir"].strip("/")).lower() == base or ("/" + v["dir"].strip("/")).lower().startswith(base + "/")]
            for w in words:
                sub = [v for v in sub if w in (v["dir"] + "/" + v["name"]).lower()]
            if not sub:
                print("  조건에 맞는 이벤트가 없습니다.")
                continue
            last_title = "USN 삭제 이벤트"
            finds = show_usn(sub, pt, f"변경 저널(USN) 삭제 이벤트 - 최신순" + (f" / 최근 {days}일" if days else "") + (f" / /{cur.path} 아래" if here and cur.path else ""),
                             size=10 ** 9 if allp else 40)
        elif cmd == "rs":
            if finds:
                show_results(finds, pt, last_title, flat_time=last_title.find("최근 삭제 항목") >= 0)
            else:
                print("  보여 줄 직전 결과가 없습니다.")
        elif cmd in ("copy", "c"):
            if sel.empty():
                print("선택한 항목이 없습니다. 먼저 s 로 선택하세요.")
                continue
            state["cur"] = cur
            return True
        else:
            print("알 수 없는 명령입니다. help 로 도움말을 보세요.")


def _walk_nodes(root: Node):
    stack = [root]
    while stack:
        n = stack.pop()
        for k in n.kids.values():
            yield n.path, k
            stack.append(k)



# === FAT-BEGIN ===
# --------------------------------------------------------------------------
# FAT12/16/32, exFAT 지원 (목록 / 건강도 / 복사). NTFS 와 같은 Entry/트리/복사 기능을 공유한다.
#  - 복사는 Entry.runs 를 (클러스터번호-2, 길이) 로 기록하고, FatBoot.data_base 를 더해 위치를 계산한다.
# --------------------------------------------------------------------------
import array as _array

OEM_CP = "cp949"
FAT_DOT = b".          "
FAT_DOTDOT = b"..         "


def _u16(b, o):
    return b[o] | (b[o + 1] << 8)


def _u32(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


def _u64(b, o):
    return _u32(b, o) | (_u32(b, o + 4) << 32)


class FatBoot:
    """copy_one 이 Boot 처럼 쓸 수 있는 FAT/exFAT 볼륨 정보"""
    ok = True
    record_size = 1024

    def __init__(self, kind: str):
        self.kind = kind            # FAT12 / FAT16 / FAT32 / exFAT
        self.bps = 512
        self.cluster = 512
        self.data_base = 0          # 볼륨 시작 기준 첫 데이터 클러스터(#2)의 바이트 위치
        self.n_clusters = 0
        self.fat_off = 0            # FAT1 위치(바이트)
        self.fat_len = 0            # FAT 하나의 크기(바이트)
        self.n_fats = 2
        self.root_cluster = 2
        self.root_off = 0           # FAT12/16 고정 루트 위치(바이트)
        self.root_len = 0
        self.problems: List[str] = []
        self.label_hint = ""

    @property
    def is_exfat(self) -> bool:
        return self.kind == "exFAT"


def detect_fs(sec: bytes) -> Optional[str]:
    """부트섹터로 파일시스템 종류 추정: NTFS / exFAT / FAT / None"""
    if len(sec) < 512:
        return None
    if sec[3:11] == b"NTFS    " and sec[510:512] == b"\x55\xaa":
        return "NTFS"
    if sec[3:11] == b"EXFAT   ":
        return "exFAT"
    if sec[510:512] != b"\x55\xaa" or sec[0] not in (0xEB, 0xE9):
        return None
    bps, spc, rsvd, nf = _u16(sec, 11), sec[13], _u16(sec, 14), sec[16]
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1) or rsvd == 0 or nf not in (1, 2):
        return None
    return "FAT"


def parse_fat_boot(sec: bytes, vol_size: int) -> FatBoot:
    bps, spc, rsvd, nf = _u16(sec, 11), sec[13], _u16(sec, 14), sec[16]
    root_ent = _u16(sec, 17)
    tot = _u16(sec, 19) or _u32(sec, 32)
    fatsz = _u16(sec, 22) or _u32(sec, 36)
    root_sec = (root_ent * 32 + bps - 1) // bps
    data_sec = rsvd + nf * fatsz + root_sec
    ncl = max((tot - data_sec) // spc, 0) if spc else 0
    kind = "FAT12" if ncl < 4085 else ("FAT16" if ncl < 65525 else "FAT32")
    fb = FatBoot(kind)
    fb.bps, fb.cluster, fb.n_clusters, fb.n_fats = bps, bps * spc, ncl, nf
    fb.fat_off, fb.fat_len = rsvd * bps, fatsz * bps
    fb.data_base = data_sec * bps
    if kind == "FAT32":
        fb.root_cluster = _u32(sec, 44)
    else:
        fb.root_off, fb.root_len = (rsvd + nf * fatsz) * bps, root_ent * 32
    if tot * bps > vol_size + 4096:
        fb.problems.append("부트섹터의 총 섹터 수가 실제 볼륨 크기보다 큼")
    if fatsz == 0 or ncl == 0:
        fb.problems.append("FAT 크기/클러스터 수가 비정상")
    if kind == "FAT32" and not (2 <= fb.root_cluster < ncl + 2):
        fb.problems.append("루트 폴더 클러스터 번호가 비정상")
    return fb


def parse_exfat_boot(sec: bytes, vol_size: int) -> FatBoot:
    fb = FatBoot("exFAT")
    sh_b, sh_c = sec[0x6C], sec[0x6D]
    if not (9 <= sh_b <= 12) or sh_c > 25 or sh_b + sh_c > 25:
        fb.problems.append("비정상 섹터/클러스터 크기 정보")
        return fb
    fb.bps = 1 << sh_b
    fb.cluster = 1 << (sh_b + sh_c)
    fb.fat_off = _u32(sec, 0x50) * fb.bps
    fb.fat_len = _u32(sec, 0x54) * fb.bps
    fb.data_base = _u32(sec, 0x58) * fb.bps
    fb.n_clusters = _u32(sec, 0x5C)
    fb.root_cluster = _u32(sec, 0x60)
    fb.n_fats = sec[0x6E] or 1
    vlen = _u64(sec, 0x48)
    if vlen * fb.bps > vol_size + 4096:
        fb.problems.append("부트섹터의 볼륨 길이가 실제 볼륨 크기보다 큼")
    if not (2 <= fb.root_cluster < fb.n_clusters + 2):
        fb.problems.append("루트 폴더 클러스터 번호가 비정상")
    if fb.n_clusters == 0:
        fb.problems.append("클러스터 수가 0")
    return fb


def dos_datetime(d: int, t: int) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime(1980 + (d >> 9), (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63, (t & 31) * 2)
    except ValueError:
        return None


def lfn_checksum(short11: bytes) -> int:
    s = 0
    for b in short11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def exfat_set_checksum(data: bytes) -> int:
    cs = 0
    for i, b in enumerate(data):
        if i in (2, 3):
            continue
        cs = (((cs & 1) << 15) | (cs >> 1)) + b & 0xFFFF
    return cs


class DirEnt:
    __slots__ = ("name", "attr", "cluster", "size", "mtime", "deleted", "dot", "note", "nofat", "valid_len", "pos")

    def __init__(self, name, attr, cluster, size, mtime, deleted, dot=None, note="", nofat=False, valid_len=None):
        self.name, self.attr, self.cluster, self.size, self.mtime = name, attr, cluster, size, mtime
        self.deleted, self.dot, self.note, self.nofat, self.valid_len = deleted, dot, note, nofat, valid_len

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & 0x10)


def _lfn_text(slots: List[bytes]) -> str:
    """slots: 순번이 오름차순인 LFN 슬롯 목록"""
    raw = b"".join(s[1:11] + s[14:26] + s[28:32] for s in slots)
    chars = []
    for i in range(0, len(raw) - 1, 2):
        u = raw[i] | (raw[i + 1] << 8)
        if u == 0:
            break
        if u == 0xFFFF:
            continue
        chars.append(u)
    return b"".join(c.to_bytes(2, "little") for c in chars).decode("utf-16-le", "replace")


def parse_fat_dir(data: bytes, fat32: bool, oem: str = OEM_CP) -> List[DirEnt]:
    out: List[DirEnt] = []
    pend: List[bytes] = []
    for i in range(len(data) // 32):
        e = data[i * 32:(i + 1) * 32]
        b0 = e[0]
        if b0 == 0:
            break
        attr = e[11]
        if attr & 0x3F == 0x0F:
            pend.append(e)
            continue
        if attr & 0x08:           # 볼륨 라벨
            pend = []
            continue
        deleted = b0 == 0xE5
        raw = bytearray(e[:11])
        if b0 == 0x05:
            raw[0] = 0xE5
        cl = _u16(e, 26) | ((_u16(e, 20) << 16) if fat32 else 0)
        size = _u32(e, 28)
        mt = dos_datetime(_u16(e, 24), _u16(e, 22))
        if raw[:11] == FAT_DOT or raw[:11] == FAT_DOTDOT or (not deleted and raw[0] == 0x2E and raw[1] in (0x20, 0x2E)):
            out.append(DirEnt("." if raw[1] == 0x20 else "..", attr, cl, size, mt, deleted, dot="." if raw[1] == 0x20 else ".."))
            pend = []
            continue
        note = ""
        name = None
        if pend:
            slots = pend[::-1]
            cs = {s[13] for s in slots}
            if len(cs) == 1:
                c0 = next(iter(cs))
                ok = True
                if not deleted:
                    ok = lfn_checksum(bytes(raw)) == c0 and all((s[0] & 0x1F) == k + 1 for k, s in enumerate(slots))
                elif lfn_checksum(bytes(raw)) != c0:
                    ok = False
                    for cand in range(0x21, 0x100):      # 지워진 짧은 이름의 첫 글자를 체크섬으로 역산
                        raw[0] = cand
                        if lfn_checksum(bytes(raw)) == c0:
                            ok = True
                            break
                    if not ok:
                        raw[0] = 0xE5
                        note = "긴 이름 체크섬이 맞지 않아 이름이 부정확할 수 있음"
                        ok = True
                if ok:
                    name = _lfn_text(slots)
                else:
                    note = "긴 이름 항목이 짧은 이름과 맞지 않아 짧은 이름을 사용"
            else:
                note = "긴 이름 조각들이 서로 맞지 않아 짧은 이름을 사용"
        pend = []
        if not name:
            base, ext = bytes(raw[:8]).rstrip(b" "), bytes(raw[8:11]).rstrip(b" ")
            if deleted and raw[0] == 0xE5:
                base = b"_" + base[1:]
                note = note or "지워진 짧은 이름의 첫 글자를 알 수 없어 '_' 로 표시"
            bs, es = base.decode(oem, "replace"), ext.decode(oem, "replace")
            if e[12] & 0x08:
                bs = bs.lower()
            if e[12] & 0x10:
                es = es.lower()
            name = bs + ("." + es if es else "")
        out.append(DirEnt(name, attr, cl, size, mt, deleted, None, note))
    return out


def parse_exfat_dir(data: bytes) -> Tuple[List[DirEnt], Optional[Tuple[int, int]]]:
    """exFAT 폴더 내용 -> (항목들, 할당 비트맵(클러스터, 길이))"""
    out: List[DirEnt] = []
    bitmap = None
    n = len(data) // 32
    i = 0
    while i < n:
        t = data[i * 32]
        if t == 0:
            break
        if t == 0x81:
            bitmap = (_u32(data, i * 32 + 20), _u64(data, i * 32 + 24))
        if t in (0x85, 0x05):
            e = data[i * 32:(i + 1) * 32]
            cnt = e[1]
            deleted = t == 0x05
            if cnt < 2 or i + cnt >= n:
                i += 1
                continue
            sec = [data[(i + 1 + k) * 32:(i + 2 + k) * 32] for k in range(cnt)]
            st = sec[0]
            want = 0x40 if deleted else 0xC0
            if len(st) < 32 or st[0] != want:
                i += 1
                continue
            attr = _u16(e, 4)
            nlen = st[3]
            chars = b"".join(s[2:32] for s in sec[1:] if s[0] == (0x41 if deleted else 0xC1))
            name = chars[:nlen * 2].decode("utf-16-le", "replace")
            note = ""
            if not deleted and exfat_set_checksum(data[i * 32:(i + 1 + cnt) * 32]) != _u16(e, 2):
                note = "항목 세트 체크섬 불일치"
            t32 = _u32(e, 12)
            mt = dos_datetime(t32 >> 16, t32 & 0xFFFF)
            out.append(DirEnt(name, attr, _u32(st, 20), _u64(st, 24), mt, deleted, None, note,
                              nofat=bool(st[1] & 2), valid_len=_u64(st, 8)))
            i += 1 + cnt
            continue
        i += 1
    return out, bitmap


class FatScanner:
    def __init__(self, img, vol: Volume, fb: FatBoot, oem: str = OEM_CP, check_data: bool = False, root: str = ""):
        self.img, self.vol, self.fb, self.oem, self.check_data = img, vol, fb, oem, check_data
        self.root = root                 # --root (소문자 정규화). 비어 있으면 전체
        self.hint_paths: List[str] = []
        self.n = fb.n_clusters
        self.fat: Optional[_array.array] = None
        self.fat_mask = 0xFFFFFFFF
        self.eoc = 0xFFFFFFF8
        self.fat_diff = 0
        self.fat_used = 2
        self.rows: List[list] = []                   # [경로, Entry, 상태]
        self.live: List[tuple] = []                  # (Entry, runs, deleted)
        self.next_id = 100
        self.live_claim = bytearray(self.n + 3)
        self.del_claim = bytearray(self.n + 3)
        self.any_conflict = False
        self.visited: Set[int] = set()
        self.notes: List[str] = []
        self.stat: Counter = Counter()
        self.bitmap: Optional[bytes] = None
        self.bitmap_loc: Optional[Tuple[int, int]] = None
        self.root_ok = True
        self.t_last = 0.0

    # ---- 읽기 도우미 ----
    def _read(self, off: int, n: int) -> bytes:
        return self.img.read(self.vol.offset + off, n)

    def cluster_off(self, c: int) -> int:
        return self.fb.data_base + (c - 2) * self.fb.cluster

    def load_fat(self):
        fb = self.fb
        kind = fb.kind
        if kind in ("FAT32", "exFAT"):
            code = "I"
            self.fat_mask = 0x0FFFFFFF if kind == "FAT32" else 0xFFFFFFFF
            self.eoc = 0x0FFFFFF8 if kind == "FAT32" else 0xFFFFFFF8
        elif kind == "FAT16":
            code = "H"
            self.fat_mask, self.eoc = 0xFFFF, 0xFFF8
        else:
            code = None
            self.fat_mask, self.eoc = 0xFFF, 0xFF8
        want = min(fb.fat_len, (fb.n_clusters + 2) * (4 if code == "I" else 2 if code == "H" else 2))
        if kind == "FAT12":
            want = min(fb.fat_len, ((fb.n_clusters + 2) * 3 + 1) // 2 + 2)
        copies = []
        for k in range(fb.n_fats if not fb.is_exfat else 1):
            copies.append(fb.fat_off + k * fb.fat_len)
        arrs = []
        diff = 0
        for ci, base in enumerate(copies[:2]):
            raw_parts = []
            pos = 0
            prog = Progress(want, f"FAT{ci + 1} 읽기", as_bytes=True) if want > 32 * 1024 * 1024 else None
            while pos < want:
                n = min(64 * 1024 * 1024, want - pos)
                raw_parts.append(self._read(base + pos, n))
                pos += n
                if prog:
                    prog.update(pos)
            if prog:
                prog.done()
            arrs.append(b"".join(raw_parts))
        raw = arrs[0]
        if len(arrs) > 1 and len(arrs[1]) == len(raw):
            if arrs[1] != raw:
                diff = sum(1 for a in range(0, len(raw), 4096) if raw[a:a + 4096] != arrs[1][a:a + 4096])
        self.fat_diff = diff
        if len(arrs) > 1 and not any(raw[:4096]) and any(arrs[1][:4096]):
            raw = arrs[1]
            self.notes.append("FAT1 이 비어 있어 FAT2(백업)를 사용함")
        if code:
            a = _array.array(code)
            sz = a.itemsize
            a.frombytes(raw[:len(raw) // sz * sz])
            if sys.byteorder == "big":
                a.byteswap()
        else:   # FAT12
            a = _array.array("H")
            for i in range(0, len(raw) - 1, 3):
                b0, b1 = raw[i], raw[i + 1]
                a.append(b0 | ((b1 & 0x0F) << 8))
                if i + 2 < len(raw):
                    a.append((b1 >> 4) | (raw[i + 2] << 4))
        self.fat = a
        self.fat_used = len(a) - a.count(0)

    def nxt(self, c: int):
        """다음 클러스터. None=체인 끝, -1=비정상(빈칸/불량/범위밖)"""
        if c < 2 or c >= len(self.fat):
            return -1
        v = self.fat[c] & self.fat_mask
        if v >= self.eoc:
            return None
        if v < 2 or v >= self.n + 2:
            return -1
        return v

    def walk(self, start: int, need: int):
        """체인을 따라 최대 need 개 클러스터. -> (runs, count, 상태) 상태: ok / short / broken / long"""
        runs: List[list] = []
        c, cnt = start, 0
        need = min(need, self.n)
        while cnt < need:
            if c < 2 or c >= self.n + 2:
                return [tuple(r) for r in runs], cnt, "broken"
            if runs and runs[-1][0] + runs[-1][1] == c - 2:
                runs[-1][1] += 1
            else:
                runs.append([c - 2, 1])
            cnt += 1
            nx = self.nxt(c)
            if cnt >= need:
                return [tuple(r) for r in runs], cnt, ("ok" if nx is None else ("long" if nx != -1 else "ok"))
            if nx is None:
                return [tuple(r) for r in runs], cnt, "short"
            if nx == -1:
                return [tuple(r) for r in runs], cnt, "broken"
            c = nx
        return [], 0, "ok"

    def dir_chain(self, start: int) -> Tuple[List[int], bool]:
        out, seen, c = [], set(), start
        while c is not None and c != -1 and 2 <= c < self.n + 2 and c not in seen and len(out) < 1 << 20:
            seen.add(c)
            out.append(c)
            c = self.nxt(c)
        return out, c is None

    def read_dir(self, start: Optional[int], deleted: bool, nofat_len: Optional[int] = None) -> bytes:
        fb = self.fb
        if start is None:   # FAT12/16 고정 루트
            return self._read(fb.root_off, fb.root_len)
        if nofat_len is not None:
            n = -(-nofat_len // fb.cluster)
            return self._read(self.cluster_off(start), min(n, 1 << 14) * fb.cluster)
        if deleted:
            cl = [start]
            while len(cl) < 16 and start + len(cl) < self.n + 2 and self.fat[start + len(cl)] == 0:
                cl.append(start + len(cl))
            return self._read(self.cluster_off(start), len(cl) * fb.cluster)
        cl, _ = self.dir_chain(start)
        return b"".join(self._read(self.cluster_off(c), fb.cluster) for c in cl)

    # ---- 파일 데이터 위치/건강도 ----
    def file_runs(self, d: DirEnt, deleted: bool) -> Tuple[list, List[str], int]:
        fb = self.fb
        size, cl = d.size, fb.cluster
        need = -(-size // cl)
        issues: List[str] = []
        risk = 0
        if size == 0:
            return [], issues, 0
        start = d.cluster
        if not (2 <= start < self.n + 2):
            return [], ["시작 클러스터가 0 이거나 범위를 벗어남(데이터 위치를 알 수 없음)"], 2
        if need > self.n:
            issues.append("기록된 크기가 볼륨보다 큼(크기 정보 손상)")
            risk = 2
        if fb.is_exfat and d.nofat and not deleted:
            runs = [(start - 2, min(need, self.n + 2 - start))]
            if runs[0][1] < need:
                issues.append("연속 저장 구간이 볼륨 끝을 넘어감")
                risk = 2
            return runs, issues, risk
        if not deleted:
            runs, cnt, status = self.walk(start, need)
            if status in ("short", "broken"):
                issues.append(f"클러스터 체인이 중간에 끊김: 기록된 크기 {fmt_size(size)} 중 {fmt_size(min(cnt * cl, size))} 만 이어짐")
                risk = 2
            elif status == "long":
                issues.append("(참고) 클러스터 체인이 기록된 크기보다 김(크기 정보 또는 체인 불일치)")
            return runs, issues, risk
        # 삭제된 파일
        if fb.is_exfat:
            if d.nofat:
                runs = [(start - 2, min(need, self.n + 2 - start))]
                issues.append("삭제된 파일: 연속 저장 표시(NoFatChain)에 따라 복구")
            else:
                runs, cnt, status = self.walk(start, need)
                if status == "ok" or (status == "long" and cnt == need):
                    issues.append("삭제된 파일: 남아 있는 FAT 체인에 따라 복구")
                else:
                    runs = [(start - 2, min(need, self.n + 2 - start))]
                    issues.append("삭제된 파일: FAT 체인 정보가 없어 연속 저장을 가정하고 복구(조각났다면 내용이 다를 수 있음)")
            risk = max(risk, 1)
            used = 0
            if self.bitmap:
                used = sum(count_set_bits(self.bitmap, a, a + ln) for a, ln in runs)
            tot = sum(ln for _, ln in runs)
            if used:
                issues.append(f"{used}/{tot} 클러스터가 비트맵상 이미 다른 데이터에 할당됨(덮어쓰임)")
                risk = 2
            return runs, issues, risk
        last = min(start + need, self.n + 2)
        runs = [(start - 2, last - start)]
        if last - start < need:
            issues.append("연속 저장을 가정한 범위가 볼륨 끝을 넘어감")
            risk = 2
        free = self.fat[start:last].count(0)
        issues.append("삭제된 파일: 클러스터 체인 정보가 지워져 연속 저장을 가정하고 복구(조각났다면 내용이 다를 수 있음)")
        risk = max(risk, 1)
        if free < last - start:
            issues.append(f"{last - start - free}/{last - start} 클러스터가 이미 다른 파일에 재사용됨(내용 덮어쓰임)")
            risk = 2
        return runs, issues, risk

    def _claim(self, runs, deleted: bool):
        arr = self.del_claim if deleted else self.live_claim
        for a, ln in runs:
            b = min(a + ln, len(arr))
            if a >= b:
                continue
            if any(arr[a:b]):
                self.any_conflict = True
                for i in range(a, b):
                    arr[i] = 2 if arr[i] else 1
            else:
                arr[a:b] = b"\x01" * (b - a)

    def _progress(self):
        now = time.monotonic()
        if now - self.t_last >= 1.0:
            self.t_last = now
            sys.stderr.write("\r" + padw(fit(f"폴더 읽는 중... 폴더 {len(self.visited)}개 / 항목 {len(self.rows)}개"), term_cols()))
            sys.stderr.flush()

    # ---- 트리 순회 ----
    def new_entry(self, d: DirEnt, deleted: bool, parent_id: int) -> Entry:
        e = Entry(self.next_id, 1, 0, not deleted, d.is_dir, 0)
        self.next_id += 1
        e.parent, e.name, e.size, e.mtime = parent_id, d.name, 0 if d.is_dir else d.size, d.mtime
        e.source = "EXFAT" if self.fb.is_exfat else "FAT"
        e.data_nonresident = True
        e.data_size_known = True
        e.has_data_attr = not d.is_dir
        if d.note:
            e.issues.append(d.note)
            e.risk = 1 if d.deleted or "체크섬" in d.note else 0
        return e

    def walk_tree(self, roots: List[tuple]):
        """roots: (폴더 시작 클러스터 또는 None(고정 루트), 경로, 부모 id, 삭제 여부, 상태, nofat 길이)"""
        stack = list(roots)
        fb = self.fb
        while stack:
            start, path, pid, pdel, status, nlen = stack.pop()
            if start is not None:
                if start in self.visited:
                    continue
                self.visited.add(start)
            try:
                data = self.read_dir(start, pdel, nlen)
            except OSError:
                data = b""
            if fb.is_exfat:
                ents, bm = parse_exfat_dir(data)
                if bm and self.bitmap_loc is None and path == "":
                    self.bitmap_loc = bm
            else:
                ents = parse_fat_dir(data, fb.kind == "FAT32", self.oem)
            self._progress()
            for d in ents:
                if d.dot or not d.name:
                    continue
                deleted = pdel or d.deleted
                full = f"{path}/{d.name}" if path else d.name
                emit = True
                if self.root:
                    fl = full.lower()
                    if len(self.hint_paths) < 20000:
                        self.hint_paths.append(full)
                    inside = fl == self.root or fl.startswith(self.root + "/")
                    if not inside and not self.root.startswith(fl + "/"):
                        continue          # --root 와 무관한 경로는 읽지 않는다
                    emit = inside         # 상위 폴더는 내려가기만 하고 목록에는 넣지 않는다
                if not emit and not d.is_dir:
                    continue
                e = self.new_entry(d, deleted, pid)
                if d.is_dir:
                    ok = 2 <= d.cluster < self.n + 2
                    if not ok:
                        e.issues.append("폴더의 시작 클러스터가 비정상(내용을 읽을 수 없음)")
                        e.risk = 2
                    elif deleted and not fb.is_exfat:
                        head = self._read(self.cluster_off(d.cluster), 64)
                        if head[:11] != FAT_DOT or (_u16(head, 20) << 16 | _u16(head, 26) if fb.kind == "FAT32" else _u16(head, 26)) != d.cluster:
                            ok = False
                            e.issues.append("삭제된 폴더: 내용이 이미 덮어쓰여 하위 항목을 찾을 수 없음")
                            e.risk = max(e.risk, 1)
                    if ok:
                        stack.append((d.cluster, full, e.recno, deleted, status, d.size if (fb.is_exfat and d.nofat) else None))
                    if emit:
                        self.rows.append([full, e, status])
                        self.stat["dir"] += 1
                    continue
                runs, iss, risk = self.file_runs(d, deleted)
                e.runs = [r for r in runs]
                e.issues.extend(iss)
                e.risk = max(e.risk, risk)
                if not deleted and d.valid_len is not None and d.valid_len < d.size:
                    e.issues.append("유효 데이터 길이가 파일 크기보다 작음(뒷부분은 기록된 적 없는 영역)")
                    e.risk = max(e.risk, 1)
                self._claim(runs, deleted)
                self.live.append((e, runs, deleted))
                self.rows.append([full, e, status])
                self.stat["del" if deleted else "file"] += 1

    def load_bitmap(self):
        if not self.fb.is_exfat or not self.bitmap_loc:
            return
        c, ln = self.bitmap_loc
        if 2 <= c < self.n + 2 and 0 < ln <= (self.n + 7) // 8 + 4096:
            self.bitmap = self._read(self.cluster_off(c), ln)

    def finish(self):
        """교차 연결/겹침 판정, 내용 확인"""
        cl = self.fb.cluster
        if self.any_conflict:
            for e, runs, deleted in self.live:
                if not runs:
                    continue
                if not deleted:
                    if any(b"\x02" in self.live_claim[a:a + ln] for a, ln in runs):
                        e.issues.append("다른 파일과 같은 클러스터를 사용(교차 연결): 어느 한쪽 내용은 틀릴 수 있음")
                        e.risk = 2
                else:
                    if any(any(self.live_claim[a:a + ln]) for a, ln in runs):
                        if not any("재사용" in m or "할당됨" in m for m in e.issues):
                            e.issues.append("삭제 후 다른 파일이 같은 클러스터를 사용 중(덮어쓰임)")
                        e.risk = 2
                    elif any(b"\x02" in self.del_claim[a:a + ln] for a, ln in runs):
                        e.issues.append("다른 삭제 파일과 같은 클러스터를 사용(둘 중 하나는 덮어쓰였을 수 있음)")
                        e.risk = max(e.risk, 1)
        elif self.fat is not None:
            pass
        if self.check_data:
            prog = Progress(len(self.live), "내용 확인", as_bytes=False)
            for k, (e, runs, deleted) in enumerate(self.live, 1):
                if runs and e.size > 0:
                    head = self._read(self.fb.data_base + runs[0][0] * cl, 32)
                    e.sig = sig_check(e.name or "", head, e.size)
                    if e.sig == "bad":
                        e.issues.append("파일 시작 부분이 확장자 시그니처와 다르거나 0으로 채워져 있음(내용 유실 가능)")
                        e.risk = 2
                if k % 2000 == 0:
                    prog.update(k)
            prog.done()


def fat_scan_dirs(img, vol: Volume, fb: FatBoot) -> Dict[int, int]:
    """볼륨 전체에서 폴더 클러스터(앞머리가 '.' '..' 항목)를 찾는다. -> {클러스터: 부모 클러스터(0=루트)}"""
    out: Dict[int, int] = {}
    pat = FAT_DOT + b"\x10"
    total = fb.n_clusters * fb.cluster
    chunk = max(16 * 1024 * 1024 // fb.cluster, 1) * fb.cluster
    prog = Progress(max(total, 1), "폴더 클러스터 스캔", as_bytes=True)
    pos = 0
    t = time.monotonic()
    while pos < total:
        data = img.read(vol.offset + fb.data_base + pos, min(chunk, total - pos) + 64)
        i = data.find(pat)
        while i != -1:
            if i % fb.cluster == 0 and i + 64 <= len(data) and data[i + 32:i + 43] == FAT_DOTDOT:
                c = pos // fb.cluster + i // fb.cluster + 2
                hi = _u16(data, i + 20) if fb.kind == "FAT32" else 0
                me = _u16(data, i + 26) | (hi << 16)
                if me == c:
                    hp = _u16(data, i + 52) if fb.kind == "FAT32" else 0
                    out[c] = _u16(data, i + 58) | (hp << 16)
            i = data.find(pat, i + 1)
        pos += chunk
        prog.update(min(pos, total), f"폴더 후보 {len(out)}개")
    prog.done(f"폴더 후보 {len(out)}개")
    return out


def main_fat(args, img, vol: Volume, pt: Paint) -> int:
    sec = img.read(vol.offset, 512)
    kind0 = detect_fs(sec)
    fb: Optional[FatBoot] = None
    boot_note = ""
    if kind0 == "exFAT":
        fb = parse_exfat_boot(sec, vol.size)
    elif kind0 == "FAT":
        fb = parse_fat_boot(sec, vol.size)
    if fb is None or fb.problems:   # 백업 부트섹터 시도
        for lba, label in ((6, "FAT32 백업 부트섹터(6번)"), (12, "exFAT 백업 부트섹터(12번)")):
            b2 = img.read(vol.offset + lba * 512, 512)
            k2 = detect_fs(b2)
            if k2 in ("FAT", "exFAT") and (k2 == "exFAT") == (lba == 12):
                f2 = parse_exfat_boot(b2, vol.size) if k2 == "exFAT" else parse_fat_boot(b2, vol.size)
                if not f2.problems:
                    boot_note = f"주 부트섹터가 손상되어 {label}를 사용함" if fb is not None or kind0 is None else ""
                    fb = f2
                    break
    if fb is None or fb.problems:
        print(pt.red("\n[!] FAT/exFAT 부트섹터를 해석하지 못했습니다: " + (", ".join(fb.problems) if fb else "시그니처 없음")))
        return 1
    print(f"\n[파일시스템] {fb.kind} / 클러스터 {fmt_size(fb.cluster)} x {fb.n_clusters}개 / 섹터 {fb.bps}B"
          + (f" / {boot_note}" if boot_note else ""))
    sc = FatScanner(img, vol, fb, args.oem_cp, args.check_data, root_key(args.root))
    sc.load_fat()
    sys.stderr.write("\r" + " " * 100 + "\r")
    roots: List[tuple] = []
    if fb.kind in ("FAT12", "FAT16"):
        roots.append((None, "", 0, False, "ok", None))
    else:
        roots.append((fb.root_cluster, "", 0, False, "ok", None))
    if fb.is_exfat and sc.fat is not None:
        # 루트가 NoFatChain 이 아니므로 FAT 체인을 따른다 (비트맵 위치는 루트 폴더 항목에서)
        pass
    sc.walk_tree(roots)
    sys.stderr.write("\r" + " " * 100 + "\r")
    sc.load_bitmap()
    if sc.bitmap is not None:   # 비트맵은 루트를 읽은 뒤에야 알 수 있으므로 삭제 파일 판정을 다시 한다
        for e, runs, deleted in sc.live:
            if deleted and runs and fb.is_exfat:
                used = sum(count_set_bits(sc.bitmap, a, a + ln) for a, ln in runs)
                tot = sum(ln for _, ln in runs)
                if used and not any("비트맵상" in m for m in e.issues):
                    e.issues.append(f"{used}/{tot} 클러스터가 비트맵상 이미 다른 데이터에 할당됨(덮어쓰임)")
                    e.risk = 2
    lost_found = 0
    root_empty = sc.stat["file"] + sc.stat["dir"] + sc.stat["del"] == 0 and not sc.root
    if args.mode == "scan" and not fb.is_exfat and fb.kind != "FAT12":
        dirs = fat_scan_dirs(img, vol, fb)
        lost = {c: p for c, p in dirs.items() if c not in sc.visited}
        lost_found = len(lost)
        tops = [c for c, p in lost.items() if p not in lost]
        lroots = [(c, f"[유실된폴더_{c}]", 0, False, "orphan", None) for c in sorted(tops)]
        before = len(sc.rows)
        sc.walk_tree(lroots)
        rest = [c for c in lost if c not in sc.visited]
        sc.walk_tree([(c, f"[유실된폴더_{c}]", 0, False, "orphan", None) for c in sorted(rest)])
        sys.stderr.write("\r" + " " * 100 + "\r")
        print(f"[폴더 스캔] 루트에서 이어지지 않는 폴더 {lost_found}개 발견 -> 항목 {len(sc.rows) - before}개 추가([유실된폴더_클러스터번호] 아래)")
    elif args.mode == "scan" and fb.is_exfat:
        print("[폴더 스캔] exFAT 은 폴더 스캔을 지원하지 않습니다(루트부터 읽은 결과만 표시)")
    sc.finish()
    if sc.root:
        print(f"[--root] /{norm_path(args.root)} 아래만 읽었습니다. 범위 밖 파일과의 교차 연결(클러스터 공유)은 검사하지 못합니다.")

    rows = sc.rows
    for path, e, st in rows:
        if st == "orphan":
            e.issues.append("(참고) 상위 폴더 경로를 복원하지 못함")
    files = [e for _p, e, _s in rows if not e.is_dir]
    cnt = Counter(e.risk for e in files)
    # ---- 디스크 상태 ----
    reasons: List[Tuple[int, str]] = []
    if boot_note:
        reasons.append((1, boot_note))
    if sc.fat_diff:
        reasons.append((1, f"FAT1 과 FAT2 가 서로 다름(다른 {sc.fat_diff}개 4KiB 구간)"))
    if root_empty:
        reasons.append((2, "루트 폴더에서 항목을 하나도 읽지 못함(루트 폴더 손상 가능)"
                        + (" -> --mode scan 으로 폴더를 직접 찾아 보세요" if (not fb.is_exfat and args.mode != "scan") else "")))
    if files:
        bad_r = cnt[2] / len(files)
        if bad_r > 0.02:
            reasons.append((2, f"손상추정 파일 {cnt[2]}개 ({bad_r:.1%})"))
        elif cnt[2]:
            reasons.append((1, f"손상추정 파일 {cnt[2]}개 ({bad_r:.2%})"))
    if sc.fat is not None and fb.kind != "exFAT":
        free_ratio = sc.fat.count(0) / max(len(sc.fat), 1)
        if sc.fat_used <= 4 and fb.n_clusters > 100:
            reasons.append((2, "FAT 이 사실상 비어 있음(포맷/초기화됐거나 FAT 손상) -> 정상 파일도 삭제 파일처럼 연속 저장 가정으로 복구됨"))
    if lost_found:
        reasons.append((1, f"루트에서 이어지지 않는 폴더 {lost_found}개를 스캔으로 찾음"))
    reasons.extend((1, n) for n in sc.notes)
    worst = max([r[0] for r in reasons], default=0)
    grade = {0: "양호", 1: "주의", 2: "심각한 손상"}[worst]
    print(f"\n[디스크 상태] {grade}")
    if not reasons:
        print("  - 특이사항 없음")
    for _l, r in reasons:
        print(f"  - {r}")
    print(f"\n[검사 요약] 폴더 {sc.stat['dir']}개 / 파일 {sc.stat['file']}개 / 삭제된 파일 {sc.stat['del']}개 / 이미지 읽기 오류 {img.read_errors}")
    print(f"[파일 건강도] 전체 파일 {len(files)}개: 정상 {cnt[0]} / 의심 {cnt[1]} / 손상추정 {cnt[2]}"
          + ("" if args.check_data else "   (--check-data 를 주면 파일 내용 시작 부분도 확인)"))
    ic: Counter = Counter()
    for e in files:
        for m in e.issues:
            if not m.startswith("(참고)"):
                ic[re.sub(r"\d+/\d+|\d+(\.\d+)? ?[KMGT]?i?B", "N", m)] += 1
    for m, c in ic.most_common(8):
        print(f"  - {m}: {c}개")
    print("  ※ FAT 계열은 삭제하면 클러스터 연결 정보가 지워집니다. 삭제된 파일은 '연속 저장'을 가정해 복구하므로 조각난 파일은 내용이 어긋날 수 있습니다(의심 표시).")

    def is_system(e: Entry) -> bool:
        return False

    def visible(e: Entry) -> bool:
        if args.deleted_only and e.in_use:
            return False
        if args.only_issues and e.risk == 0:
            return False
        return True

    shown = sorted((r for r in rows if visible(r[1])), key=lambda r: r[0].lower())
    if not args.no_csv and not out_blocked(args, pt, args.csv, "결과 CSV"):
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["경로", "종류", "상태", "건강도", "문제점", "크기(바이트)", "수정시간", "번호", "출처", "경로복원", "데이터시작클러스터"])
            for path, e, st in shown:
                first = (e.runs[0][0] + 2) if e.runs else ""
                w.writerow(["/" + path, "폴더" if e.is_dir else "파일", "정상" if e.in_use else "삭제", RISK_LABEL[e.risk],
                            " | ".join(e.issues), "" if e.is_dir else e.size, e.mtime or "", e.recno, e.source, st, first])
        print(f"\nCSV 저장: {os.path.abspath(args.csv)} ({len(shown)}개 항목)")
    print_root_scope(args, rows, pt, sc.hint_paths)
    _lines = []
    for path, e, st in shown:
        mark = ("D" if e.is_dir else "-") + ("x" if not e.in_use else " ")
        size = "" if e.is_dir else fmt_size(e.size)
        mt = e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else "-"
        _lines.append(color_row(f"{mark} {padw(RISK_LABEL[e.risk], 8)} {size:>10}  {mt:16}  /{path}", e, pt))
    print_listing(_lines, args.head, args.page, "[파일 목록]")
    r = tree_and_copy(args, img, vol, fb, None, rows, visible, pt)
    if r is not None:
        return r
    print("\n범례: D=폴더, x=삭제된 항목, [유실된폴더_N]=루트에서 이어지지 않아 클러스터 번호로 묶은 폴더")
    return 0
# === FAT-END ===


def print_listing(lines: List[str], head: int, page: Optional[int], total_label: str) -> None:
    """파일 목록 출력. --page N 이면 N 줄씩 끊어 보여 주고(Enter=다음, 번호=그 쪽, q=끝), 아니면 --head 개수만 출력한다."""
    n = len(lines)
    if page and sys.stdin is not None and sys.stdin.isatty():
        pages = max(1, (n + page - 1) // page)
        cur = 0
        while True:
            print(f"\n{total_label} 총 {n}개 - {cur + 1}/{pages}쪽 ({cur * page + 1}~{min(n, (cur + 1) * page)})")
            for ln in lines[cur * page:(cur + 1) * page]:
                print(fit(ln))
            if pages == 1:
                return
            try:
                ans = input(f"[Enter=다음 쪽 / p=이전 / 쪽번호(1~{pages}) / q=목록 끝내기] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if ans in ("q", "x"):
                return
            if ans == "p":
                cur = max(0, cur - 1)
            elif ans.isdigit() and 1 <= int(ans) <= pages:
                cur = int(ans) - 1
            elif cur + 1 < pages:
                cur += 1
            else:
                return
    print(f"\n{total_label} 총 {n}개" + (f" (앞 {head}개만 표시, 전체는 --head 0 / 쪽 단위 보기는 --page 50 / CSV)"
                                      if head and n > head else ""))
    for ln in (lines[:head] if head else lines):
        print(fit(ln))


def list_dest_candidates() -> List[Tuple[str, str, str, int]]:
    """저장 위치 후보 [(경로, 종류, 이름, 여유바이트)]. Windows 는 드라이브 문자, 그 외는 마운트 지점."""
    out: List[Tuple[str, str, str, int]] = []
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            mask = k.GetLogicalDrives()
            kinds = {2: "USB/이동식", 3: "고정 디스크", 4: "네트워크", 5: "CD/DVD", 6: "램디스크"}
            for i in range(26):
                if not mask & (1 << i):
                    continue
                root = f"{chr(65 + i)}:\\"
                t = k.GetDriveTypeW(root)
                if t not in kinds or t == 5:
                    continue
                label = ctypes.create_unicode_buffer(261)
                k.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
                free = ctypes.c_ulonglong(0)
                tot = ctypes.c_ulonglong(0)
                ok = k.GetDiskFreeSpaceExW(root, None, ctypes.byref(tot), ctypes.byref(free))
                if not ok:
                    continue
                out.append((root, kinds[t], label.value, free.value))
        else:
            skip = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "overlay", "squashfs", "autofs",
                    "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs", "fusectl", "hugetlbfs", "ramfs", "binfmt_misc"}
            seen = set()
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    parts = ln.split()
                    if len(parts) < 3 or parts[2] in skip or parts[1].startswith(("/proc", "/sys", "/dev", "/run/lock", "/snap")):
                        continue
                    mp = parts[1].replace("\\040", " ")
                    if mp in seen:
                        continue
                    seen.add(mp)
                    try:
                        free = shutil.disk_usage(mp).free
                    except OSError:
                        continue
                    out.append((mp, parts[2], parts[0], free))
    except Exception:  # noqa: BLE001
        pass
    return out


def parent_dir(p: str) -> Optional[str]:
    """상위 폴더(없으면 None). Windows 드라이브 루트 E:\\ 와 / 위로는 올라가지 않는다."""
    t = p.rstrip("\\/")
    if not t:
        return None
    up = os.path.dirname(t)
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", up):
        up += "\\"
    if not up or up == p:
        return None
    return up


def browse_dest(base: str, pt) -> Optional[str]:
    """저장 위치를 폴더 트리를 타고 내려가며 고른다. Enter = 현재 폴더 아래 recovered 에 저장, q = 취소(None)."""
    cur = base
    name = "recovered"
    page = 0
    PAGE = 30
    while True:
        try:
            subs = sorted((e.name for e in os.scandir(cur) if e.is_dir(follow_symlinks=False)), key=str.lower)
        except OSError as e:
            print(pt.red(f"  폴더를 열 수 없습니다: {e}"))
            up = parent_dir(cur)
            if not up:
                return None
            cur = up
            continue
        subs = [x for x in subs if x not in ("$RECYCLE.BIN", "System Volume Information", "$Recycle.Bin")]
        print(f"\n[현재 위치] {cur}   (하위 폴더 {len(subs)}개)")
        show = subs[page * PAGE:(page + 1) * PAGE]
        for i, x in enumerate(show, page * PAGE + 1):
            print(f"  {i:3d}. {x}/")
        if len(subs) > PAGE:
            print(f"  ({page + 1}/{(len(subs) + PAGE - 1) // PAGE}쪽, + / - 로 쪽 이동)")
        print(f"  번호=그 폴더로 이동 / ..=상위 / n 이름=만들 폴더 이름 변경 / Enter=여기에 '{name}' 폴더를 만들고 저장 / q=취소")
        try:
            ans = input(f"{cur}> ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            return None
        if not ans:
            return os.path.join(cur, name)
        low = ans.lower()
        if low == "q":
            return None
        if ans == "+":
            if (page + 1) * PAGE < len(subs):
                page += 1
            continue
        if ans == "-":
            page = max(0, page - 1)
            continue
        if ans == "..":
            up = parent_dir(cur)
            if up:
                cur, page = up, 0
            else:
                print("  더 위로 갈 수 없습니다.")
            continue
        if low == "n" or low.startswith("n "):
            nn = ans[1:].strip()
            if nn and not re.search(r'[\\/:*?"<>|]', nn):
                name = nn
            else:
                print("  폴더 이름이 비었거나 사용할 수 없는 문자가 있습니다.")
            continue
        if ans.isdigit() and 1 <= int(ans) <= len(subs):
            cur = os.path.join(cur, subs[int(ans) - 1])
            page = 0
            continue
        if ans in subs:
            cur = os.path.join(cur, ans)
            page = 0
            continue
        print("  알 수 없는 입력입니다.")


def choose_dest(args, pt, default: Optional[str]) -> Optional[str]:
    """복사 저장 위치를 목록에서 번호로 고르거나 경로를 직접 입력받는다. 빈 입력은 기본값, 취소는 None."""
    cands = list_dest_candidates()
    print("\n[저장 위치 선택] 복구한 파일을 저장할 곳을 고르세요 (원본과 다른 디스크 권장)")
    ok_idx = {}
    for i, (path, kind, name, free) in enumerate(cands, 1):
        why = dest_conflicts_with_source(args.image, os.path.abspath(path)) if args.image else None
        mark = ""
        if why:
            mark = pt.red("  <- 읽는 원본과 같은 디스크: 선택 불가")
        else:
            ok_idx[i] = path
        nm = f" [{name}]" if name else ""
        print(f"  {i:2d}. {path:<14} {kind}{nm}  여유 {fmt_size(free)}{mark}")
    if not cands:
        print("  (자동으로 찾은 저장 위치가 없습니다)")
    print("  번호 = 목록에서 선택 / 경로 직접 입력 = 예) F:\\recovered / Enter = " + (default if default else "취소") + " / q = 취소")
    while True:
        try:
            ans = input("저장 위치: ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            return None
        if ans.lower() == "q":
            return None
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(cands):
            n = int(ans)
            if n not in ok_idx:
                print(pt.red("  원본과 같은 디스크는 고를 수 없습니다. 다른 번호를 고르세요."))
                continue
            res = browse_dest(ok_idx[n], pt)
            if res is None:
                print("  (목록으로 돌아갑니다. 다시 선택하거나 q 로 취소하세요)")
                continue
            return res
        return ans


def tree_and_copy(args, img, vol, boot, st_mft, rows, visible, pt) -> Optional[int]:
    """폴더 트리 출력 / 선택 / 복사(NTFS, FAT 공용). 복사를 했으면 종료 코드를, 아니면 None 을 돌려준다."""
    if args.tree is not None or args.browse or args.copy_to:
        vis_rows = [r for r in rows if visible(r[1])]
        root = build_tree(vis_rows)
        items_all = [(norm_path(p), e) for p, e, _s in vis_rows if not e.is_dir]
        if args.tree is not None:
            print(f"\n[폴더 트리] 깊이 {args.tree} (노랑=손상추정 파일 포함, 대상 세대: --gen {args.gen})")
            print(fmt_node_line(root, pt))
            print_tree(root, max(args.tree, 1), pt, 1)
        sel = Selection()
        for x in args.select:
            nd = find_node(root, x)
            if nd is not None and (nd.nfiles or nd.kids or norm_path(x) == ""):
                sel.add_dir(x)
            else:
                sel.add_file(x)
        if args.select_file:
            with open(args.select_file, encoding="utf-8-sig") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        nd = find_node(root, ln)
                        (sel.add_dir if nd is not None else sel.add_file)(ln)
        for x in args.exclude:
            sel.add_excl(x)
        if args.root and args.copy_to and sel.empty() and not args.browse:
            if find_node(root, args.root) is not None:
                sel.add_dir(args.root)
                print(f"\n[--root] 선택 항목이 없어 /{norm_path(args.root)} 아래 전체를 복사 대상으로 합니다.")
        go = bool(args.copy_to and not sel.empty())
        if args.copy_to and sel.empty() and not args.browse:
            print(pt.yel("\n--copy-to 를 쓰려면 --select 경로(또는 --select-file, --browse)로 복구할 항목을 지정하세요. 예: --select /폴더이름  (전체는 --select /)"))
        dest_cur = args.copy_to
        bstate: dict = {}
        if st_mft is not None and isinstance(boot, Boot):
            def _usn_provider():
                last = [-1]

                def prog(d, t):
                    pc = int(d * 100 / max(t, 1))
                    if pc != last[0] and pc % 5 == 0:
                        last[0] = pc
                        sys.stdout.write(f"\r  변경 저널 읽는 중... {pc}%   ")
                        sys.stdout.flush()
                evs, msg = load_usn(img, vol, boot, st_mft, rows, prog)
                sys.stdout.write("\r" + " " * 40 + "\r")
                return (usn_delete_view(evs, rows) if evs else []), msg
            bstate["usn_provider"] = _usn_provider
        if args.root and find_node(root, args.root) is not None:
            bstate["cur"] = find_node(root, args.root)     # 탐색기는 --root 폴더에서 시작
        run_no = 0
        rc = 0
        while True:
            if args.browse:
                go = interactive_browse(root, items_all, sel, pt, bstate)
                if not go:
                    break
                dest_cur = choose_dest(args, pt, dest_cur)
                if not dest_cur:
                    print("저장 경로가 없어 복사를 취소했습니다.")
                    continue
            if not (go and dest_cur):
                break
            dest_abs = os.path.abspath(dest_cur)
            why = dest_conflicts_with_source(args.image, dest_abs)
            if why:
                print(pt.red(why))
                if not args.browse:
                    return 2
                continue
            plan = plan_copy(items_all, sel, args.skip_deleted, args.skip_damaged)
            run_no += 1
            rep_base = args.report or os.path.join(os.path.dirname(dest_abs) or ".", "ntfs_copy_report.csv")
            rep = rep_base if run_no == 1 else f"{os.path.splitext(rep_base)[0]}_{run_no}{os.path.splitext(rep_base)[1]}"
            if out_blocked(args, pt, rep, "복사 보고서"):
                rep = os.path.join(dest_abs, os.path.basename(rep))
            print(f"\n[복사 #{run_no}] 선택 {len(sel.dirs)}개 폴더 / {len(sel.files)}개 파일 / 제외 {len(sel.excl)}개 -> 대상 파일 {len(plan)}개"
                  f" (--gen {args.gen}" + (", 삭제 제외" if args.skip_deleted else "") + (", 손상추정 제외" if args.skip_damaged else "") + ")")
            rc = run_copy(img, vol, boot, st_mft, plan, dest_cur, not args.no_tag, args.overwrite, args.force, rep, pt)
            if not args.browse:
                return rc
            sel.dirs.clear(); sel.files.clear(); sel.excl.clear()
            print(pt.grn("\n복사가 끝났습니다. 같은 목록에서 다음 폴더를 이어서 선택하세요. (q = 종료)"))
        return rc
    return None


def parse_range(s: str) -> Tuple[int, int]:
    a, _, b = s.partition(":")
    return int(float(a or 0) * GIB), int(float(b) * GIB) if b else 1 << 62


def in_ranges(starts: List[int], ranges: List[Tuple[int, int]], off: int) -> bool:
    i = bisect_right(starts, off) - 1
    return i >= 0 and ranges[i][0] <= off < ranges[i][1]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows 콘솔에서 출력 불가 문자로 죽지 않게
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="NTFS 이미지 파일 목록 추출 (읽기 전용)")
    ap.add_argument("image", nargs="?", help="디스크 이미지(.dd/.img/.raw/.001/.E01) 또는 장치: \\\\.\\PhysicalDrive1, E:, /dev/sdb")
    ap.add_argument("--partition", type=int, help="사용할 볼륨 번호 (목록에서 확인)")
    ap.add_argument("--offset", type=int, help="NTFS 볼륨 시작 오프셋(바이트). 지정하면 파티션 탐색 생략")
    ap.add_argument("--size", type=int, help="--offset 사용 시 볼륨 크기(바이트). 기본: 이미지 끝까지")
    ap.add_argument("--mode", choices=["auto", "mft", "scan"], default="auto")
    ap.add_argument("--cache", help="스캔 결과 저장/재사용 파일 (예: scan.pkl). 있으면 스캔을 건너뛰고 그 결과를 이용")
    ap.add_argument("--scan-range", help="스캔할 볼륨 범위(GiB) 'START:END' (예: 0:200)")
    ap.add_argument("--csv", default="ntfs_list_result.csv", help="결과 CSV 경로 (기본: ntfs_list_result.csv)")
    ap.add_argument("--no-csv", action="store_true", help="CSV 저장 안 함")
    ap.add_argument("--head", type=int, default=30, help="콘솔에 출력할 목록 개수 (0=전체, 기본 30)")
    ap.add_argument("--page", type=int, nargs="?", const=50, default=None, metavar="줄수",
                    help="목록 전체를 쪽 단위로 넘겨 보기 (기본 50줄; Enter=다음 쪽, p=이전, 번호=이동, q=끝). --head 보다 우선")
    ap.add_argument("--all", action="store_true", help="$MFT 등 시스템 메타파일도 표시")
    ap.add_argument("--deleted-only", action="store_true", help="삭제된 항목만 표시")
    ap.add_argument("--only-issues", action="store_true", help="의심/손상추정 항목만 표시")
    ap.add_argument("--check-data", action="store_true", help="각 파일의 시작 부분을 읽어 확장자 시그니처와 대조(느릴 수 있음)")
    ap.add_argument("--no-probe", action="store_true", help="선언된 $MFT 끝 뒤 영역 탐사를 생략")
    ap.add_argument("--gen", choices=["current", "old", "all"], default="current",
                    help="목록/CSV 에 낼 세대: current=현재 세대(기본), old=이전 세대(MFT 연장 구간), all=전부")
    ap.add_argument("--drive", type=int, metavar="N", help="Windows 물리 디스크 번호(= \\\\.\\PhysicalDriveN). 읽기 전용")
    ap.add_argument("--list-drives", action="store_true", help="연결된 물리 디스크와 볼륨 후보를 나열하고 종료")
    ap.add_argument("--oem-cp", default="cp949", help="FAT 짧은 이름(8.3)의 문자 코드(기본 cp949, 일본어는 cp932)")
    ap.add_argument("--root", metavar="경로", help="이 경로(폴더) 아래만 목록/트리/내용확인/복구 대상으로 삼음. 예: --root /Users/나/Documents "
                                                   "(NTFS 는 $MFT 전체를 읽어야 해서 목록 읽기는 빨라지지 않지만 내용 확인과 복사가 줄고, FAT 는 해당 폴더만 읽음)")
    g = ap.add_argument_group("복구(복사) - 원본 이미지는 읽기만 합니다")
    g.add_argument("--tree", type=int, nargs="?", const=2, metavar="깊이", help="폴더 트리 요약 출력(기본 깊이 2). 폴더별 파일 수/삭제/의심/손상추정 표시")
    g.add_argument("--browse", action="store_true", help="대화형 폴더 탐색기로 복구할 폴더/파일을 고른 뒤 복사")
    g.add_argument("--copy-to", metavar="폴더", help="선택한 항목을 이 폴더에 폴더 구조 그대로 복사 (원본과 다른 디스크 권장)")
    g.add_argument("--select", action="append", default=[], metavar="경로", help="복구할 폴더/파일 경로(여러 번 지정 가능). 예: --select /CLEARED_EGame_exe/Save  (/ 만 쓰면 전체)")
    g.add_argument("--select-file", metavar="파일", help="복구할 경로 목록 파일(한 줄에 하나, # 은 주석)")
    g.add_argument("--exclude", action="append", default=[], metavar="경로", help="선택에서 뺄 폴더/파일 경로")
    g.add_argument("--skip-deleted", action="store_true", help="삭제된 항목은 복사하지 않음")
    g.add_argument("--skip-damaged", action="store_true", help="손상추정 파일은 복사하지 않음")
    g.add_argument("--no-tag", action="store_true", help="손상추정 파일 이름에 [손상추정] 표시를 붙이지 않음")
    g.add_argument("--overwrite", action="store_true", help="저장 위치에 같은 이름이 있으면 덮어씀(기본: 같은 크기면 건너뛰고 다르면 번호를 붙여 저장)")
    g.add_argument("--force", action="store_true", help="저장 공간 부족 경고를 무시")
    g.add_argument("--report", help="복사 결과 보고서 CSV 경로(기본: 저장 폴더 옆 ntfs_copy_report.csv)")
    g.add_argument("--no-color", action="store_true", help="색상 끄기")
    _argv = list(argv) if argv is not None else sys.argv[1:]
    ap.add_argument("--menu", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--ctrlc", action="store_true", help="Ctrl+C 로 프로그램을 중단할 수 있게 함(기본은 Ctrl+C 무시)")
    args = ap.parse_args(_argv)
    pt = Paint(enable_color(args.no_color))
    if args.list_drives:
        return list_drives()
    if args.drive is not None:
        args.image = f"\\\\.\\PhysicalDrive{args.drive}"
    if not args.image:
        ap.error("디스크 이미지/장치 경로가 필요합니다 (장치 목록은 --list-drives)")
    args.image = normalize_source(args.image)

    try:
        img = open_image(args.image)
    except PermissionError:
        print(pt.red("[!] 장치를 열 수 없습니다(권한 없음). Windows 는 '관리자 권한으로 실행'한 cmd 에서, Linux 는 sudo 로 실행하세요."))
        return 1
    except OSError as ex:
        print(pt.red(f"[!] 열 수 없습니다: {ex}"))
        return 1
    is_dev = isinstance(img, DeviceImage)
    if is_dev and not any(a == "--csv" or a.startswith("--csv=") for a in _argv):
        args.no_csv = True    # 장치를 읽을 때는 기본 위치(현재 폴더)가 원본 디스크일 수 있어 CSV 를 자동 저장하지 않음
    print(f"{'장치(읽기 전용)' if is_dev else '이미지'}: {args.image}  ({fmt_size(img.size)})")
    if is_dev:
        print(pt.yel("  * 장치를 읽기 전용으로 열었습니다. 이 프로그램은 원본에 쓰지 않습니다. 복구할 파일은 반드시 '다른 디스크'에 저장하세요."))

    if args.offset is not None:
        vol = Volume(args.offset, args.size or (img.size - args.offset), "사용자 지정", True)
    else:
        vols = discover_volumes(img)
        print("\n[볼륨 후보]")
        _lt = {}
        if is_device_path(args.image or ""):
            _m = re.search(r"(?i)physicaldrive(\d+)$", args.image)
            _dk = int(_m.group(1)) if _m else args.image
            _lt = {o: val for (d, o), val in volume_letters().items() if d == _dk}
        for i, v in enumerate(vols, 1):
            _h = next((val for o, val in _lt.items() if abs(o - v.offset) < 1 << 20), None)
            _w = f" [{_h[0]}{' ' + _h[1] if _h[1] else ''}]" if _h else ""
            print(f"  {i}.{_w} offset={v.offset} ({fmt_size(v.offset)}), size={fmt_size(v.size)} - {v.note}")
        if args.partition is None and len(vols) > 1:
            if sys.stdin.isatty():
                try:
                    ans = input(f"\n여러 볼륨이 있습니다. 사용할 번호(1~{len(vols)}) 입력: ").strip()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                if not (ans.isdigit() and 1 <= int(ans) <= len(vols)):
                    print("올바른 번호가 아니어서 종료합니다.")
                    return 1
                args.partition = int(ans)
            else:
                print("\n여러 볼륨이 있습니다. --partition N 으로 선택하세요.")
                return 1
        if args.partition is not None and not (1 <= args.partition <= len(vols)):
            print(f"--partition 은 1~{len(vols)} 사이여야 합니다.")
            return 1
        vol = vols[(args.partition or 1) - 1]

    if vol.fs in ("FAT", "exFAT"):
        return main_fat(args, img, vol, pt)
    boot = load_boot(img, vol)
    scan_range = parse_range(args.scan_range) if args.scan_range else None

    # ---- $MFT 읽기 ----
    mft_entries: Optional[Dict[int, Entry]] = None
    st_mft: Optional[Stats] = None
    mft_failed = False
    try:
        mft_entries, st_mft = load_via_mft(img, vol, boot, probe=not args.no_probe)
    except Exception as ex:  # noqa: BLE001
        print(f"\n[!] MFT 정상 경로 실패: {ex}")
        if args.mode == "mft":
            return 1
        mft_failed = True

    # ---- 스캔 결과(캐시) ----
    want_scan = args.mode == "scan" or mft_failed
    meta = (img.size, vol.offset, vol.size, scan_range)
    cached = None
    if args.cache and os.path.exists(args.cache):
        try:
            with open(args.cache, "rb") as f:
                cached = pickle.load(f)
            if cached.get("meta") != meta:
                print(f"[!] 캐시 {args.cache} 는 다른 이미지/볼륨/범위의 결과라 무시합니다.")
                cached = None
        except Exception as ex:  # noqa: BLE001
            print(f"[!] 캐시를 읽지 못함({ex}). 무시합니다.")
            cached = None
    scan_entries: Dict[int, Entry] = {}
    idx: Dict[Tuple[int, int], IdxEnt] = {}
    st_scan: Optional[Stats] = None
    if cached is not None:
        ver = cached.get("version", 1)
        idx, st_scan = cached.get("idx", {}), cached["st"]
        scan_entries = cached.get("entries", {})
        for e in scan_entries.values():
            upgrade_entry(e)
        st_scan.index_added = st_scan.index_filled = 0
        for f in dataclasses.fields(Stats):   # 구버전 캐시의 Stats 에 없는 필드 채우기
            if not hasattr(st_scan, f.name):
                setattr(st_scan, f.name, f.default_factory() if f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
                        else f.default)
        print(f"캐시에서 스캔 결과를 불러옴: {args.cache}"
              + ("" if ver >= 3 else " (구버전: 레코드 위치/순번 정보 없음, 폴더 인덱스는 그대로 사용)"))
    elif want_scan:
        if not boot.ok:
            boot.record_size = 1024  # 기본값 가정
        scan_entries, idx, st_scan = load_via_scan(img, vol, boot, scan_range)
        if args.cache and not out_blocked(args, pt, args.cache, "스캔 캐시"):
            with open(args.cache, "wb") as f:
                pickle.dump({"version": CACHE_VERSION, "meta": meta, "entries": scan_entries,
                             "idx": idx, "st": st_scan}, f)
            print(f"스캔 결과 저장: {args.cache} (다음 실행부터 스캔을 건너뜁니다)")

    entries: Dict[int, Entry] = {}
    entries.update(scan_entries)
    if mft_entries is not None:
        for k, e in mft_entries.items():  # MFT 우선 (단, 옮겨진 레코드가 스캔된 레코드보다 오래되었으면 양보)
            old = entries.get(k)
            if old is None or e.pos_recno is None or e.lsn >= old.lsn:
                entries[k] = e
    if st_mft is not None:
        for k, e in st_mft.tail.items():  # 선언된 $MFT 끝 뒤에서 찾은 레코드
            old = entries.get(k)
            if old is None or (old.source.startswith("SCAN") and e.lsn >= old.lsn):
                entries[k] = e
    st = st_mft if st_mft is not None else st_scan
    assert st is not None

    # 캐시에서 온 폴더 레코드는 $INDEX_ROOT 를 아직 읽지 않았을 수 있으므로 해당 위치만 다시 읽는다
    todo = [e for e in entries.values() if e.is_dir and e.iroot is None and e.offset is not None
            and e.source.startswith(("SCAN", "TAIL"))]
    if todo:
        prog = Progress(len(todo), "폴더 인덱스(상주) 다시 읽기", as_bytes=False)
        for n_, e in enumerate(sorted(todo, key=lambda x: x.offset), 1):
            raw = img.read(vol.offset + e.offset, boot.record_size)
            r = parse_record(raw, e.recno) if len(raw) == boot.record_size else None
            e.iroot = r.iroot if (r is not None and r.recno == e.recno and r.iroot is not None) else []
            if n_ % 2000 == 0:
                prog.update(n_)
        prog.done()
    n_root = 0
    for e in list(entries.values()) + list(st_scan.extras if st_scan is not None else []):
        for ie in (getattr(e, 'iroot', None) or ()):
            n_root += 1
            key = (ie.recno, ie.seq)
            old = idx.get(key)
            if old is None or (_NS_PRIORITY.get(ie.ns, 0), ie.lsn) > (_NS_PRIORITY.get(old.ns, 0), old.lsn):
                idx[key] = ie
    if idx:
        merge_index(entries, idx, st_scan if st_scan is not None else st)

    # 현재 $MFT 에 없고 다른 방법(스캔/연속 영역 탐사)으로만 발견된 레코드
    have_mft = mft_entries is not None

    offsets_known = any(e.offset is not None for e in scan_entries.values())
    cur_ranges: List[Tuple[int, int]] = []
    if st_mft is not None and boot.ok:
        cur_ranges = sorted((l * boot.cluster, (l + ln) * boot.cluster) for l, ln in st_mft.mft_runs if l is not None)

    # ---- 선언된 $MFT 밖이지만 번호가 이어지는 '연장 구간' 찾기 (MFT 가 잘린 경우) ----
    cont_bases: set = set()
    cont_info: List[dict] = []
    gaps: List[Tuple[int, int]] = []
    if have_mft and st_mft is not None and offsets_known and cur_ranges and st_mft.mft_expected:
        declared = st_mft.mft_expected
        c_starts = [r[0] for r in cur_ranges]
        by_base: Dict[int, List[Entry]] = {}
        for x in scan_entries.values():
            if x.offset is None or in_ranges(c_starts, cur_ranges, x.offset):
                continue
            by_base.setdefault(x.offset - x.recno * boot.record_size, []).append(x)
        for base, lst in by_base.items():
            if len(lst) >= (100 if st_scan.valid > 5000 else 3) and sum(1 for x in lst if x.recno >= declared) >= 0.9 * len(lst):
                cont_bases.add(base)
                nums = sorted(x.recno for x in lst)
                cont_info.append({"base": base, "count": len(lst), "lo": nums[0], "hi": nums[-1],
                                  "first": min(x.offset for x in lst), "last": max(x.offset for x in lst) + boot.record_size})
        if cont_bases:
            for x in entries.values():
                if x.offset is not None and x.source.startswith("SCAN") and \
                        x.offset - x.recno * boot.record_size in cont_bases:
                    x.mft_cont = True
            present = set(entries) | st_mft.empty_set | st_mft.bad_set | set(range(declared))
            top = max(x["hi"] for x in cont_info)
            run_start = None
            for n in range(declared, top + 2):
                if n <= top and n not in present:
                    if run_start is None:
                        run_start = n
                elif run_start is not None:
                    gaps.append((run_start, n - 1))
                    run_start = None
            cont_info.sort(key=lambda d: d["lo"])

    def is_remnant(e: Entry) -> bool:
        return e.source.startswith(("SCAN", "TAIL")) and not e.mft_cont and e.recno >= 4   # 0~3 은 $MFTMirr 사본일 수 있음

    scan_only = [e for e in entries.values() if is_remnant(e)] if have_mft else []
    n_outside = 0
    if have_mft and st_mft is not None and boot.ok and offsets_known:
        rng = sorted((l * boot.cluster, (l + ln) * boot.cluster) for l, ln in st_mft.mft_runs if l is not None)
        starts = [r[0] for r in rng]
        for e in scan_only:
            if e.source.startswith("TAIL"):
                n_outside += 1
            elif e.offset is not None and not in_ranges(starts, rng, e.offset):
                e.outside_mft = True
                n_outside += 1

    # ---- 경로 복원 ----
    extras: List[Entry] = []
    if st_scan is not None:
        c_starts = [r[0] for r in cur_ranges]
        seen_pos = {e.offset for e in entries.values() if e.offset is not None}
        for x in st_scan.extras + [sc for k, sc in scan_entries.items()
                                    if k in entries and entries[k] is not sc and entries[k].source.startswith(("MFT", "TAIL"))]:
            upgrade_entry(x)
            if x.offset is None or x.offset in seen_pos:
                continue
            if cur_ranges and in_ranges(c_starts, cur_ranges, x.offset):
                continue   # 현재 $MFT 안의 같은 레코드(예: 미러)
            seen_pos.add(x.offset)
            x.source = "SCAN"
            x.outside_mft = True
            extras.append(x)
    if have_mft:
        scan_only = scan_only + extras
    resolver = PathResolver(entries, idx, st_mft, extras, boot.record_size, cur_ranges, cont_bases, gaps)
    rows = []
    pstatus: Dict[int, str] = {}
    status_cnt: Counter = Counter()
    lost_children: Counter = Counter()
    lost_reason: Dict[tuple, str] = {}
    lost_samples: Dict[tuple, List[str]] = {}
    lost_src: Dict[tuple, Counter] = {}

    def _src_tag(e_):
        g_ = resolver.grp(e_)
        return f"{e_.source}/{'int' if isinstance(g_, int) else g_}"
    reason_cnt: Counter = Counter()
    named = 0
    for recno, e in entries.items():
        if e.name is None or recno == ROOT_RECNO:
            continue
        named += 1
        path, status, bn, reason = resolver.path_of(e)
        pstatus[recno] = status
        status_cnt[status] += 1
        if status in ("orphan", "loop") and bn is not None:
            lost_children[bn[:2]] += 1
            lost_reason[bn[:2]] = reason
            reason_cnt[reason] += 1
            sm = lost_samples.setdefault(bn[:2], [])
            if len(sm) < 3:
                sm.append(e.name or "")
            lost_src.setdefault(bn[:2], Counter())[_src_tag(e)] += 1
        rows.append([path, e, status])
    for e in extras:
        if e.name is None or e.recno == ROOT_RECNO:
            continue
        named += 1
        path, status, bn, reason = resolver.path_of(e)
        status_cnt[status] += 1
        if status in ("orphan", "loop") and bn is not None:
            lost_children[bn[:2]] += 1
            lost_reason[bn[:2]] = reason
            reason_cnt[reason] += 1
            lost_src.setdefault(bn[:2], Counter())[_src_tag(e)] += 1
        rows.append([path, e, status])
    orphans = status_cnt["orphan"] + status_cnt["loop"]
    for n in resolver.implied:
        e = entries[n]
        e.is_dir = True
        e.issues.append("(참고) 폴더 플래그가 지워진 레코드로 보임: 같은 순번으로 이 레코드를 부모로 기록한 하위 항목이 있어 폴더로 간주")

    # ---- --root: 지정한 경로 아래만 (내용 확인/목록/복사 대상) ----
    rk = root_key(args.root)
    root_ids: Optional[Set[int]] = None
    if rk:
        root_ids = {id(e_) for p_, e_, _s in rows if under_root(p_, rk)}

    # ---- 건강도 ----
    bitmap, bitmap_why = load_bitmap(img, vol, boot, entries) if boot.ok else (None, "부트섹터 손상")
    health_notes = analyze_health(entries, vol, boot, img, bitmap, bitmap_why, args.check_data, pstatus, have_mft,
                                  (lambda e_: id(e_) in root_ids) if root_ids is not None else None)
    for path, e, status in rows:
        if status not in ("ok", "fixdir", "prev", "idx") and e.source != "INDEX":
            if status in ("orphan", "loop"):
                e.issues.append("(참고) 상위 폴더 경로를 복원하지 못함")
            elif status == "idxl":
                e.issues.append("(참고) 상위 폴더 이름을 순번 확인 없이 폴더 인덱스에서 추정함")
            else:
                e.issues.append("(참고) 상위 폴더가 순번 불일치(다른 세대의 폴더일 수 있음)")

    def is_system(e: Entry) -> bool:
        return e.recno < 24 and (e.recno < 16 or (e.name or "").startswith("$"))

    def visible(e: Entry) -> bool:
        if root_ids is not None and id(e) not in root_ids:
            return False
        if not args.all and is_system(e):
            return False
        if args.deleted_only and e.in_use:
            return False
        if args.only_issues and e.risk == 0:
            return False
        if args.gen == "current" and e.mft_cont:
            return False
        if args.gen == "old" and not e.mft_cont:
            return False
        return True

    shown = sorted((r for r in rows if visible(r[1])), key=lambda r: r[0].lower())

    # ---- 시스템 메타파일 점검 ----
    sys_lines, sys_bad = [], []
    by_name: Dict[str, List[Entry]] = {}
    wanted = set(SYSTEM_NAMES) - {"."}
    for e in entries.values():
        if e.name in wanted:
            by_name.setdefault(e.name, []).append(e)
    roots = [e for e in entries.values() if e.name == "." and e.is_dir and e.recno != ROOT_RECNO]
    if roots:
        by_name["."] = roots
    for n, expect in enumerate(SYSTEM_NAMES):
        e = entries.get(n)
        if e is None or e.name != expect:
            actual = "레코드 없음" if e is None else f"'{e.name}' (순번 {e.seq}, {'사용중' if e.in_use else '삭제됨'})"
            others = [x for x in by_name.get(expect, []) if x.recno != n][:3]
            where = ""
            if others:
                where = "  | 같은 이름의 레코드: " + ", ".join(
                    f"#{x.recno}(순번 {x.seq}, {'사용중' if x.in_use else '삭제됨'})" for x in others)
            sys_lines.append(f"  #{n:<2} {expect:<9} !! 예상과 다름: {actual}{where}")
            sys_bad.append(n)
        else:
            nrun = len(e.runs)
            sys_lines.append(f"  #{n:<2} {expect:<9} OK  {'사용중' if e.in_use else '미사용'}"
                             f"  {fmt_size(e.size):>10}  run {nrun}개" + ("  (확장 레코드 병합)" if e.merged_ext else ""))

    # ---- 상태 판정 ----
    extra: List[Tuple[int, str]] = []
    if st_mft is not None and st_mft.mft_expected and st_mft.mft_total < st_mft.mft_expected:
        extra.append((2, f"$MFT 선언 크기({st_mft.mft_expected}개)보다 적게 읽음({st_mft.mft_total}개)"))
    orphans_j, named_j = orphans, named
    if cont_info:
        n_cont = sum(d["count"] for d in cont_info)
        extra.append((1, f"$MFT 가 선언한 {st_mft.mft_expected}개보다 뒤 번호의 레코드 {n_cont}개가 이전 세대 MFT 의 남은 부분으로 발견됨"
                         "(데이터가 덮어쓰였는지는 [세대별 요약] 참고, 아래 경로/건강도 판정은 현재 세대 기준)"))
        cur_rows = [r for r in rows if not r[1].mft_cont]
        named_j = len(cur_rows)
        orphans_j = sum(1 for r in cur_rows if r[2] in ("orphan", "loop"))
    if boot.ok:
        ncl_ = vol.size // boot.cluster
        if bitmap is None:
            extra.append((2, f"$Bitmap 을 읽지 못함: {bitmap_why}"))
        else:
            cov_ = len(bitmap) * 8
            set_ = count_set_bits(bitmap, 0, cov_)
            if cov_ < 0.9 * ncl_:
                extra.append((2, f"$Bitmap 이 볼륨 크기에 비해 너무 작음(볼륨의 {cov_ * 100 / max(ncl_, 1):.0f}% 만 다룸)"))
            if set_ < 0.001 * cov_:
                extra.append((2, "$Bitmap 이 사실상 비어 있음(할당 비트 거의 없음) -> 그대로 마운트하면 기존 데이터를 덮어쓸 위험"))
    if have_mft and scan_only and named:
        extra.append((2 if len(scan_only) / named > 0.01 else 1,
                      f"현재 $MFT 에 없는 레코드 {len(scan_only)}개가 스캔/연속 영역 탐사에서만 발견됨"
                      "(이전 파일시스템의 잔재이거나 레코드 0 의 크기가 오래된 값일 가능성)"))
    if st_mft is not None and st_mft.pos_mismatch:
        ratio = st_mft.pos_mismatch / max(st_mft.valid, 1)
        extra.append((2 if ratio > 0.001 else 1,
                      f"레코드 {st_mft.pos_mismatch}개({ratio:.3%})가 자기 번호와 다른 위치에 놓여 있음"))
    if sys_bad:
        extra.append((2, "시스템 메타파일 레코드 " + ", ".join(f"#{n}" for n in sys_bad) + " 가 예상과 다름"))
    recovered = sum(1 for e in entries.values() if e.source == "INDEX")
    grade, reasons = judge(boot, st, mft_failed, orphans_j, named_j, recovered, extra)

    # ---- 리포트 ----
    print(f"\n[디스크 상태] {grade}")
    for r in reasons:
        print(f"  - {r}")

    if st_mft is not None:
        rs = boot.record_size
        print("\n[$MFT 점검]")
        print(f"  선언된 크기 {st_mft.mft_expected}개 레코드 / 확인된 data run {st_mft.mft_extents}개 "
              f"(확장 레코드로 이어진 조각 {st_mft.mft_ext_used}개 해석) / 실제로 읽은 레코드 {st_mft.mft_total}개")
        if st_mft.pos_mismatch:
            top = sorted(st_mft.mismatch_deltas.items(), key=lambda kv: -kv[1])[:5]
            print(f"  레코드 내장 번호 != 위치: {st_mft.pos_mismatch}개 ({st_mft.pos_mismatch / max(st_mft.valid, 1):.3%})"
                  f" / (내장-위치) 차이 상위: " + ", ".join(f"{d:+d} x{c}" for d, c in top))
            samples = sorted(st_mft.mismatch_samples, key=lambda s: (s[0] >= 24, s[0]))[:8]
            for pos, emb, nm in samples:
                print(f"     위치 #{pos} -> 레코드가 말하는 번호 #{emb}  '{nm}'")
        else:
            print("  레코드 내장 번호와 위치가 모두 일치")
        pr = st_mft.probe
        if pr is not None:
            if pr["valid"]:
                print(f"  선언된 끝 뒤 탐사: 물리적으로 이어지는 영역에 FILE 레코드 {pr['valid']}개 "
                      f"(번호 {pr['min']}~{pr['max']}, 위치와 번호 일치 {pr['contig']}개)")
                if pr["valid"] > 0.5 * max(st_mft.mft_expected, 1):
                    print("    -> $MFT 가 선언된 크기보다 훨씬 길게 이어집니다: 레코드 0 이 오래된 값이거나, 이전 MFT 의 꼬리가 남은 것")
            else:
                print("  선언된 끝 뒤 탐사: 이어지는 FILE 레코드 없음")
        if st_scan is not None:
            line = f"  스캔 결과: 발견한 레코드 {st_scan.valid}개 (같은 번호 중복 {st_scan.dup_records}개)"
            line += f", 현재 $MFT 에 없고 스캔/탐사로만 발견 {len(scan_only)}개"
            line += f" (그중 $MFT 영역 밖 {n_outside}개)" if offsets_known or n_outside else " (스캔 레코드 위치 정보 없음: 구버전 캐시)"
            print(line)
        elif scan_only:
            print(f"  현재 $MFT 에 없고 탐사로만 발견된 레코드 {len(scan_only)}개")
        for n in st_mft.notes:
            print(f"  * {n}")
        if scan_only:
            def _gs(label, grp):
                if not grp:
                    print(f"  {label}: 0개")
                    return
                lsns = sorted(x.lsn for x in grp)
                mts = sorted(x.mtime for x in grp if x.mtime)
                dirs = sum(1 for x in grp if x.is_dir)
                dt = (lambda d: d.strftime("%Y-%m-%d")) if mts else None
                tline = f" / 수정시간 {dt(mts[0])} ~ 중앙 {dt(mts[len(mts) // 2])} ~ {dt(mts[-1])}" if mts else ""
                print(f"  {label}: {len(grp)}개(폴더 {dirs}) / LSN {lsns[0]} ~ 중앙 {lsns[len(lsns) // 2]} ~ {lsns[-1]}{tline}")
            print("\n[세대 비교] 현재 $MFT 의 레코드 vs 그 밖에서만 발견된 레코드 (두 집단의 LSN/수정시간 범위가 다르면 서로 다른 시기의 파일시스템)")
            _gs("현재 $MFT ", [x for x in entries.values() if x.source.startswith("MFT") and x.name])
            _gs("그 밖(발견)", scan_only)
    if cont_info:
        print(f"\n[MFT 연장 구간] 선언된 $MFT({st_mft.mft_expected}개) 뒤 번호의 레코드가 디스크의 다른 위치에서 이어서 발견됨"
              " -> 이전 세대 MFT 의 남은 부분으로 보임(현재 세대와 데이터 위치가 겹치면 이 구간 파일은 덮어쓰였을 가능성이 큼)")
        for d in cont_info:
            span = d["hi"] - d["lo"] + 1
            line = (f"  볼륨 {fmt_size(d['first'])} 부근: 번호 {d['lo']}~{d['hi']} 중 {d['count']}개 발견 "
                    f"({d['count'] * 100 / max(span, 1):.1f}%)")
            if bitmap is not None and boot.ok:
                c0, c1 = d["first"] // boot.cluster, -(-d["last"] // boot.cluster)
                if c0 >= len(bitmap) * 8:
                    line += " / 이 영역은 $Bitmap 이 다루는 범위 밖"
                else:
                    al = count_set_bits(bitmap, c0, min(c1, len(bitmap) * 8))
                    line += f" / 이 영역의 비트맵 사용률 {al * 100 / max(min(c1, len(bitmap) * 8) - c0, 1):.0f}%"
            print(line)
        if gaps:
            tot = sum(b - a + 1 for a, b in gaps)
            print(f"  번호 공백(어디에도 레코드가 없음): {tot}개 - " + ", ".join(f"#{a}~#{b}" for a, b in gaps[:6])
                  + (" ..." if len(gaps) > 6 else ""))
            print("    이 번호의 폴더를 부모로 가진 항목은 폴더 이름을 알 수 없어 [유실된폴더_번호] 로 묶입니다.")
        print("  * 이 구간이 빈 공간으로 표시돼 있는지는 [비트맵 점검]을 보세요. 원본 디스크에는 쓰기를 하지 말고 이미지로만 작업하세요.")
    if st_scan is not None and offsets_known:
        groups: Dict[object, List[Entry]] = {}
        for x in list(scan_entries.values()) + extras:
            if x.offset is None:
                continue
            gk = x.offset - x.recno * boot.record_size
            if cur_ranges and in_ranges([r[0] for r in cur_ranges], cur_ranges, x.offset):
                gk = "CUR"
            groups.setdefault(gk, []).append(x)
        thr = 50 if len(scan_entries) + len(extras) > 5000 else 2
        big = sorted(((k, v) for k, v in groups.items() if len(v) >= thr), key=lambda kv: -len(kv[1]))
        if big:
            print("\n[세대 분석] 스캔에서 찾은 FILE 레코드를 'MFT 가 시작했을 위치'(위치 - 번호 x 레코드크기)별로 묶음."
                  " 같은 묶음 = 하나의 연속된 MFT 구간")
            for k, v in big[:10]:
                nums = sorted(x.recno for x in v)
                lsns = sorted(x.lsn for x in v)
                mts = sorted(x.mtime for x in v if x.mtime)
                mt = f" / 수정시간 {mts[0]:%Y-%m-%d}~{mts[len(mts) // 2]:%Y-%m-%d}~{mts[-1]:%Y-%m-%d}" if mts else ""
                where = "현재 $MFT" if k == "CUR" else f"MFT 시작 추정 위치 {fmt_size(k)}"
                if k in cont_bases:
                    where += " (연장 구간)"
                print(f"  {where}: 레코드 {len(v)}개 / 번호 {nums[0]}~{nums[-1]} / LSN 중앙 {lsns[len(lsns) // 2]}{mt}"
                      f" / 폴더 {sum(1 for x in v if x.is_dir)}")
            small = sum(len(v) for k, v in groups.items() if len(v) < thr)
            if small:
                print(f"  (그 외 흩어진 레코드 {small}개)")
    print("\n[시스템 메타파일]")
    for line in sys_lines:
        print(line)

    print(f"\n[검사 요약] 방식={st.mode}  읽은 레코드={st.mft_total}  유효={st.valid}  빈 레코드={st.empty}  "
          f"무효={st.bad}  확장레코드={st.ext_records}  이미지 읽기 오류={img.read_errors}")
    deleted_cnt = sum(1 for e in entries.values() if e.name and not e.in_use)
    print(f"           이름 있는 항목={named}  삭제됨={deleted_cnt}  경로 복원 실패={orphans}")
    if st_scan is not None:
        print(f"[폴더 인덱스] INDX 블록={st_scan.index_blocks}  인덱스 항목={st_scan.index_found}  "
              f"-> MFT 에 없어 추가={st_scan.index_added}  이름 보충={st_scan.index_filled}")
    for n in health_notes:
        print(f"  * {n}")
    print(f"  * 볼륨: 섹터={boot.bps}B 클러스터={fmt_size(boot.cluster)} 레코드={boot.record_size}B")

    if boot.ok:
        print("\n[비트맵 점검]")
        be = entries.get(BITMAP_RECNO)
        ncl = vol.size // boot.cluster
        need = (ncl + 7) // 8
        print(f"  볼륨 {fmt_size(vol.size)} = 클러스터 {ncl}개 -> $Bitmap 은 약 {fmt_size(need)} 필요")
        if bitmap is None:
            print(f"  $Bitmap 을 읽지 못함: {bitmap_why}")
        else:
            cov = len(bitmap) * 8
            setb = count_set_bits(bitmap, 0, cov)
            nz = sum(1 for b in bitmap[:1 << 20] if b)
            print(f"  $Bitmap 선언 크기 {fmt_size(be.size) if be else '?'} / 읽은 크기 {fmt_size(len(bitmap))}"
                  f" -> 볼륨의 앞 {min(cov, ncl) * boot.cluster / GIB:.0f} GiB ({min(cov, ncl) * 100 / max(ncl, 1):.0f}%) 만 다룸")
            print(f"  다루는 범위의 할당(1) 비트 {setb}개 ({setb * 100 / max(cov, 1):.1f}%) / 앞 1 MiB 중 0이 아닌 바이트 {nz}개")
            if cov < ncl:
                print("  * 비트맵이 볼륨보다 작음: 뒤쪽 영역의 사용 여부는 알 수 없음(뒤쪽 MFT 연장 구간 포함)")
            if setb == 0 or nz == 0:
                print("  * 비트맵이 전부 0: 초기화됐거나 다른 곳을 읽고 있을 가능성")
    print(f"\n[부모 참조 진단] 이름 있는 항목 {named}개")
    print(f"  경로 복원: 현재 MFT 와 일치 {status_cnt['ok']} / 폴더 플래그만 지워진 레코드를 폴더로 간주 {status_cnt['fixdir']} / 이전 세대 MFT 폴더로 복원 {status_cnt['prev']} / 폴더 인덱스(INDX)로 복원 {status_cnt['idx']} / "
          f"순번 없이 인덱스로 추정 {status_cnt['idxl']} / 순번 불일치(약한 연결) {status_cnt['weak']} / 실패 {orphans}")
    if reason_cnt:
        print("  실패 원인(항목 수): " + ", ".join(f"{LOST_REASON_TEXT.get(k, k).split(' - ')[0].split('(')[0].strip()} {v}"
                                                for k, v in reason_cnt.most_common()))
        by_rec: Dict[int, Dict[Optional[int], int]] = {}
        for (n, ps), c in lost_children.items():
            by_rec.setdefault(n, {})[ps] = c
        print("  자식이 가장 많은 유실 폴더 10개 (같은 번호는 합산, '순번 없음' = 순번 정보가 없는 구버전 캐시 항목):")
        for n, d in sorted(by_rec.items(), key=lambda kv: -sum(kv[1].values()))[:10]:
            main_ps = max(d, key=lambda k: d[k])
            parts = ", ".join(f"순번 {'없음' if s is None else s}: {c}" for s, c in sorted(
                d.items(), key=lambda kv: -kv[1])[:3])
            print(f"    #{n}: 하위 {sum(d.values())}개 ({parts}) - "
                  f"{resolver.describe_lost((n, main_ps), lost_reason[(n, main_ps)])}")
            if lost_samples.get((n, main_ps)):
                print("        하위 항목 예: " + ", ".join(lost_samples[(n, main_ps)]))
            if lost_src.get((n, main_ps)):
                print("        자식 구성(출처/세대): " + ", ".join(f"{k} {v}" for k, v in lost_src[(n, main_ps)].most_common(4)))
        if not idx:
            print("  -> 폴더 인덱스(INDX) 정보가 없습니다. --mode scan --cache scan.pkl 로 한 번 스캔하면 "
                  "유실된 폴더 이름을 복원해 볼 수 있습니다.")
        else:
            print("  -> 위 폴더들은 폴더 인덱스(INDX)에서도 찾지 못했습니다. 해당 파일들은 [유실된폴더_번호] 아래에 묶어 표시합니다.")

    files = [r[1] for r in rows if not r[1].is_dir and (args.all or not is_system(r[1]))]
    cnt = Counter(e.risk for e in files)
    print(f"\n[파일 건강도] 전체 파일 {len(files)}개: 정상 {cnt[0]} / 의심 {cnt[1]} / 손상추정 {cnt[2]}"
          + ("" if args.check_data else "   (--check-data 를 주면 파일 내용 시작 부분도 확인)"))
    issue_counter: Counter = Counter()
    for e in files:
        for m in e.issues:
            if not m.startswith("(참고)"):
                issue_counter[re.sub(r"\d+%", "N%", m)] += 1
    for m, c in issue_counter.most_common(8):
        print(f"  - {m}: {c}개")
    if cont_info:
        print("  [세대별 요약]")
        for nm, flag in (("현재 세대(선언된 $MFT)", False), ("이전 세대(연장 구간)", True)):
            g = [e for e in files if bool(e.mft_cont) == flag]
            c = Counter(e.risk for e in g)
            line = f"    {nm}: 파일 {len(g)}개 / 정상 {c[0]} 의심 {c[1]} 손상추정 {c[2]}"
            if flag:
                xo = [e for e in g if e.xovl is not None]
                line += (f" / 현재 파일과 겹침 없음 {sum(1 for e in xo if e.xovl == 0)}"
                         f", 일부 겹침 {sum(1 for e in xo if 0 < e.xovl < 0.99)}, 거의 전부 겹침 {sum(1 for e in xo if e.xovl >= 0.99)}")
            print(line)
        crow = [r for r in rows if not r[1].mft_cont and not r[1].is_dir and (args.all or not is_system(r[1]))]
        st_c = Counter(r[2] for r in crow)
        print("    현재 세대 파일 경로 복원: " + ", ".join(f"{k} {v}" for k, v in st_c.most_common())
              + "  (ok=정상 경로, fixdir=폴더 플래그가 지워진 폴더 경유, idx=폴더 인덱스로 복원, orphan=유실된폴더)")
        top: Counter = Counter()
        for path_, e_, _st in crow:
            top[path_.split("/", 1)[0] if "/" in path_ else "(최상위 파일)"] += 1
        print("    현재 세대 최상위 폴더별 파일 수: " + ", ".join(f"{k} {v}" for k, v in top.most_common(12)))
        oldn = [r[1] for r in rows if r[1].mft_cont and not r[1].is_dir]
        res_n = sum(1 for e_ in oldn if e_.xovl is None)
        print(f"    이전 세대 중 데이터 위치(런)가 없는 파일 {res_n}개(상주 데이터/빈 파일: 내용이 MFT 레코드 안에 있어 덮어쓰임과 무관) "
              "- 목록은 --gen old")
    if args.check_data:
        tab: Dict[str, Counter] = {"현재": Counter(), "연장": Counter()}
        for e in files:
            if e.sig:
                tab["연장" if e.mft_cont else "현재"][e.sig + ("+겹침" if e.ov_risk else "")] += 1
        print("  [내용 확인 결과] 파일 시작 부분을 확장자 시그니처와 대조 (ok=일치, bad=불일치/0채움, unk=확인 불가 확장자)")
        for k, c in tab.items():
            if c:
                tot = sum(c.values())
                chk = c["ok"] + c["bad"] + c["ok+겹침"] + c["bad+겹침"]
                good = c["ok"] + c["ok+겹침"]
                print(f"    {k} 구간: {tot}개 확인 / 시그니처 판별 가능 {chk}개 중 일치 {good}개 "
                      f"({good * 100 / max(chk, 1):.0f}%) / " + ", ".join(f"{a} {b}" for a, b in sorted(c.items())))
    if (status_cnt["ok"] + status_cnt["idx"] + status_cnt["prev"] + status_cnt["fixdir"] < 0.5 * max(named, 1) or sys_bad
            or (have_mft and len(scan_only) > 0.01 * max(named, 1))
            or (st_mft is not None and st_mft.pos_mismatch)):
        print("  ※ 폴더 구조/시스템 파일에 이상이 있어 위 건강도는 참고용입니다(이전 세대 레코드가 섞여 있으면 과대 평가될 수 있음).")

    if not args.no_csv and not out_blocked(args, pt, args.csv, "결과 CSV"):
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["경로", "종류", "상태", "건강도", "문제점", "크기(바이트)", "수정시간(UTC)",
                        "MFT번호", "출처", "경로복원", "데이터시작LCN", "세대", "현재파일과겹침비율"])
            for path, e, status in shown:
                first = next((l for l, _ in e.runs if l is not None), "") if e.runs else ""
                w.writerow(["/" + path, "폴더" if e.is_dir else "파일", "정상" if e.in_use else "삭제",
                            RISK_LABEL[e.risk], " | ".join(e.issues), "" if e.is_dir else e.size,
                            e.mtime or "", e.recno, e.source, status, first,
                            "이전" if e.mft_cont else "현재", "" if e.xovl is None else f"{e.xovl:.2f}"])
        print(f"\nCSV 저장: {os.path.abspath(args.csv)} ({len(shown)}개 항목) - 전체 목록은 이 파일(엑셀)에서 확인")

    if cont_info and args.gen != "all":
        hid = sum(1 for r in rows if (r[1].mft_cont if args.gen == "current" else not r[1].mft_cont))
        print(f"\n[목록 필터] --gen {args.gen}: {'이전' if args.gen == 'current' else '현재'} 세대 항목 {hid}개는 숨김 "
              "(--gen all 로 전부, --gen old 로 이전 세대만)")
    print_root_scope(args, rows, pt)
    _lines = []
    for path, e, status in shown:
        flag = "x" if not e.in_use else ("i" if e.source == "INDEX" else ("o" if e.mft_cont else ("s" if have_mft and is_remnant(e) else " ")))
        mark = ("D" if e.is_dir else "-") + flag
        size = "" if e.is_dir else fmt_size(e.size)
        mt = e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else "-"
        _lines.append(color_row(f"{mark} {padw(RISK_LABEL[e.risk], 8)} {size:>10}  {mt:16}  /{path}", e, pt))
    print_listing(_lines, args.head, args.page, "[파일 목록]")
    r_ = tree_and_copy(args, img, vol, boot, st_mft, rows, visible, pt)
    if r_ is not None:
        return r_

    print("\n범례: D=폴더, x=삭제된 항목, o=이전 세대(연장 구간) 레코드, i=MFT 레코드 없이 인덱스에서만 복원, s=현재 MFT 에 없고 스캔/탐사에서만 발견(이전 세대 잔재 가능),"
          " [유실된폴더_N]=부모 폴더 정보 없음")
    return 0


# --------------------------------------------------------------------------
# 시작 메뉴(인자 없이 실행/더블클릭) + 창이 바로 닫히지 않게 하는 처리
# --------------------------------------------------------------------------
def own_console() -> bool:
    """Windows: 이 프로그램이 자기 전용 콘솔 창에서 실행 중인가(더블클릭/관리자 재실행). 끝나면 창이 사라지는 경우."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        arr = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(arr, 4) <= 1  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def is_admin() -> bool:
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def pause_exit(force: bool = False):
    if force or own_console():
        try:
            input("\nEnter 키를 누르면 창이 닫힙니다...")
        except (EOFError, KeyboardInterrupt):
            pass


def relaunch_as_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe, params = sys.executable, f'"{os.path.abspath(sys.argv[0])}"'
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def launcher_loop() -> int:
    import shlex
    first = True
    while True:
        print("\n==================================================")
        print("  디스크 복구 도구  (읽기 전용: 원본에는 쓰지 않습니다)")
        print("==================================================")
        if not is_admin():
            print("  * 관리자 권한이 아닙니다. 이미지 파일은 열 수 있지만 드라이브/물리 디스크는 관리자 권한이 필요합니다.")
        print("\n참조할 대상을 입력하세요.")
        print("  - 드라이브 문자       예) E:")
        print("  - 물리 디스크 번호    예) 1          (= \\\\.\\PhysicalDrive1)")
        print("  - 이미지 파일 경로    예) D:\\backup\\disk.001   (파일을 이 창에 끌어다 놓아도 됩니다)")
        print("  - L = 연결된 디스크/볼륨 목록 보기      Q 또는 exit = 종료\n")
        try:
            src = input("대상: ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            return 0
        if not src:
            print("  (종료하려면 q 또는 exit 를 입력하세요)")
            continue
        if src.lower() in ("q", "quit", "exit", "종료"):
            return 0
        if src.lower() == "l":
            list_drives()
            continue
        if src.isdigit():
            src = f"\\\\.\\PhysicalDrive{src}"
        elif re.fullmatch(r"[A-Za-z]:\\?", src):
            src = src[:2]
        is_dev = is_device_path(normalize_source(src)) or bool(re.fullmatch(r"[A-Za-z]:", src) and os.name == "nt")
        if is_dev and os.name == "nt" and not is_admin():
            print("\n[!] 드라이브/물리 디스크를 읽으려면 관리자 권한이 필요합니다.")
            try:
                ans = input("관리자 권한으로 다시 실행할까요? (Y/N): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 0
            if ans == "y" and relaunch_as_admin():
                return 0
            continue
        if not is_dev and not os.path.exists(src):
            print("\n[!] 파일을 찾을 수 없습니다. 경로를 다시 확인하세요.")
            continue
        print("\n추가 옵션이 있으면 입력하세요 (없으면 그냥 Enter).")
        print("  예) --partition 1     --check-data     --root /Users/나/Documents     --mode scan --cache D:\\scan.pkl")
        print("  * 읽는 드라이브 안에는 파일을 만들지 마세요(캐시/CSV 경로도 다른 디스크로).")
        try:
            opt = input("옵션: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        extra = [x.strip('"') for x in shlex.split(opt, posix=False)] if opt else []
        print(f"\n대상: {src}\n복구한 파일은 반드시 '다른 디스크'에 저장하세요.\n")
        try:
            main([src, "--browse"] + extra)
        except SystemExit:
            pass
        except KeyboardInterrupt:
            print("\n중단되었습니다.")
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("\n[!] 오류가 발생했습니다. 위 내용을 이슈로 알려 주세요(파일 경로 등 개인정보는 지우고).")
        try:
            if input("\n다른 대상을 다시 지정할까요? (Y/N): ").strip().lower() != "y":
                return 0
        except (EOFError, KeyboardInterrupt):
            return 0
        first = False


def run() -> int:
    """진입점. 인자 없이 실행하면 시작 메뉴, 오류/종료 시 전용 콘솔 창이면 Enter 를 기다려 창이 바로 닫히지 않게 한다."""
    argv = sys.argv[1:]
    if "--ctrlc" not in argv:        # Ctrl+C 로 실수로 종료되지 않게 무시한다(--ctrlc 를 주면 예전처럼 Ctrl+C 로 중단 가능)
        try:
            import signal
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:  # noqa: BLE001
            pass
    try:
        if "--menu" in argv or (not argv and sys.stdin is not None and sys.stdin.isatty()):
            return launcher_loop() or 0      # 메뉴에서 이미 종료를 선택했으므로 추가로 Enter 를 기다리지 않는다
        else:
            rc = main(argv)
    except SystemExit as e:
        pause_exit()
        raise e
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        rc = 130
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\n[!] 오류가 발생했습니다. 위 내용을 이슈로 알려 주세요(파일 경로 등 개인정보는 지우고).")
        pause_exit(True)
        return 1
    pause_exit()
    return rc or 0


def _fast_exit(rc: int):
    """큰 목록(수십만 개 객체)을 가진 채 정상 종료하면 파이썬이 메모리를 정리하느라 수 초 멈춘다. 출력을 비우고 바로 종료한다."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(rc & 0xFF if isinstance(rc, int) else 1)


if __name__ == "__main__":
    try:
        _rc = run()
    except SystemExit as _e:
        _rc = _e.code if isinstance(_e.code, int) else (0 if _e.code is None else 1)
        if not isinstance(_e.code, (int, type(None))):
            print(_e.code, file=sys.stderr)
    print("\n프로그램을 종료합니다.")
    _fast_exit(_rc or 0)
