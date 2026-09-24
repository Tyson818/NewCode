"""定义式 Agent 的本地安全发现、校验和稳定来源合并。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import stat
from typing import Iterable

import yaml

from .types import (
    MAX_AGENT_BODY_BYTES,
    AgentCatalog,
    AgentDefinition,
    AgentDiagnostic,
    AgentSource,
    AgentValidationError,
    parse_agent_frontmatter,
    parse_agent_isolation,
)


PROJECT_AGENTS_RELATIVE_ROOT = Path(".newcode") / "agents"
USER_AGENTS_RELATIVE_ROOT = Path(".newcode") / "agents"
BUILTIN_AGENTS_RELATIVE_ROOT = Path("resources") / "agents"
MAX_AGENT_FRONTMATTER_BYTES = 16 * 1024
_SOURCE_ORDER = (AgentSource.PROJECT, AgentSource.USER, AgentSource.BUILTIN, AgentSource.PLUGIN)


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML mapping 重复键，避免隐式覆盖字段。"""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise AgentValidationError("subagent_definition_invalid") from exc
        if duplicate:
            raise AgentValidationError("subagent_definition_invalid")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class AgentDiscovery:
    """扫描 Markdown 定义；不执行正文、脚本或插件代码。"""

    def __init__(self, builtin_root: Path | None = None) -> None:
        self._builtin_root = (
            Path(builtin_root)
            if builtin_root is not None
            else Path(__file__).resolve().parent.parent / BUILTIN_AGENTS_RELATIVE_ROOT
        )

    def discover(
        self,
        workspace: Path,
        *,
        user_home: Path | None = None,
        builtin_root: Path | None = None,
        plugin_roots: Iterable[Path] = (),
        available_tools: Iterable[str] | None = None,
    ) -> AgentCatalog:
        workspace_root = Path(workspace).resolve(strict=False)
        home_root = (Path(user_home) if user_home is not None else Path.home()).resolve(strict=False)
        roots: dict[AgentSource, list[tuple[Path, Path]]] = {
            AgentSource.PROJECT: [(workspace_root / PROJECT_AGENTS_RELATIVE_ROOT, workspace_root)],
            AgentSource.USER: [(home_root / USER_AGENTS_RELATIVE_ROOT, home_root)],
            AgentSource.BUILTIN: [
                (Path(builtin_root) if builtin_root is not None else self._builtin_root, _filesystem_anchor(Path(builtin_root) if builtin_root is not None else self._builtin_root))
            ],
            AgentSource.PLUGIN: [
                (Path(root), _filesystem_anchor(Path(root))) for root in plugin_roots
            ],
        }
        tool_names = None if available_tools is None else frozenset(available_tools)
        diagnostics: list[AgentDiagnostic] = []
        candidates: dict[AgentSource, list[AgentDefinition]] = {source: [] for source in _SOURCE_ORDER}

        for source in _SOURCE_ORDER:
            for root, boundary in roots[source]:
                discovered, issues = self._scan_root(source, root, boundary, tool_names)
                candidates[source].extend(discovered)
                diagnostics.extend(issues)

        winners: dict[str, AgentDefinition] = {}
        for source in _SOURCE_ORDER:
            by_name: dict[str, list[AgentDefinition]] = {}
            for definition in candidates[source]:
                by_name.setdefault(definition.name.casefold(), []).append(definition)
            for normalized_name, definitions in by_name.items():
                if len(definitions) > 1:
                    diagnostics.extend(
                        AgentDiagnostic("subagent_definition_conflict", source, normalized_name)
                        for _ in definitions
                    )
                    continue
                if normalized_name not in winners:
                    winners[normalized_name] = definitions[0]

        ordered = tuple(sorted(winners.values(), key=lambda item: item.name))
        return AgentCatalog(ordered, tuple(diagnostics))

    def _scan_root(
        self,
        source: AgentSource,
        root: Path,
        boundary: Path,
        available_tools: frozenset[str] | None,
    ) -> tuple[list[AgentDefinition], list[AgentDiagnostic]]:
        diagnostics: list[AgentDiagnostic] = []
        safe, exists = _check_root(root, boundary)
        if not safe:
            return [], [AgentDiagnostic("subagent_path_unsafe", source)]
        if not exists:
            return [], diagnostics
        try:
            children = sorted(root.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        except OSError:
            return [], [AgentDiagnostic("subagent_discovery_failed", source)]

        definitions: list[AgentDefinition] = []
        for entry in children:
            if entry.suffix.casefold() != ".md":
                if entry.is_symlink():
                    diagnostics.append(AgentDiagnostic("subagent_path_unsafe", source))
                continue
            try:
                definition = _load_definition(source, root, entry, available_tools)
            except AgentValidationError as exc:
                diagnostics.append(AgentDiagnostic(exc.code, source))
            except (OSError, UnicodeError, yaml.YAMLError):
                diagnostics.append(AgentDiagnostic("subagent_discovery_failed", source))
            else:
                definitions.append(definition)
        return definitions, diagnostics


def _load_definition(
    source: AgentSource,
    root: Path,
    entry: Path,
    available_tools: frozenset[str] | None,
) -> AgentDefinition:
    if not _safe_regular_file(entry, root):
        raise AgentValidationError("subagent_path_unsafe")
    try:
        raw = entry.read_bytes()
    except OSError as exc:
        raise AgentValidationError("subagent_discovery_failed") from exc
    if len(raw) > MAX_AGENT_FRONTMATTER_BYTES + MAX_AGENT_BODY_BYTES:
        raise AgentValidationError("subagent_definition_invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentValidationError("subagent_definition_invalid") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter_text, body = _split_frontmatter(text)
    if len(frontmatter_text.encode("utf-8")) > MAX_AGENT_FRONTMATTER_BYTES:
        raise AgentValidationError("subagent_definition_invalid")
    if len(body.encode("utf-8")) > MAX_AGENT_BODY_BYTES:
        raise AgentValidationError("subagent_definition_invalid")
    try:
        raw_metadata = yaml.load(frontmatter_text, Loader=_UniqueKeyLoader)
    except AgentValidationError:
        raise
    except yaml.YAMLError as exc:
        raise AgentValidationError("subagent_definition_invalid") from exc
    name, description, allow, deny, model, iterations, permission_mode = parse_agent_frontmatter(raw_metadata)
    isolation = parse_agent_isolation(raw_metadata)
    if available_tools is not None and any(tool not in available_tools for tool in (*allow, *deny)):
        raise AgentValidationError("subagent_tool_unknown")
    return AgentDefinition(
        name=name,
        description=description,
        source=source,
        tools_allow=allow,
        tools_deny=deny,
        max_iterations=iterations,
        permission_mode=permission_mode,
        model=model,
        body=body.strip(),
        digest=hashlib.sha256(raw).hexdigest(),
        root=root,
        entry=entry,
        isolation=isolation,
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise AgentValidationError("subagent_definition_invalid")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AgentValidationError("subagent_definition_invalid")
    return text[4:end], text[end + len("\n---\n") :]


def _filesystem_anchor(path: Path) -> Path:
    absolute = path.absolute()
    return Path(absolute.anchor)


def _check_root(root: Path, boundary: Path) -> tuple[bool, bool]:
    raw = root.absolute()
    allowed = boundary.resolve(strict=False)
    try:
        relative = raw.relative_to(allowed)
    except ValueError:
        # Explicit built-in/plugin roots are allowed outside workspace/home but
        # must still be a non-symlink path within their filesystem anchor.
        allowed = _filesystem_anchor(raw)
        try:
            relative = raw.relative_to(allowed)
        except ValueError:
            return False, False
    if ".." in relative.parts:
        return False, False
    current = allowed
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return False, False
        except OSError:
            return False, False
    try:
        if not raw.exists():
            return True, False
        mode = raw.lstat().st_mode
    except OSError:
        return False, False
    if not stat.S_ISDIR(mode):
        return False, True
    try:
        raw.resolve(strict=True).relative_to(allowed.resolve(strict=False))
    except ValueError:
        # For explicit roots the earlier anchor is the containment boundary.
        if not _is_within(raw.resolve(strict=True), _filesystem_anchor(raw)):
            return False, True
    return True, True


def _safe_regular_file(path: Path, root: Path) -> bool:
    raw = path.absolute()
    try:
        relative = raw.relative_to(root.absolute())
    except ValueError:
        return False
    if not relative.parts or ".." in relative.parts:
        return False
    current = root.absolute()
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return False
        except OSError:
            return False
    try:
        mode = raw.lstat().st_mode
        raw.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(mode)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
