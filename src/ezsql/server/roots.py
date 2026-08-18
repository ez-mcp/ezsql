"""Root resolution for EZSQL (plan §6.3 — Option B).

The explicit ``root`` tool parameter is the PRIMARY and ONLY root-resolution
mechanism. No ``list_roots()`` call (SEP-2577 deprecated Roots). The
``.ezsql/config.toml`` ``project_root`` field is the fallback. Missing both →
``FailureEnvelope(kind="missing_root")``. **Never ``Path.cwd()``.**

Security (T1): ``root`` is validated as absolute, existing, and a directory.
Symlinks are resolved. Semantic misuse (scanning ``/etc``) is accepted
residual risk (§17 Q1) — matches MCP's trust model.
"""

from pathlib import Path

from ezsql.config import EzsqlConfig
from ezsql.server.models import FailureEnvelope


def resolve_root(
    root_param: str | None,
    config: EzsqlConfig,
) -> Path | FailureEnvelope:
    """Resolve the project root from the tool parameter or config.

    Priority (plan §6.3 — Option B):
    1. ``root_param`` (explicit, agent-supplied) — primary.
    2. ``config.project_root`` — optional pinned default.
    3. Neither → ``FailureEnvelope(kind="missing_root")``.

    Never calls ``Path.cwd()``. Never calls ``list_roots()``.

    Args:
        root_param: The ``root`` tool parameter, or None.
        config: The loaded EZSQL config (may have ``project_root``).

    Returns:
        A resolved ``Path`` to an existing directory, or a
        ``FailureEnvelope`` explaining why resolution failed.
    """
    # 1. root_param (primary)
    if root_param is not None and root_param.strip():
        return _validate_root(root_param)

    # 2. config.project_root (fallback)
    if config.project_root and config.project_root.strip():
        return _validate_root(config.project_root)

    # 3. Neither — fail safely (never Path.cwd())
    return FailureEnvelope(
        kind="missing_root",
        detail="No project root provided. Pass the 'root' parameter with the "
               "absolute path to your project, or pin a default via "
               ".ezsql/config.toml [ezsql] project_root.",
        recoverable=True,
        next_steps=[
            "Pass the 'root' parameter with the absolute path to your project root.",
            "Or run 'ezsql init' to pin a default in .ezsql/config.toml.",
        ],
    )


def _validate_root(root_str: str) -> Path | FailureEnvelope:
    """Validate a root path string (T1.1–T1.3).

    - Must resolve to an absolute path (no ``..`` traversal after resolve).
    - Must be an existing directory (not a file, not a symlink to a file).
    - Symlinks are resolved to their target.
    """
    try:
        path = Path(root_str).resolve()
    except (OSError, ValueError) as exc:
        return FailureEnvelope(
            kind="invalid_root",
            detail=f"Could not resolve root path: {exc}",
            recoverable=True,
            next_steps=["Provide a valid absolute directory path."],
        )

    # Must be absolute after resolution (T1.1)
    if not path.is_absolute():
        return FailureEnvelope(
            kind="invalid_root",
            detail=f"Root path does not resolve to an absolute path: {root_str}",
            recoverable=True,
            next_steps=["Provide an absolute directory path."],
        )

    # Must exist (T1.2)
    if not path.exists():
        return FailureEnvelope(
            kind="invalid_root",
            detail=f"Root path does not exist: {path}",
            recoverable=True,
            next_steps=[
                "Verify the path is correct.",
                "Ensure the directory exists before calling find_context.",
            ],
        )

    # Must be a directory (T1.2 — not a file, not a symlink to a file)
    if not path.is_dir():
        return FailureEnvelope(
            kind="invalid_root",
            detail=f"Root path is not a directory: {path}",
            recoverable=True,
            next_steps=["Provide a directory path, not a file."],
        )

    return path


__all__ = ["resolve_root"]
