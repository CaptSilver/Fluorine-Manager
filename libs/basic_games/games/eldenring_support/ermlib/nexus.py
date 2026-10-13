"""Choosing a file of a Nexus mod: which are MAIN, which variant a file is, and
whether a pin should move to a newer file. Fetching the file list and the
download is not done here."""
import re
from typing import NamedTuple

from .errors import ErmError


def main_files(files):
    # category_name == "MAIN" already excludes OLD_VERSION/ARCHIVED by
    # construction — Nexus only ever tags one category per file. Some mods
    # (e.g. Minimal HUD #148) ship several MAIN files at once — numbered
    # variants, not versions — so this can legitimately return more than one.
    return [f for f in files if f.get("category_name") == "MAIN"]


def find_file_by_version(files, version):
    for f in files:
        if f.get("category_name") == "MAIN" and f.get("version") == version:
            return f
    raise ErmError(f"version {version} not found on Nexus (removed?) — try --update")


def find_file_by_id(files, file_id):
    # Compared as text: the id arrives as an int from a profile and as a string
    # from the lockfile, and the two have to select the same file.
    for f in files:
        if str(f.get("file_id")) == str(file_id):
            return f
    raise ErmError(f"file id {file_id} not found on Nexus for this mod")


# Nexus filenames come in two shapes:
#   legacy   Name-<modid>-<dashed version>-<epoch>.ext
#   current  Name <modid> <version> <ISO stamp> <token>.ext
# Both bury the variant name in front of machine noise.
_ARCHIVE_EXT = re.compile(r"\.(zip|7z|rar)$", re.I)
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z")
_EPOCH = re.compile(r"\b\d{9,}\b")
_TRAILING_TOKEN = re.compile(r"\s+[A-Za-z0-9]{9}\s*$")
_SEPARATORS = re.compile(r"[\s\-]+")
_VERSION_PARTS = re.compile(r"[.\-_]")


def variant_key(file_name, mod_id, version):
    """Which *variant* of a mod a file is, independent of its version.

    Normalise, THEN tokenise. Order is load-bearing: splitting first shatters
    an ISO stamp (2026-07-14T10-47Z) into 2026/07/14t10/47z, and those
    fragments then read as ordinary name words -- which is how an earlier cut
    of this matched none of map-for-goblins' nine variants instead of one.

    Version components are dropped as whole tokens, never as substrings: a
    mod whose version is "1" would otherwise lose every "1" in its name.
    """
    stem = _ARCHIVE_EXT.sub("", file_name)
    stem = _ISO_STAMP.sub("", stem)
    stem = _EPOCH.sub("", stem)
    stem = _TRAILING_TOKEN.sub("", stem)
    drop = {str(mod_id)}
    if version:
        v = str(version).lower()
        drop.update({v, v.lstrip("v")})
        drop.update(p for p in _VERSION_PARTS.split(v.lstrip("v")) if p)
    words = [t.lower() for t in _SEPARATORS.split(stem) if t]
    return " ".join(w for w in words
                    if w not in drop and w.lstrip("v") not in drop).strip()


class PinChoice(NamedTuple):
    """What to do with one mod's pin.

    `action` is "unchanged", "repin" or "ambiguous". `file` is the file to use
    and is None only when ambiguous; `candidates` is populated only then.
    """
    action: str
    file: dict
    candidates: tuple
    reason: str


def resolve_pin(current_file, files, mod_id, frozen=False):
    """Which file this mod should use now, never guessing between variants.

    The safety property: anything ambiguous keeps the current pin. The worst
    outcome is a missed update the report names, never a substituted mod --
    which is what makes moving pins automatically acceptable at all.
    """
    if frozen:
        if current_file is None:
            raise ErmError(
                f"mod {mod_id}: `freeze = true` needs a `file_id` — "
                "there is nothing to freeze")
        return PinChoice("unchanged", current_file, (), "frozen")

    mains = main_files(files)
    if not mains:
        return PinChoice("ambiguous", None, (), "no MAIN file on Nexus")

    if current_file is None:
        if len(mains) == 1:
            return PinChoice("unchanged", mains[0], (), "sole MAIN")
        return PinChoice("ambiguous", None, tuple(mains),
                         f"{len(mains)} MAIN files — set `file_id`")

    if any(m["file_id"] == current_file["file_id"] for m in mains):
        return PinChoice("unchanged", current_file, (), "pin is still a MAIN")

    if len(mains) == 1:
        return PinChoice("repin", mains[0], (), "sole MAIN supersedes the pin")

    key = variant_key(current_file["file_name"], mod_id, current_file.get("version"))
    matches = [m for m in mains
               if variant_key(m["file_name"], mod_id, m.get("version")) == key]
    if len(matches) == 1:
        return PinChoice("repin", matches[0], (), f"variant {key!r}")
    return PinChoice("ambiguous", None, tuple(mains),
                     f"variant {key!r} matched {len(matches)} of {len(mains)} MAIN files")
