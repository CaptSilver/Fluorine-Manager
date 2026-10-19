import shutil
import subprocess
from pathlib import Path

from .errors import ErmError, PathError


def harden_swap(game):
    """FS-only (no privilege): back up the real EAC launcher (ONCE) and swap in
    a copy of eldenring.exe. Returns the spg path. Idempotent-safe."""
    game = Path(game)
    spg = game / "start_protected_game.exe"
    eldenring = game / "eldenring.exe"
    backup = game / "start_protected_game.exe.erm-backup"
    if not eldenring.exists():
        raise PathError(f"eldenring.exe not found in {game} — can't harden")
    # CRITICAL: only back up if no backup exists. If a backup already exists,
    # spg is ALREADY the eldenring copy — backing it up again would overwrite
    # the real EAC-launcher backup with the eldenring copy, destroying the
    # only copy of the real launcher. This is the exact bug that made
    # community exe-swap scripts unsafe.
    try:
        if not backup.exists():
            if not spg.exists():
                raise PathError(f"start_protected_game.exe not found in {game} — nothing to swap")
            shutil.copy2(spg, backup)
        # (re)create the swapped exe from eldenring.exe
        if spg.exists():
            spg.unlink()
        shutil.copy2(eldenring, spg)
    except OSError as exc:
        raise PathError(f"failed to swap start_protected_game.exe: {exc}") from exc
    return spg


def unharden_restore(game):
    """FS-only: restore the real EAC launcher from backup, remove the swap+backup."""
    game = Path(game)
    spg = game / "start_protected_game.exe"
    backup = game / "start_protected_game.exe.erm-backup"
    if not backup.exists():
        raise PathError(f"not hardened (no backup at {backup}) — nothing to restore")
    try:
        if spg.exists():
            spg.unlink()
        shutil.move(str(backup), str(spg))
    except OSError as exc:
        raise PathError(f"failed to restore start_protected_game.exe: {exc}") from exc


def set_immutable(path, on):
    """Privileged: pkexec chattr +i/-i. The password prompt comes from the
    polkit agent, so nothing is piped to pkexec. Raises ErmError on failure.
    Monkeypatched in tests; never calls real pkexec there.

    chattr is resolved to an absolute path first: pkexec exits 127 both when
    it cannot find the program and when authorization fails, so a missing
    chattr would otherwise be reported as an authorization problem."""
    flag = "+i" if on else "-i"
    chattr = shutil.which("chattr")
    if chattr is None:
        raise ErmError(
            "chattr not found — it is the filesystem attribute tool, "
            "packaged as e2fsprogs"
        )
    try:
        r = subprocess.run(["pkexec", chattr, flag, str(path)])
    except FileNotFoundError as exc:
        raise ErmError(f"pkexec not available (is polkit installed?): {exc}") from exc
    if r.returncode == 126:
        raise ErmError(
            "pkexec authentication dialog was dismissed (rc 126) — "
            "approve the prompt to change the immutable attribute"
        )
    if r.returncode == 127:
        raise ErmError(
            "pkexec not authorized (rc 127) — no polkit agent reachable, "
            "authorization was refused, or the program could not be run"
        )
    if r.returncode != 0:
        raise ErmError(
            f"chattr {flag} failed (rc {r.returncode}) — the filesystem may "
            "not support the immutable attribute"
        )
