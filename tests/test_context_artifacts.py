import json
from pathlib import Path

import pytest

from newcode.context.artifacts import ArtifactStore


def test_artifact_is_redacted_and_inside_workspace(tmp_path: Path):
    store = ArtifactStore(tmp_path, "session_1", ("secret-value",))
    result = store.write("read_file", {"token": "x", "content": "secret-value"})
    path = tmp_path / result.relative_path
    assert path.is_file()
    assert "secret-value" not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["data"]["token"] == "[REDACTED]"


def test_artifact_store_is_bound_to_each_worktree_root(tmp_path: Path):
    main = tmp_path / "main"
    child = tmp_path / "child"
    main.mkdir()
    child.mkdir()
    main_store = ArtifactStore(main, "same-session")
    child_store = ArtifactStore(child, "same-session")

    main_result = main_store.write("read_file", {"content": "main"})
    child_result = child_store.write("read_file", {"content": "child"})

    assert (main / main_result.relative_path).is_file()
    assert (child / child_result.relative_path).is_file()
    child_store.cleanup_current()
    assert (main / main_result.relative_path).is_file()
    assert not (child / child_result.relative_path).exists()


def test_artifact_rejects_unsafe_session_and_cleanup_stays_in_root(tmp_path: Path):
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path, "../escape")
    store = ArtifactStore(tmp_path, "current")
    store.write("tool", {"value": 1})
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    store.cleanup_current()
    assert not store.session_dir.exists()
    assert outside.exists()


def test_artifact_truncates_large_data(tmp_path: Path):
    store = ArtifactStore(tmp_path, "session")
    result = store.write("tool", "x" * (21 * 1024 * 1024))
    assert result.truncated
    assert result.byte_count <= 20 * 1024 * 1024


def test_artifact_refuses_session_symlink_escape(tmp_path: Path):
    store = ArtifactStore(tmp_path, "session")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.root.mkdir(parents=True)
    try:
        store.session_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前环境无法创建符号链接: {exc}")
    with pytest.raises(ValueError):
        store.write("tool", {"value": 1})
