from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.tasks import TaskService
from newcode.teams.types import (
    RecordChange, TaskState, TeamError, TeamMemberRecord, TeamRole, new_id,
)

from test_teams_paths import make_paths


def setup_tasks(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("task tests")
    members = tuple(TeamMemberRecord(name, name, agent_name="reader") for name in ("alice", "bob"))
    store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"),
                             changes=tuple(RecordChange("members", item.member_id, item.to_dict()) for item in members))
    return paths, store, team, TaskService(store, team.team_id)


def test_task_crud_state_transitions_and_tombstone(tmp_path):
    paths, store, team, tasks = setup_tasks(tmp_path)
    task = tasks.create("lead", title="write tests", description="basic")
    assert tasks.get("alice", task.task_id) == task
    assigned = tasks.assign("lead", task.task_id, "alice", expected_revision=1)
    assert (assigned.assignee, assigned.state, assigned.revision) == ("alice", TaskState.ASSIGNED, 2)
    running = tasks.update("alice", task.task_id, expected_revision=2, changes={"state": "in_progress"})
    done = tasks.complete("alice", task.task_id, expected_revision=3, summary="done")
    assert done.state is TaskState.COMPLETED and done.revision == 4
    assert tasks.list("lead") == (done,)
    before_log = sorted(p.name for p in (paths.team(team.team_id) / "transactions").glob("*.json"))
    with pytest.raises(TeamError, match="team_task_delete_denied"):
        tasks.delete("lead", task.task_id, expected_revision=4)
    assert sorted(p.name for p in (paths.team(team.team_id) / "transactions").glob("*.json")) == before_log

    deletable = tasks.create("lead", title="remove me")
    tombstone = tasks.delete("lead", deletable.task_id, expected_revision=1)
    assert tombstone.deleted is True
    assert deletable.task_id not in {item.task_id for item in tasks.list("lead")}
    assert tasks.get("lead", task.task_id).state is TaskState.COMPLETED
    assert store.load_records(team.team_id, "tasks")


def test_concurrent_claim_has_one_winner_and_loser_does_not_overwrite(tmp_path):
    _, store, team, tasks = setup_tasks(tmp_path)
    task = tasks.create("lead", title="race")
    barrier = Barrier(2)

    def claim(member):
        barrier.wait(timeout=5)
        for attempt in range(2):
            try:
                result = tasks.claim(member, task.task_id, expected_revision=1)
                return ("won", result.assignee, result.revision)
            except TeamError as exc:
                if exc.code != "team_store_busy" or attempt:
                    return ("error", exc.code)
        return ("error", "team_store_busy")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("alice", "bob")))
    assert sum(result[0] == "won" for result in results) == 1
    assert sum(result in (("error", "team_task_conflict"), ("error", "team_store_busy"))
               for result in results) == 1
    current = tasks.get("lead", task.task_id)
    assert current.assignee in {"alice", "bob"} and current.revision == 2
    assert tasks.claim(current.assignee, task.task_id, expected_revision=1) == current


def test_stale_update_and_forbidden_role_preserve_projection_and_commit_log(tmp_path):
    paths, store, team, tasks = setup_tasks(tmp_path)
    task = tasks.create("lead", title="original", description="keep")
    assigned = tasks.assign("lead", task.task_id, "alice", expected_revision=1)
    projection = paths.team(team.team_id) / "tasks" / f"{task.task_id}.json"
    before = projection.read_bytes()
    log_before = sorted(p.name for p in (paths.team(team.team_id) / "transactions").glob("*.json"))
    with pytest.raises(TeamError, match="team_task_conflict"):
        tasks.update("lead", task.task_id, expected_revision=1, changes={"title": "lost update"})
    with pytest.raises(TeamError, match="team_role_denied"):
        tasks.delete("alice", task.task_id, expected_revision=assigned.revision)
    assert projection.read_bytes() == before
    assert sorted(p.name for p in (paths.team(team.team_id) / "transactions").glob("*.json")) == log_before
    assert tasks.get("lead", task.task_id).title == "original"


def test_task_state_machine_rejects_invalid_terminal_transition_without_write(tmp_path):
    paths, _, team, tasks = setup_tasks(tmp_path)
    task = tasks.create("lead", title="terminal")
    assigned = tasks.assign("lead", task.task_id, "alice", expected_revision=1)
    running = tasks.update("alice", task.task_id, expected_revision=2, changes={"state": "in_progress"})
    done = tasks.complete("alice", task.task_id, expected_revision=3)
    projection = paths.team(team.team_id) / "tasks" / f"{task.task_id}.json"
    before = projection.read_bytes()
    with pytest.raises(TeamError, match="team_task_conflict"):
        tasks.update("lead", task.task_id, expected_revision=done.revision, changes={"title": "reopen"})
    with pytest.raises(TeamError, match="team_task_conflict"):
        tasks.update("lead", task.task_id, expected_revision=done.revision, changes={"state": "open"})
    assert projection.read_bytes() == before


def test_dependencies_reject_missing_self_and_cycles(tmp_path):
    _, _, _, tasks = setup_tasks(tmp_path)
    first = tasks.create("lead", title="first")
    second = tasks.create("lead", title="second", dependencies=(first.task_id,))
    with pytest.raises(TeamError, match="team_task_dependency_invalid"):
        tasks.create("lead", title="missing", dependencies=(new_id("task"),))
    with pytest.raises(TeamError, match="team_task_dependency_invalid"):
        tasks.update("lead", first.task_id, expected_revision=1,
                     changes={"dependencies": (first.task_id,)})
    with pytest.raises(TeamError, match="team_task_dependency_cycle"):
        tasks.update("lead", first.task_id, expected_revision=1,
                     changes={"dependencies": (second.task_id,)})
    assert tasks.get("lead", first.task_id).dependencies == ()
    assert tasks.get("lead", second.task_id).dependencies == (first.task_id,)


def test_concurrent_dependency_updates_cannot_commit_cycle(tmp_path):
    _, _, _, tasks = setup_tasks(tmp_path)
    first = tasks.create("lead", title="first")
    second = tasks.create("lead", title="second")
    barrier = Barrier(2)

    def add_edge(pair):
        task_id, dependency = pair
        barrier.wait(timeout=5)
        for attempt in range(2):
            try:
                tasks.update("lead", task_id, expected_revision=1,
                             changes={"dependencies": (dependency,)})
                return "committed"
            except TeamError as exc:
                if exc.code != "team_store_busy" or attempt:
                    return exc.code
        return "team_store_busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add_edge, ((first.task_id, second.task_id),
                                           (second.task_id, first.task_id))))
    assert results.count("committed") == 1
    assert results.count("team_task_dependency_cycle") + results.count("team_store_busy") == 1
    final = {task.task_id: task for task in tasks.list("lead")}
    assert (bool(final[first.task_id].dependencies) + bool(final[second.task_id].dependencies)) == 1
    tasks.store._validate_task_graph({task_id: task.to_dict() for task_id, task in final.items()})


def test_incomplete_dependencies_block_claim_assign_and_execution(tmp_path):
    _, _, _, tasks = setup_tasks(tmp_path)
    prerequisite = tasks.create("lead", title="prerequisite")
    dependent = tasks.create("lead", title="dependent", dependencies=(prerequisite.task_id,))
    with pytest.raises(TeamError, match="team_task_not_ready"):
        tasks.claim("alice", dependent.task_id, expected_revision=1)
    with pytest.raises(TeamError, match="team_task_not_ready"):
        tasks.assign("lead", dependent.task_id, "bob", expected_revision=1)
    with pytest.raises(TeamError, match="team_task_not_ready"):
        tasks.is_ready(dependent.task_id)

    assigned = tasks.assign("lead", prerequisite.task_id, "alice", expected_revision=1)
    running = tasks.update("alice", prerequisite.task_id, expected_revision=assigned.revision,
                           changes={"state": "in_progress"})
    completed = tasks.complete("alice", prerequisite.task_id, expected_revision=running.revision)
    assert completed.state is TaskState.COMPLETED
    assert tasks.is_ready(dependent.task_id) is True
    claimed = tasks.claim("bob", dependent.task_id, expected_revision=1)
    assert claimed.assignee == "bob"
