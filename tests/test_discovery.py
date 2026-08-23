"""Slot 51 — the discovery scope.

`PathJail` gets an escape suite because a hole in it is unattended damage. This
guard reaches *outside* that jail by design, so it gets the same treatment and
one extra question the jail never has to answer: **can a denied directory be
smuggled back in under an allowed root?**

The line this guard draws is paths, never contents. Every test below is really
asking one of two things: can it be made to look somewhere it should not, and
can it be made to hand back something it should not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aop.guards.denial import GuardDenied
from aop.guards.discovery import DEFAULT_DENY_NAMES, DiscoveryScope

PROJECT = Path(__file__).resolve().parents[1]
TEST_CONFIG = Path(__file__).resolve().parent / "config"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """An allowed root with something worth finding, and something not."""
    (tmp_path / "allowed" / "proj").mkdir(parents=True)
    (tmp_path / "allowed" / "proj" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "allowed" / "proj" / "notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "allowed" / "proj" / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "allowed" / "proj" / "id_rsa").write_text("KEY", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "main.py").write_text("x", encoding="utf-8")
    return tmp_path


@pytest.fixture
def scope(tree: Path) -> DiscoveryScope:
    return DiscoveryScope([tree / "allowed"], deny_dirs=[str(tree / "denied")])


# ================================ deny by default ===========================


def test_no_roots_means_nothing_is_searchable(tmp_path):
    """The shipped default. Same posture as the command allowlist."""
    empty = DiscoveryScope()
    assert not empty.is_searchable(tmp_path)
    with pytest.raises(GuardDenied) as caught:
        empty.resolve_for_search(tmp_path)
    assert "no discovery roots" in str(caught.value)


def test_refusing_to_look_is_not_the_same_as_finding_nothing(tmp_path):
    """A tool that returned `[]` here would teach the model that the machine is
    empty, rather than that it was not allowed to look."""
    with pytest.raises(GuardDenied):
        DiscoveryScope().locate("*.py")


# ================================ containment ===============================


def test_a_path_outside_every_root_is_refused(scope, tree):
    with pytest.raises(GuardDenied) as caught:
        scope.resolve_for_search(tree / "outside")
    assert "not under any discovery root" in str(caught.value)


def test_traversal_out_of_an_allowed_root_is_refused(scope, tree):
    with pytest.raises(GuardDenied):
        scope.resolve_for_search(str(tree / "allowed" / ".." / "outside"))


def test_containment_is_by_parts_not_by_prefix(tmp_path):
    """`allowed-evil` is not inside `allowed`. The classic version of this bug."""
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed-evil").mkdir()
    scope = DiscoveryScope([tmp_path / "allowed"])
    assert not scope.is_searchable(tmp_path / "allowed-evil")


@pytest.mark.parametrize(
    "hostile",
    [
        "",
        "   ",
        "\\\\server\\share",
        "//server/share",
        "D:relative",
        "C:/tmp/CON",
        "C:/tmp/NUL.txt",
        "C:/tmp/COM1",
        "C:/tmp/file.txt:hidden",
    ],
)
def test_the_jails_syntax_hazards_are_refused_here_too(scope, hostile):
    """Reused from `pathjail`, not restated — so a hazard learned in one guard
    cannot be missing from the other."""
    with pytest.raises(GuardDenied):
        scope.resolve_for_search(hostile)


def test_a_nul_byte_is_refused(scope):
    with pytest.raises(GuardDenied):
        scope.resolve_for_search("C:/tmp/a\x00b")


# ============================ the denylist always wins ======================


def test_a_denied_directory_nested_in_an_allowed_root_still_loses(tmp_path):
    """The question the jail never has to answer.

    Adding `~` as a root must not silently re-expose `~/.ssh`.
    """
    (tmp_path / "home" / "secrets").mkdir(parents=True)
    scope = DiscoveryScope(
        [tmp_path / "home"], deny_dirs=[str(tmp_path / "home" / "secrets")]
    )
    assert scope.is_searchable(tmp_path / "home")
    with pytest.raises(GuardDenied) as caught:
        scope.resolve_for_search(tmp_path / "home" / "secrets")
    assert "denylist" in str(caught.value)


def test_a_denied_directorys_children_are_denied(tmp_path):
    (tmp_path / "home" / "secrets" / "deep").mkdir(parents=True)
    scope = DiscoveryScope(
        [tmp_path / "home"], deny_dirs=[str(tmp_path / "home" / "secrets")]
    )
    assert not scope.is_searchable(tmp_path / "home" / "secrets" / "deep")


def test_the_walk_never_descends_into_a_denied_directory(tmp_path):
    (tmp_path / "home" / "secrets").mkdir(parents=True)
    (tmp_path / "home" / "secrets" / "target.py").write_text("x", encoding="utf-8")
    (tmp_path / "home" / "ok.py").write_text("x", encoding="utf-8")
    scope = DiscoveryScope(
        [tmp_path / "home"], deny_dirs=[str(tmp_path / "home" / "secrets")]
    )
    names = {Path(f.path).name for f in scope.locate("*.py")}
    assert names == {"ok.py"}


@pytest.mark.parametrize(
    "secret",
    [".env", ".env.production", "id_rsa", "server.pem", "app.key", ".netrc"],
)
def test_credential_filenames_are_never_returned(tmp_path, secret):
    """A denied *name* catches the secret dropped into an otherwise reasonable
    project folder, which a denied *directory* would miss entirely."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / secret).write_text("SECRET", encoding="utf-8")
    scope = DiscoveryScope([tmp_path])
    assert scope.locate("*") == [] or all(
        Path(f.path).name != secret for f in scope.locate("*")
    )


def test_the_default_denylist_covers_the_obvious_credential_stores():
    scope = DiscoveryScope([Path.home()])
    for spot in ("~/.ssh", "~/.aws", "~/.gnupg", "~/.claude"):
        assert not scope.is_searchable(Path(spot).expanduser()), spot


def test_the_shipped_deny_names_are_not_empty():
    """A regression guard: emptying this list disables the name filter while
    every other test here still passes."""
    assert len(DEFAULT_DENY_NAMES) >= 10
    assert ".env" in DEFAULT_DENY_NAMES


# ================================= symlinks =================================


def _link_dir(link: Path, target: Path) -> None:
    """Create a directory link, however this platform will let us.

    Windows refuses `symlink_to` without SeCreateSymbolicLinkPrivilege, so a
    plain `skipif(os.name == "nt")` would skip the escape that matters most on
    the only platform this actually runs on. A **junction** needs no privilege
    and `os.path.realpath` resolves it identically, so the guard is tested
    against a real reparse point here rather than assumed.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass
    if os.name != "nt":
        pytest.skip("cannot create a directory link on this platform")
    import subprocess

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip(f"cannot create a junction here: {result.stderr.strip()}")


def test_a_link_out_of_an_allowed_root_is_not_followed(tmp_path):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.py").write_text("x", encoding="utf-8")
    _link_dir(tmp_path / "allowed" / "link", tmp_path / "outside")

    scope = DiscoveryScope([tmp_path / "allowed"])
    assert [Path(f.path).name for f in scope.locate("secret.py")] == []


def test_a_link_pointing_at_a_denied_directory_is_refused(tmp_path):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "secrets").mkdir()
    _link_dir(tmp_path / "allowed" / "link", tmp_path / "secrets")

    scope = DiscoveryScope([tmp_path / "allowed"], deny_dirs=[str(tmp_path / "secrets")])
    with pytest.raises(GuardDenied):
        scope.resolve_for_search(tmp_path / "allowed" / "link")


def test_a_link_into_an_allowed_root_still_cannot_return_a_denied_name(tmp_path):
    """Resolution happens per *result*, not just at the search root."""
    (tmp_path / "allowed" / "proj").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / ".env").write_text("SECRET", encoding="utf-8")
    _link_dir(tmp_path / "allowed" / "proj" / "vendor", tmp_path / "elsewhere")

    scope = DiscoveryScope([tmp_path / "allowed"])
    assert [f for f in scope.locate(".env")] == []


# ================================== locating ================================


def test_locate_finds_by_glob(scope):
    names = {Path(f.path).name for f in scope.locate("*.py")}
    assert names == {"main.py"}


def test_locate_reports_size_and_age_not_content(scope, tree):
    (found,) = scope.locate("notes.md").hits
    assert found.size_bytes == 1
    assert found.modified.tzinfo is not None
    assert not hasattr(found, "content")
    assert "x" not in found.path or Path(found.path).name == "notes.md"


def test_locate_never_leaves_the_allowed_root(scope, tree):
    """`main.py` exists both inside and outside. Only one may come back."""
    paths = [Path(f.path) for f in scope.locate("main.py")]
    assert all("outside" not in p.parts for p in paths)
    assert len(paths) == 1


def test_locate_respects_the_result_cap(tmp_path):
    (tmp_path / "many").mkdir()
    for i in range(50):
        (tmp_path / "many" / f"f{i}.txt").write_text("x", encoding="utf-8")
    scope = DiscoveryScope([tmp_path], max_results=10)
    assert len(scope.locate("*.txt")) == 10


def test_an_explicit_limit_cannot_exceed_the_configured_cap(tmp_path):
    (tmp_path / "many").mkdir()
    for i in range(30):
        (tmp_path / "many" / f"f{i}.txt").write_text("x", encoding="utf-8")
    scope = DiscoveryScope([tmp_path], max_results=5)
    assert len(scope.locate("*.txt", limit=999)) == 5


def test_searching_an_explicit_root_must_still_be_inside_the_allowlist(scope, tree):
    with pytest.raises(GuardDenied):
        scope.locate("*.py", root=tree / "outside")


def test_locate_finds_directories_too(tmp_path):
    (tmp_path / "code" / "myproject").mkdir(parents=True)
    scope = DiscoveryScope([tmp_path])
    found = scope.locate("myproject").hits
    assert len(found) == 1
    assert found[0].is_dir


# ================================== wiring ==================================
#
# "A component that is green in isolation may still be wired to nothing." The
# Slot 42 audit found `due_for_resume()`, `lifecycle.resume()` and the whole
# suspension mechanism consumed by nothing at all — every part tested, no caller.
# These assert the caller exists.


def test_the_operator_builds_the_scope_from_policy(tmp_path):
    from aop.core.config import load_settings
    from aop.operator import Operator

    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    settings.policy.discovery.roots = [str(tmp_path / "code")]
    settings.policy.discovery.max_results = 7
    (tmp_path / "code").mkdir()

    operator = Operator(settings)
    assert operator.discovery.roots == (Path(os.path.realpath(tmp_path / "code")),)
    assert operator.discovery.max_results == 7


def test_the_shipped_default_grants_nothing(tmp_path):
    """Every shipped policy must be closed. A guard that ships open is a guard
    that was never a guard."""
    from aop.core.config import load_settings

    for config in ("config", "config-claude", "config-deepseek"):
        settings = load_settings(PROJECT / config, project_root=tmp_path)
        assert settings.policy.discovery.roots == [], config


def test_the_locate_tool_is_offered_only_when_roots_exist(tmp_path):
    """An always-present tool that always refuses trains the model to stop
    asking, and burns a round trip every time it re-learns that."""
    from aop.execution.tools import build_toolbox
    from aop.guards import PathJail

    jail = PathJail(tmp_path / "ws")
    assert "locate" not in build_toolbox(jail).names()
    assert "locate" not in build_toolbox(jail, discovery=DiscoveryScope()).names()
    assert "locate" in build_toolbox(jail, discovery=DiscoveryScope([tmp_path])).names()


async def test_the_tool_returns_paths_and_refuses_to_leave_the_scope(tmp_path):
    """Through the ToolBox, the way a worker actually reaches it."""
    from aop.execution.tools import build_toolbox
    from aop.guards import PathJail

    (tmp_path / "allowed" / "proj").mkdir(parents=True)
    (tmp_path / "allowed" / "proj" / "main.py").write_text("secret content", encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "other.py").write_text("x", encoding="utf-8")

    import json

    from aop.registry.adapter import ToolCall

    box = build_toolbox(
        PathJail(tmp_path / "ws"), discovery=DiscoveryScope([tmp_path / "allowed"])
    )

    async def call(**args) -> str:
        return await box.dispatch(
            ToolCall(id="c1", name="locate", arguments=json.dumps(args))
        )

    out = await call(pattern="*.py")
    assert "main.py" in out
    assert "secret content" not in out, "locate must never return file contents"

    # A denial travels back as a structured tool message, never as a crash —
    # cheap round trip, same tier, cache intact, never an escalation.
    denied = await call(pattern="*.py", root=str(tmp_path / "elsewhere"))
    assert "discovery" in denied.lower()
    assert "other.py" not in denied


# ================================== bounds ==================================
#
# Running this against a real home directory is how these got here: a pattern
# matching one directory walked the whole tree and had not finished in two
# minutes. A cap on RESULTS does not bound a search that finds nothing — only
# depth and a clock do. Inside a task that is unbounded wall clock, which is the
# same shape as F-06.


def test_a_deep_tree_is_bounded_by_depth(tmp_path):
    deep = tmp_path
    for i in range(12):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "buried.txt").write_text("x", encoding="utf-8")

    shallow = DiscoveryScope([tmp_path], max_depth=3)
    assert shallow.locate("buried.txt").hits == []

    deeper = DiscoveryScope([tmp_path], max_depth=20)
    assert len(deeper.locate("buried.txt").hits) == 1


def test_a_search_that_finds_nothing_still_terminates(tmp_path):
    """The defect found by running it. The result cap cannot stop this one."""
    import time as _time

    base = tmp_path
    for i in range(6):
        base = base / f"level{i}"
    base.mkdir(parents=True)
    for i in range(200):
        (base / f"file{i}.txt").write_text("x", encoding="utf-8")

    scope = DiscoveryScope([tmp_path], timeout_seconds=0.5, max_depth=20)
    started = _time.monotonic()
    result = scope.locate("nothing-matches-this-*")
    assert _time.monotonic() - started < 5.0
    assert result.hits == []


def test_truncation_says_why(tmp_path):
    """"Found nothing" and "gave up" are different answers."""
    (tmp_path / "many").mkdir()
    for i in range(20):
        (tmp_path / "many" / f"f{i}.txt").write_text("x", encoding="utf-8")

    capped = DiscoveryScope([tmp_path], max_results=5).locate("*.txt")
    assert len(capped.hits) == 5
    assert capped.truncated == "result cap"

    complete = DiscoveryScope([tmp_path], max_results=500).locate("*.txt")
    assert complete.truncated is None


def test_the_tool_surfaces_truncation_to_the_model(tmp_path):
    """A model told "no matches" stops asking; one told the search was cut short
    can narrow it."""
    from aop.execution.tools import DiscoveryTools

    (tmp_path / "many").mkdir()
    for i in range(20):
        (tmp_path / "many" / f"f{i}.txt").write_text("x", encoding="utf-8")

    out = DiscoveryTools(DiscoveryScope([tmp_path], max_results=5)).locate("*.txt")
    assert "truncated" in out
