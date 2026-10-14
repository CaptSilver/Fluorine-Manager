"""The merge, as a function from mod folders to bytes.

erm's conflicts module merged by moving files between packages and deleting
the sources. This one only reads: it indexes the contributors, plans every
merge in memory and returns the outputs for generated.build to write. The
contributor folders are never modified.

Two mods shipping one file never stops a launch. A profile's [[merges]] entry
decides it when there is one; otherwise the engine merges what it has a
strategy for against the game's own copy, and the highest-priority mod wins the
rest. Paths are compared the way the game compares them, case-insensitively.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple

from . import archives, generated, kinds, mods, state, treewalk
from .ermlib import gamebuild, github, heal, merge
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


class Provider(NamedTuple):
    mod: str
    rel: str  # the spelling this mod ships, after the profile's renames and prunes
    path: Path  # the file itself


def canonical(rel: str) -> str:
    """The spelling the game knows `rel` by: lowercase.

    The archive index hashes the lowercased path (bhd5.normalise) and holds no
    original spellings, so the game cannot tell msg/engUS/x from msg/engus/x and
    the index cannot be asked which is right. Lowercase is the game's spelling
    for every path, indexed or not.
    """
    return rel.lower()


def listings_of(contributors: list[Contributor]) -> dict[str, list[treewalk.Entry]]:
    return {contributor.mod: treewalk.package_listing(contributor.source) for contributor in contributors}


def index(
    contributors: list[Contributor],
    listings: dict[str, list[treewalk.Entry]] | None = None,
    warnings: list[str] | None = None,
    dropped: dict[str, list[str]] | None = None,
) -> dict[str, list[Provider]]:
    """{game path: the providers, in contributor order}, paths in the game's spelling.

    A mod that ships one path under two spellings provides it once: the game's
    own spelling if it has it, else the first in sorted order. The spellings it
    loses go in `dropped` ({mod: [spelling]}) so they can be kept out of its
    view, and are named in `warnings`. `listings` are the contributors' walks
    when the caller already made them.
    """
    listings = listings_of(contributors) if listings is None else listings
    found: dict[str, list[Provider]] = {}
    for contributor in contributors:
        own: dict[str, Provider] = {}
        spellings: dict[str, list[str]] = {}
        for entry in listings[contributor.mod]:
            key = canonical(entry.rel)
            spellings.setdefault(key, []).append(entry.rel)
            kept = own.get(key)
            if kept is None or (entry.rel == key and kept.rel != key):
                own[key] = Provider(contributor.mod, entry.rel, entry.path)
        for key, provider in own.items():
            found.setdefault(key, []).append(provider)
            lost = [rel for rel in spellings[key] if rel != provider.rel]
            if not lost:
                continue
            if dropped is not None:
                dropped.setdefault(contributor.mod, []).extend(lost)
            if warnings is not None:
                warnings.append(
                    f"{contributor.mod} ships {' and '.join(sorted([provider.rel, *lost]))}, which the game reads as "
                    f"one path. {provider.rel} is the one used; the other is not loaded."
                )
    return found


# The strategies the engine tries on a colliding file the profile declares no
# merge for. The first table is for exact paths, the second for path endings.
AUTO_EXACT: dict[str, str] = {REGULATION: "param-rows"}
AUTO_SUFFIX: dict[str, str] = {".msgbnd.dcx": "fmg-3way", ".talkesdbnd.dcx": "esd-3way"}


def auto_strategy(rel: str) -> str | None:
    """The strategy for `rel` (in the game's spelling), or None when the engine has none."""
    if rel in AUTO_EXACT:
        return AUTO_EXACT[rel]
    for suffix, name in AUTO_SUFFIX.items():
        if rel.endswith(suffix):
            return name
    return None


def declare_merges(merges: list[dict]) -> dict[str, dict]:
    """The merges by path in the game's spelling, refusing two different declarations for one path."""
    declared: dict[str, dict] = {}
    for entry in merges:
        rel = canonical(entry["path"])
        prior = declared.get(rel)
        if prior is not None and {**prior, "path": rel} != {**entry, "path": rel}:
            raise ConflictError(
                f"{rel}: two [[merges]] entries disagree on how to resolve it, "
                f"{prior!r} against {entry!r}. Profiles compose, so this path picked up "
                "conflicting declarations from different includes; edit one so they agree."
            )
        declared[rel] = entry
    return declared


def _merging_paths(provided: dict[str, list[Provider]], merges: list[dict]) -> list[str]:
    """The paths whose output replaces what mods ship: more than one provider, or a merge that leads with the game."""
    led_by_game = {canonical(rule["path"]) for rule in merges if rule.get("base") == "game"}
    return [key for key, providers in sorted(provided.items()) if len(providers) > 1 or key in led_by_game]


def shadowed(
    provided: dict[str, list[Provider]], merges: list[dict], dropped: dict[str, list[str]] | None = None
) -> dict[str, list[str]]:
    """{mod: [spelling]} of the files me3 must not load: those a merged output replaces under another spelling.

    The output goes to the game's spelling, and a case-variant copy beside it is
    exactly the ambiguity the output is there to settle, so the mod's own file is
    pruned from its view. Depends on names only, so it can be known before any
    file is read.
    """
    found: dict[str, set[str]] = {}
    for key in _merging_paths(provided, merges):
        for provider in provided[key]:
            if provider.rel != key:
                found.setdefault(provider.mod, set()).add(provider.rel)
    for mod, rels in (dropped or {}).items():
        found.setdefault(mod, set()).update(rels)
    return {mod: sorted(rels) for mod, rels in sorted(found.items())}


def game_archives(game_dir) -> list[str]:
    """The archive files of bhd5.ARCHIVES that exist in the game folder, sorted by name.

    The automatic merges read the game's copy of a file out of these. A DLC
    archive installed later, or a Data0.bdt gone missing, changes what the
    engine can decide while no mod and no build id moves.
    """
    return sorted(
        f"{name}{ext}"
        for name in bhd5.ARCHIVES
        for ext in (".bhd", ".bdt")
        if (Path(game_dir) / f"{name}{ext}").is_file()
    )


def collision_map(
    provided: dict[str, list[Provider]],
    merges: list[dict],
    dropped: dict[str, list[str]] | None = None,
    archives=(),
) -> dict:
    """Every colliding path with its providers and spellings, and the strategy that will be tried.

    The generated mod's fingerprint takes this: what the engine decides for a
    path follows from the providers' files, which the listing digests cover,
    from this assignment, and from which game archives exist (`archives`), since
    that decides whether the game has a copy to merge against.
    """
    declared = declare_merges(merges)
    paths = {}
    for key in _merging_paths(provided, merges):
        rule = declared.get(key)
        paths[key] = {
            "providers": [[provider.mod, provider.rel] for provider in provided[key]],
            "strategy": rule["strategy"] if rule else auto_strategy(key),
        }
    return {
        "paths": paths,
        "dropped": {mod: sorted(rels) for mod, rels in sorted((dropped or {}).items())},
        "archives": sorted(archives),
    }


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
        if not isinstance(archive, str) or not archive or Path(archive).name != archive:
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
    if not isinstance(source, dict):
        raise ConflictError(f"{rel}: `vanilla` has to be a table with `mod` and `member`, got {source!r}")
    mod_id, member = source.get("mod"), source.get("member")
    if not (isinstance(mod_id, str) and isinstance(member, str) and mod_id and member):
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
        except (bhd5.Bhd5Error, OSError) as exc:
            raise ConflictError(
                f"{rel}: the merge asks for the game's own copy as its base, and it could "
                f"not be read ({exc}). Refusing rather than merging onto a mod's older "
                "snapshot of the same file."
            ) from exc
    return found


# -- the game's own copy for automatic merges ---------------------------------


class AutoResolver(NamedTuple):
    """What an automatic merge needs from the installed game.

    `game_copy(rel)` is the game's own file, which leads the fold and is the
    ancestor of every three-way step. `check_inputs` and `check_output` raise
    ConflictError when merging the providers' files would be unsound.
    """

    game_copy: Callable[[str], bytes]
    check_inputs: Callable[[str, list, bytes], None]
    check_output: Callable[[str, bytes, list, bytes], None]


def make_auto_resolver(game_dir, live, paths: state.InstancePaths) -> AutoResolver:
    """The resolver for the installed game: archives for msg/ and the like, the kept baseline for regulation.bin.

    regulation.bin is a loose file in Game/ and not in the archives; the kept
    baseline is that file, hashed against the installed build before it is used.
    Merging a regulation is only sound when every provider branched from the
    installed build, since the game's copy stands in for the ancestor and a
    provider's rows from an older build would read as its edits and revert the
    patch. The layout gate and the row check afterwards are the rebase's own.
    """
    copies: dict[str, bytes] = {}

    def game_copy(rel: str) -> bytes:
        if rel not in copies:
            try:
                if rel == REGULATION:
                    copies[rel] = heal.adopt_baseline(game_dir, live, base=paths.baselines).read_bytes()
                else:
                    copies[rel] = bhd5.read_file(game_dir, rel)
            except (ErmError, OSError) as exc:
                raise ConflictError(f"the game's own copy of {rel} could not be read: {exc}") from exc
        return copies[rel]

    def check_inputs(rel: str, blobs: list, game: bytes) -> None:
        if rel != REGULATION:
            return
        for mod, blob in blobs:
            built = gamebuild.cached_regulation_version(blob, paths.regulation_cache)
            if built != live.regulation:
                raise ConflictError(
                    f"{mod}'s regulation.bin was built for game build {built} and the game is {live.regulation}. "
                    "Without the file it branched from, its edits cannot be told from the patch's. A [[merges]] "
                    "entry with a vanilla ancestor rebases it."
                )
        problems = heal.layout_gate(game, blobs)
        if problems:
            raise ConflictError("the param layout moved: " + "; ".join(problems))

    def check_output(rel: str, merged: bytes, blobs: list, game: bytes) -> None:
        if rel != REGULATION:
            return
        problems = heal.verify_rebase(merged, game, blobs, live)
        if problems:
            raise ConflictError("the merged regulation.bin is not a faithful merge onto the game's: " + "; ".join(problems))

    return AutoResolver(game_copy, check_inputs, check_output)


# -- planning -----------------------------------------------------------------

MERGED, WINNER, IDENTICAL = "merged", "winner", "identical"


class Decision(NamedTuple):
    """What the engine did with one colliding path.

    `providers` are the mods shipping it, lowest priority first. `winner` is set
    when the file went whole to one mod. `detail` is the reason an automatic merge
    fell back, or what a declared merge had to adapt to.
    """

    path: str
    outcome: str
    providers: tuple
    winner: str | None
    strategy: str | None
    declared: bool
    detail: str

    def to_json(self) -> dict:
        return {**self._asdict(), "providers": list(self.providers)}


class Plan(NamedTuple):
    # (path in the game's spelling, the bytes, or the file to copy when a mod's file wins whole)
    outputs: list
    decisions: list


def decisions_from_state(raw) -> list[Decision]:
    """The decisions a build recorded. Anything in the record that does not read back as one is dropped."""
    found = []
    for item in raw if isinstance(raw, list) else ():
        try:
            if item["outcome"] not in (MERGED, WINNER, IDENTICAL):
                continue
            winner, strategy = item.get("winner"), item.get("strategy")
            found.append(
                Decision(
                    str(item["path"]), item["outcome"], tuple(str(mod) for mod in item["providers"]),
                    winner if isinstance(winner, str) else None,
                    strategy if isinstance(strategy, str) else None,
                    bool(item.get("declared")), str(item.get("detail", "")),
                )
            )
        except (KeyError, TypeError, AttributeError):
            continue
    return found


def warning(decision: Decision) -> str | None:
    """The line for the launch report and the Problems list, or None when there is nothing to warn about."""
    if decision.outcome == WINNER:
        others = [mod for mod in decision.providers if mod != decision.winner]
        return (
            f"{decision.path} is shipped by {', '.join(decision.providers)} and could not be merged "
            f"({decision.detail}). {decision.winner} has the highest priority, so its copy is used and what "
            f"{', '.join(others)} change in it is not in the game."
        )
    if decision.detail:
        return f"{decision.path}: {decision.detail}"
    return None


def warnings_for(decisions: list[Decision]) -> list[str]:
    return [text for text in map(warning, decisions) if text]


def describe(decision: Decision) -> str:
    how = "the profile's merge entry" if decision.declared else f"the {decision.strategy} strategy"
    return f"{decision.path}: merged from {', '.join(decision.providers)} with {how}"


def _identical(providers: list[Provider]) -> bool:
    first = providers[0].path
    size = os.path.getsize(first)
    if any(os.path.getsize(provider.path) != size for provider in providers[1:]):
        return False
    digest = github.sha256_file(first)
    return all(github.sha256_file(provider.path) == digest for provider in providers[1:])


def _reason(exc: Exception) -> str:
    return str(exc) if isinstance(exc, ErmError) else f"{type(exc).__name__}: {exc}"


def _fold(blobs, vanilla, name, strategy, path_notes, lead_is_game):
    """`blobs[0]` merged with each later blob in turn; `lead_is_game` says blobs[0] is the game's own file.

    The strategy gets the vanilla when there is one and collects what it could
    not carry over into `path_notes` when that is a list.
    """
    out = blobs[0]
    for step, extra in enumerate(blobs[1:]):
        args = (out, extra) if vanilla is None else (out, extra, vanilla)
        kwargs = {"notes": path_notes} if path_notes is not None else {}
        # Only the first step leads with the game's file; after it `out` is a merge
        # of mods, and a strategy that treats its base as the game's file would
        # read one mod's row as the game's.
        if step == 0 and lead_is_game and name in merge.TAKES_GAME_BASE:
            kwargs["base_is_game"] = True
        out = strategy(*args, **kwargs)
    return out


def _declared(key, rule, providers, lead, vanilla_loader, strategies, notes):
    """One declared merge. The entry decides how, and tolerates the mod set having moved since it was written."""
    strategy = strategies.get(rule["strategy"])
    if strategy is None:
        raise ConflictError(
            f"{key}: unknown merge strategy {rule['strategy']!r} (known: {', '.join(sorted(strategies))})"
        )
    names = [provider.mod for provider in providers]
    detail = []
    outside = sorted(set(names) - set(rule["mods"]))
    if outside:
        detail.append(
            f"the [[merges]] entry does not name {', '.join(outside)}, but the strategy folds in every mod "
            "that ships the file"
        )
    leader = rule["prefer"]
    if leader not in names:
        leader = names[-1]
        detail.append(
            f"prefer = {rule['prefer']!r} is not one of its providers, so {leader}, the highest priority, leads"
        )
    # `prefer` wins genuine collisions, so it is the base the strategy builds on.
    ordered = [leader] + [mod for mod in names if mod != leader]
    files = {provider.mod: provider.path for provider in providers}
    blobs = [files[mod].read_bytes() for mod in ordered]
    if lead is not None:
        blobs = [lead] + blobs
    vanilla = _load_vanilla(key, rule, vanilla_loader) if rule["strategy"] in merge.NEEDS_VANILLA else None
    path_notes = [] if rule["strategy"] in merge.NEEDS_NOTES else None
    try:
        out = _fold(blobs, vanilla, rule["strategy"], strategy, path_notes, lead is not None)
    except Exception as exc:  # a mod's file can be malformed in any way; the entry's merge stops with the reason
        reason = _reason(exc)
        raise ConflictError(
            f"{key}: the [[merges]] entry's {rule['strategy']} merge of {', '.join(names)} failed: {reason}. "
            "Fix the mods, or delete the entry: without it the engine merges this file itself and, "
            "when it cannot, the highest-priority mod wins."
        ) from exc
    if notes is not None and path_notes:
        notes.extend((key, note) for note in path_notes)
    return out, Decision(key, MERGED, tuple(names), None, rule["strategy"], True, "; ".join(detail))


def _auto_fold(key, name, providers, strategies, auto):
    """The providers merged onto the game's copy, highest priority first, and what the strategy could not carry over."""
    strategy = strategies.get(name)
    if strategy is None:
        raise ConflictError(f"no merge strategy called {name!r}")
    if auto is None:
        raise ConflictError("the game's own copy is not available to merge against")
    game = auto.game_copy(key)
    blobs = [(provider.mod, provider.path.read_bytes()) for provider in reversed(providers)]
    auto.check_inputs(key, blobs, game)
    three_way = name in merge.NEEDS_VANILLA
    path_notes: list = [] if name in merge.NEEDS_NOTES else None
    out = _fold([game] + [blob for _mod, blob in blobs], game if three_way else None, name, strategy, path_notes, True)
    auto.check_output(key, out, blobs, game)
    return out, path_notes or []


def _automatic(key, providers, strategies, auto, notes):
    """A collision the profile declares nothing for: the same bytes, a merge, or the highest-priority mod's file."""
    names = tuple(provider.mod for provider in providers)
    winner = providers[-1]
    if _identical(providers):
        # One copy is enough, unless a mod's own spelling is about to be pruned: then the
        # game's spelling needs a file of its own.
        out = None if all(provider.rel == key for provider in providers) else winner.path
        return out, Decision(key, IDENTICAL, names, None, None, False, "")
    name = auto_strategy(key)
    if name is None:
        reason = "there is no way to merge this kind of file"
    else:
        try:
            merged, path_notes = _auto_fold(key, name, providers, strategies, auto)
        except Exception as exc:  # a mod's file can be malformed in any way; the reason goes in the warning
            reason = _reason(exc)
        else:
            if notes is not None:
                notes.extend((key, note) for note in path_notes)
            return merged, Decision(key, MERGED, names, None, name, False, "")
    return winner.path, Decision(key, WINNER, names, winner.mod, name, False, reason)


def plan(
    provided: dict[str, list[Provider]],
    contributors: list[Contributor],
    merges: list[dict],
    vanilla_loader: VanillaLoader,
    bases: dict[str, bytes] | None = None,
    notes: list | None = None,
    strategies: dict | None = None,
    auto: AutoResolver | None = None,
) -> Plan:
    """Every colliding path resolved, computed in memory: the outputs to write and a Decision for each.

    A path the profile declares a merge for is merged as declared. Any other
    path two or more mods ship is merged automatically when the engine has a
    strategy for it and the game's own copy to merge against, and goes whole to
    the highest-priority mod otherwise. Nothing here refuses a launch because
    mods collide. `bases` maps a path to bytes that lead a declared fold; with
    one, a single provider is a merge too. `notes` collects what a strategy could
    not carry over cleanly, tagged with the path.
    """
    strategies = merge.STRATEGIES if strategies is None else strategies
    installed = {contributor.mod for contributor in contributors}
    declared = declare_merges(merges)
    bases = {canonical(rel): blob for rel, blob in (bases or {}).items()}
    if notes is not None:
        for rel, rule in sorted(declared.items()):
            have = {provider.mod for provider in provided.get(rel, ())}
            gone = tuple(m for m in rule["mods"] if m in installed and m not in have)
            if gone:
                notes.append((rel, merge.StaleContributor(rel, gone)))
    outputs: list = []
    decisions: list[Decision] = []
    for key, providers in sorted(provided.items()):
        rule = declared.get(key)
        if len(providers) < 2 and not (rule is not None and key in bases):
            continue
        if rule is not None:
            out, decision = _declared(key, rule, providers, bases.get(key), vanilla_loader, strategies, notes)
        else:
            out, decision = _automatic(key, providers, strategies, auto, notes)
        if out is not None:
            outputs.append((key, out))
        decisions.append(decision)
    return Plan(outputs, decisions)


# -- rebasing onto the installed game build -----------------------------------


class Rebase(NamedTuple):
    bases: dict[str, bytes]
    rebased: bool
    baseline: bytes | None
    contributors: list[tuple[str, bytes]]


def rebase_bases(
    game_dir, live, merges: list[dict], provided: dict[str, list[Provider]],
    paths: state.InstancePaths, vanilla_loader: VanillaLoader,
) -> Rebase:
    """The per-path bases the merge should fold onto for the installed build."""
    regulations = [(provider.mod, provider.path.read_bytes()) for provider in provided.get(REGULATION, ())]
    ancestor = declared_ancestor(merges, REGULATION, vanilla_loader) if len(regulations) > 1 else None
    rebased = heal.prepare_rebase(
        game_dir, live, ancestor, regulations, base=paths.baselines, cache_path=paths.regulation_cache
    )
    bases = {**rebased, **load_game_bases(merges, game_dir)}
    return Rebase(bases, bool(rebased), rebased.get(REGULATION), regulations)


def verify_rebase(planned: list, rebase: Rebase, live) -> None:
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
    if isinstance(merged, Path):
        merged = merged.read_bytes()  # a file that won whole is verified like merged bytes
    problems = heal.verify_rebase(merged, rebase.baseline, rebase.contributors, live)
    if problems:
        raise heal.HealError(
            f"the merged {REGULATION} is not a faithful rebase onto build {live.regulation}:\n  "
            + "\n  ".join(problems)
            + "\nNothing was built; fix the mods and run it again."
        )
