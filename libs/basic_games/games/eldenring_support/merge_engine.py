"""The merge, as a function from mod folders to bytes.

erm's conflicts module merged by moving files between packages and deleting
the sources. This one only reads: it indexes the contributors, plans every
merge in memory and returns the outputs for generated.build to write. The
contributor folders are never modified.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple

from . import archives, generated, kinds, mods, state, treewalk
from .ermlib import heal, merge
from .ermlib.errors import ErmError
from .ermlib.formats import bhd5
from .ermlib.paths import is_safe_relpath

REGULATION = heal.REGULATION


class ConflictError(ErmError):
    """Two mods provide the same file and nothing says how to resolve it."""


class Contributor(NamedTuple):
    mod: str
    source: Path  # the mod folder, or its view when the profile has rules for it


VanillaLoader = Callable[[str, str], bytes]


def contributors_of(organizer, views: dict[str, Path]) -> list[Contributor]:
    """Enabled package mods in priority order, lowest first, each through its view.

    Natives and the ERSC mod never contribute (ERSC's tree is copied into the
    generated mod, not merged), and neither does the generated mod itself. That
    one is excluded by name as well as by kind: a generated folder whose
    meta.ini lost its kind classifies as a package (it holds regulation.bin)
    and would otherwise be indexed beside the real providers. The order is
    never sorted: with three providers the folded bytes depend on it.
    """
    return [
        Contributor(ref.name, views.get(ref.name, ref.path))
        for ref in mods.enabled_mods(organizer)
        if ref.kind == kinds.PACKAGE and ref.name != generated.NAME
    ]


def index(contributors: list[Contributor]) -> dict[str, list[str]]:
    """{game-relative path: the mods providing it, in contributor order}."""
    found: dict[str, list[str]] = {}
    for contributor in contributors:
        for rel, _path in treewalk.package_files(contributor.source):
            found.setdefault(rel, []).append(contributor.mod)
    check_no_case_only_collisions(found)
    return found


def check_no_case_only_collisions(provided: dict[str, list[str]]) -> None:
    """Refuse paths that collide only under case-folding.

    Whether me3 resolves paths case-sensitively is unverified, so which of two
    differently cased spellings it would mount is a guess. The profile answers
    it with a rename or a prune. Nothing else notices these: msg/x.dcx and
    MSG/X.dcx index as two one-provider entries.
    """
    by_fold: dict[str, list[str]] = {}
    for rel in provided:
        by_fold.setdefault(rel.casefold(), []).append(rel)
    for variants in by_fold.values():
        if len(variants) > 1:
            detail = "; ".join(f"{v!r} (from {', '.join(provided[v])})" for v in sorted(variants))
            raise ConflictError(
                f"these paths differ only in case, so it's ambiguous which file me3 "
                f"would mount: {detail}. Rename one so the paths differ for real, or "
                "drop one of the mods."
            )


def declare_merges(merges: list[dict]) -> dict[str, dict]:
    """The merges by path, refusing two different declarations for one path."""
    declared: dict[str, dict] = {}
    for entry in merges:
        rel = entry["path"]
        prior = declared.get(rel)
        if prior is not None and prior != entry:
            raise ConflictError(
                f"{rel}: two [[merges]] entries disagree on how to resolve it, "
                f"{prior!r} against {entry!r}. Profiles compose, so this path picked up "
                "conflicting declarations from different includes; edit one so they agree."
            )
        declared[rel] = entry
    return declared


# -- the vanilla file a three-way merge compares against ----------------------


def make_vanilla_loader(paths: state.InstancePaths, downloads, sources: dict[str, dict]) -> VanillaLoader:
    """A loader for vanilla ancestors out of the archives the profile pins.

    `sources` is the profile's vanilla_sources table. The archive is read from
    the downloads folder after its sha256 matches the pinned one; the ancestor
    is its own artifact and is never installed as a mod.
    """

    def load(mod_id: str, member: str) -> bytes:
        entry = sources.get(mod_id)
        archive = entry.get("archive") if isinstance(entry, dict) else None
        if not archive or Path(archive).name != archive:
            raise ConflictError(
                f"vanilla archive missing: {mod_id!r} has no archive in this profile's vanilla_sources"
            )
        path = Path(downloads) / archive
        if not os.path.isfile(path):
            raise ConflictError(f"vanilla archive missing: {archive} is not in the downloads folder")
        pinned = entry.get("sha256")
        if pinned and archives.archive_sha256(paths, path) != pinned:
            raise ConflictError(f"vanilla archive {archive} does not match the sha256 the profile pins")
        try:
            with zipfile.ZipFile(path) as bundle:
                return bundle.read(member)
        except KeyError as exc:
            raise ConflictError(
                f"{member!r} is not in {archive}: check the member path against the archive's layout"
            ) from exc
        except (OSError, zipfile.BadZipFile) as exc:
            raise ConflictError(f"cannot read vanilla from {archive}: {exc}") from exc

    return load


def _load_vanilla(rel: str, rule: dict, loader: VanillaLoader) -> bytes:
    source = rule.get("vanilla")
    if not source:
        raise ConflictError(
            f"{rel}: strategy {rule['strategy']!r} compares both mods against the vanilla file "
            'they branched from, but no `vanilla` is declared. Add vanilla = { mod = "<id>", '
            'member = "<path inside its archive>" }'
        )
    mod_id, member = source.get("mod"), source.get("member")
    if not (mod_id and member):
        raise ConflictError(f"{rel}: `vanilla` needs both `mod` and `member`, got {source!r}")
    if not is_safe_relpath(member):
        raise ConflictError(f"{rel}: unsafe vanilla member path {member!r}")
    return loader(mod_id, member)


def declared_ancestor(merges: list[dict], rel: str, loader: VanillaLoader) -> bytes | None:
    """The ancestor a declared merge of `rel` compares against, or None.

    Asked for before the merge runs, to learn whether the mods branched from
    the installed game build. It is the same loader the merge uses, so the two
    cannot judge different files.
    """
    rule = declare_merges(merges).get(rel)
    if rule is None or rule["strategy"] not in merge.NEEDS_VANILLA:
        return None
    return _load_vanilla(rel, rule, loader)


def load_game_bases(merges: list[dict], game_dir) -> dict[str, bytes]:
    """The game's own copy of every path whose merge declares base = "game".

    A base that cannot be read is fatal: dropping it would leave a
    single-provider merge with nothing to fold onto, and the mod's stale copy
    would mount over the game's own file with no symptom.
    """
    found = {}
    for rule in merges or ():
        if rule.get("base") != "game":
            continue
        rel = rule["path"]
        try:
            found[rel] = bhd5.read_file(game_dir, rel)
        except bhd5.Bhd5Error as exc:
            raise ConflictError(
                f"{rel}: the merge asks for the game's own copy as its base, and it could "
                f"not be read ({exc}). Refusing rather than merging onto a mod's older "
                "snapshot of the same file."
            ) from exc
    return found


# -- planning -----------------------------------------------------------------


def plan(
    provided: dict[str, list[str]],
    contributors: list[Contributor],
    merges: list[dict],
    vanilla_loader: VanillaLoader,
    bases: dict[str, bytes] | None = None,
    notes: list | None = None,
    strategies: dict | None = None,
) -> list[tuple[str, bytes]]:
    """Every merge the profile declares, computed in memory: [(path, bytes)].

    A path two mods provide with no declared merge is refused, since me3 would
    load one and the other would silently not be in the game. `bases` maps a
    path to bytes that lead the fold; with one, a single provider is a merge
    too (the game's file on one side, the mod on the other). `notes` collects
    what a strategy could not carry over cleanly, tagged with the path.
    """
    strategies = merge.STRATEGIES if strategies is None else strategies
    sources = {contributor.mod: contributor.source for contributor in contributors}
    declared = declare_merges(merges)
    if notes is not None:
        installed = set(sources)
        for rel, rule in sorted(declared.items()):
            gone = tuple(m for m in rule["mods"] if m in installed and m not in set(provided.get(rel, ())))
            if gone:
                notes.append((rel, merge.StaleContributor(rel, gone)))
    planned = []
    for rel, providers in sorted(provided.items()):
        if len(providers) < 2 and rel not in (bases or {}):
            continue
        rule = declared.get(rel)
        if rule is None:
            raise ConflictError(
                f"{rel} is provided by {', '.join(providers)}. me3 loads only one of them, so "
                "the rest silently won't be in the game. Declare a [[merges]] entry for this "
                "path, or drop one of the mods."
            )
        strategy = strategies.get(rule["strategy"])
        if strategy is None:
            raise ConflictError(
                f"{rel}: unknown merge strategy {rule['strategy']!r} (known: {', '.join(sorted(strategies))})"
            )
        undeclared = set(providers) - set(rule["mods"])
        if undeclared:
            raise ConflictError(
                f"{rel}: merge declares mods={sorted(rule['mods'])}, but the actual providers are "
                f"{sorted(providers)}; {', '.join(sorted(undeclared))} also ship this path and "
                "aren't declared. Update the [[merges]] entry to include them deliberately, or "
                "drop the extra mod."
            )
        prefer = rule["prefer"]
        if prefer not in providers:
            raise ConflictError(
                f"{rel}: merge names prefer={prefer!r}, but the actual providers are "
                f"{', '.join(providers)}; prefer must be one of them"
            )
        # `prefer` wins genuine collisions, so it is the base the strategy builds on.
        ordered = [prefer] + [m for m in providers if m != prefer]
        blobs = [(sources[m] / rel).read_bytes() for m in ordered]
        lead = (bases or {}).get(rel)
        if lead is not None:
            blobs = [lead] + blobs
        vanilla = _load_vanilla(rel, rule, vanilla_loader) if rule["strategy"] in merge.NEEDS_VANILLA else None
        path_notes = [] if rule["strategy"] in merge.NEEDS_NOTES else None
        out = blobs[0]
        for step, extra in enumerate(blobs[1:]):
            args = (out, extra) if vanilla is None else (out, extra, vanilla)
            kwargs = {"notes": path_notes} if path_notes is not None else {}
            # Only the first step leads with the base the caller supplied; after
            # it `out` is a merge of mods, and a strategy that treats its base as
            # the game's file would read one mod's row as the game's.
            if step == 0 and lead is not None and rule["strategy"] in merge.TAKES_GAME_BASE:
                kwargs["base_is_game"] = True
            out = strategy(*args, **kwargs)
        planned.append((rel, out))
        if notes is not None and path_notes:
            notes.extend((rel, note) for note in path_notes)
    return planned


# -- rebasing onto the installed game build -----------------------------------


class Rebase(NamedTuple):
    bases: dict[str, bytes]
    rebased: bool
    baseline: bytes | None
    contributors: list[tuple[str, bytes]]


def rebase_bases(
    game_dir, live, merges: list[dict], contributors: list[Contributor],
    paths: state.InstancePaths, vanilla_loader: VanillaLoader,
) -> Rebase:
    """The per-path bases the merge should fold onto for the installed build."""
    regulations = [
        (c.mod, (c.source / REGULATION).read_bytes())
        for c in contributors
        if os.path.isfile(c.source / REGULATION)
    ]
    ancestor = declared_ancestor(merges, REGULATION, vanilla_loader) if len(regulations) > 1 else None
    rebased = heal.prepare_rebase(
        game_dir, live, ancestor, regulations, base=paths.baselines, cache_path=paths.regulation_cache
    )
    bases = {**rebased, **load_game_bases(merges, game_dir)}
    return Rebase(bases, bool(rebased), rebased.get(REGULATION), regulations)


def verify_rebase(planned: list[tuple[str, bytes]], rebase: Rebase, live) -> None:
    """Prove a rebase did what it claims before anything is built from it.

    Both ways it can go wrong are invisible from outside: a merge that came out
    on the mods' old build reverts the patch's game data, and a dropped row
    shows up in-game only as something that never happens.
    """
    if not rebase.rebased:
        return
    merged = dict(planned).get(REGULATION)
    if merged is None:
        raise heal.HealError(
            f"nothing merged {REGULATION}, but this run rebased it onto build {live.regulation}: "
            "the packages providing it changed underneath the merge; run it again"
        )
    problems = heal.verify_rebase(merged, rebase.baseline, rebase.contributors, live)
    if problems:
        raise heal.HealError(
            f"the merged {REGULATION} is not a faithful rebase onto build {live.regulation}:\n  "
            + "\n  ".join(problems)
            + "\nNothing was built; fix the mods and run it again."
        )
