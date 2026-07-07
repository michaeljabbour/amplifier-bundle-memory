"""
Pin the interject hook's documented mempalace store path/collection against
the REAL mempalace package, so a future mempalace release that changes its
defaults is caught here rather than silently reintroducing the
~/.mempalace/chroma / mempalace_default read-write mismatch (the original
bug this module was fixed for -- see the module docstring's "Read lane"
section).

If mempalace is importable in this test environment, the expected values
are derived directly from ``mempalace.config`` (the authoritative source).
Otherwise we fall back to the documented constants, which were verified by
hand against the installed mempalace 3.5.0 package
(``mempalace/config.py``: ``DEFAULT_PALACE_PATH``, ``DEFAULT_COLLECTION_NAME``)
at the time this fix was written.
"""

from __future__ import annotations

import amplifier_module_hooks_mempalace_interject as interject


def _real_mempalace_defaults() -> tuple[str, str] | None:
    """Return (palace_path, collection_name) from the installed mempalace
    package's own config module, or None if mempalace is not importable."""
    try:
        import mempalace.config as mp_config  # type: ignore
    except ImportError:
        return None
    return (mp_config.DEFAULT_PALACE_PATH, mp_config.DEFAULT_COLLECTION_NAME)


def test_documented_palace_path_matches_reality():
    """DOCUMENTED_MEMPALACE_PALACE_PATH must equal mempalace's real default.

    Fails loudly (rather than silently passing) if mempalace is importable
    and its default has drifted from what this module's docstring/comments
    claim -- that drift is exactly the class of bug this fix addresses.
    """
    real = _real_mempalace_defaults()
    if real is None:
        # mempalace not installed in this test environment -- fall back to
        # asserting the documented constant is the one verified by hand.
        assert interject.DOCUMENTED_MEMPALACE_PALACE_PATH == "~/.mempalace/palace"
        return
    real_path, _ = real
    # mempalace's DEFAULT_PALACE_PATH is already ~-expanded (os.path.expanduser);
    # our documented constant is the un-expanded, human-readable form.
    import os

    assert os.path.expanduser(interject.DOCUMENTED_MEMPALACE_PALACE_PATH) == real_path


def test_documented_collection_name_matches_reality():
    real = _real_mempalace_defaults()
    if real is None:
        assert interject.DOCUMENTED_MEMPALACE_COLLECTION_NAME == "mempalace_drawers"
        return
    _, real_collection = real
    assert interject.DOCUMENTED_MEMPALACE_COLLECTION_NAME == real_collection


def test_documented_path_and_collection_are_not_the_old_wrong_values():
    """Regression guard for the exact reported bug: interject must never
    again default to ~/.mempalace/chroma / mempalace_default -- the store
    the palace CLI does not write to."""
    assert interject.DOCUMENTED_MEMPALACE_PALACE_PATH != "~/.mempalace/chroma"
    assert interject.DOCUMENTED_MEMPALACE_COLLECTION_NAME != "mempalace_default"


def test_module_never_hardcodes_a_chroma_path_at_runtime():
    """Structural guard: the module must not import chromadb or construct a
    PersistentClient path at all -- retrieval routes exclusively through
    mempalace's own mempalace_search MCP tool (see _mcp_search).

    Checks actual code constructs (import statements / call sites), not the
    module docstring -- which legitimately *describes* the old ChromaDB
    bug in prose as part of the fix's changelog.
    """
    import ast
    import inspect

    source = inspect.getsource(interject)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name == "chromadb" for alias in node.names), (
                "module must not import chromadb"
            )
        if isinstance(node, ast.ImportFrom):
            assert node.module != "chromadb", "module must not import from chromadb"
        if isinstance(node, ast.Attribute) and node.attr == "PersistentClient":
            raise AssertionError(
                "module must not construct a chromadb PersistentClient"
            )

    assert not hasattr(interject, "_retrieve_memories")
    assert not hasattr(interject, "_embed")
    assert not hasattr(interject, "_cosine")
    assert hasattr(interject, "_mcp_search")
