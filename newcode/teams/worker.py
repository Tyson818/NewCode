"""唯一 pane worker 入口；本阶段只提供安全 bootstrap 与 callback 执行端口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .backends import PaneBootstrap, WORKER_PROTOCOL
from .paths import TeamPaths
from .runtime import (
    ExecutorLeases, LightweightRuntime, StoreExecutorLeases, connect_pipe, _endpoint,
)
from .store import TeamStore
from .types import TeamError, TeamScope, validate_id, validate_name


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise TeamError("team_arguments_invalid")


def parse_arguments(arguments):
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--team-id")
    parser.add_argument("--member-id")
    parser.add_argument("--endpoint")
    parser.add_argument("--generation", type=int)
    parsed = parser.parse_args(arguments)
    if parsed.check:
        if any(value is not None for value in (parsed.team_id, parsed.member_id, parsed.endpoint, parsed.generation)):
            raise TeamError("team_arguments_invalid")
        return parsed
    validate_id(parsed.team_id, "team")
    if validate_name(parsed.member_id) != parsed.member_id:
        raise TeamError("team_arguments_invalid")
    _endpoint(parsed.endpoint)
    if type(parsed.generation) is not int or parsed.generation < 1:
        raise TeamError("team_arguments_invalid")
    return parsed


def module_check():
    # 只返回给可信 probe；不加载home/config/store，不创建IPC、线程或运行对象。
    return {"protocol": WORKER_PROTOCOL, "origin": str(Path(__file__).resolve()),
            "python": str(Path(sys.executable).resolve()),
            "base_python": str(Path(sys._base_executable).resolve()), "isolated": bool(sys.flags.isolated)}


def serve_worker(options, *, arguments, activation=None):
    """activation 由程序注入；CLI/模型不能指定任意 callable 或脚本路径。"""
    channel = connect_pipe(options.endpoint, deadline=time.monotonic() + 2.0)
    lease = None
    runtime = None
    scope = None
    try:
        channel.send({"type": "hello", "protocol": WORKER_PROTOCOL,
                      "team_id": options.team_id, "member_id": options.member_id,
                      "generation": options.generation, "pid": os.getpid(),
                      "origin": str(Path(__file__).resolve()), "isolated": bool(sys.flags.isolated),
                      "arguments": list(arguments)}, deadline=time.monotonic() + 2.0)
        packet = channel.receive(deadline=time.monotonic() + 2.0)
        if set(packet) != {"type", "scope", "workspace_root", "common_git_dir", "user_home", "runtime_config"} or packet["type"] != "bootstrap":
            raise TeamError("team_backend_start_failed")
        scope = TeamScope.from_dict(packet["scope"])
        if (scope.team_id, scope.member_id, scope.generation) != (options.team_id, options.member_id, options.generation):
            raise TeamError("team_scope_invalid")
        bootstrap = PaneBootstrap(Path(packet["workspace_root"]), Path(packet["common_git_dir"]),
                                  Path(packet["user_home"]), packet["runtime_config"])
        store = TeamStore(TeamPaths(bootstrap.workspace_root, common_git_dir=bootstrap.common_git_dir,
                                    user_home=bootstrap.user_home))
        lease = StoreExecutorLeases(store).acquire(scope)
        channel.send({"type": "ready", "pid": os.getpid(), "scope": scope.to_dict()},
                     deadline=time.monotonic() + 2.0)
        while True:
            command = channel.receive(deadline=time.monotonic() + 86400)
            if set(command) != {"type", "scope"} or TeamScope.from_dict(command["scope"]) != scope:
                raise TeamError("team_scope_invalid")
            if command["type"] == "stop":
                break
            if command["type"] != "wake":
                raise TeamError("team_arguments_invalid")
            if activation is None:
                # 真实成员 runner 在 Phase 4 接入；不能伪造本阶段已运行 Agent。
                channel.send({"type": "status", "state": "failed", "error_code": "team_backend_unavailable"},
                             deadline=time.monotonic() + 0.2)
                continue
            if runtime is None:
                runtime = LightweightRuntime(activation, max_workers=1, max_members=1, leases=ExecutorLeases())
                state = runtime.start(scope, bootstrap.runtime_config)
            else:
                state = runtime.wake(scope)
            channel.send({"type": "status", "state": state.state}, deadline=time.monotonic() + 0.2)
    except TeamError as exc:
        try:
            channel.send({"type": "error", "error_code": exc.code}, deadline=time.monotonic() + 0.05)
        except Exception:
            pass
        raise
    finally:
        try:
            channel.close()
        except Exception:
            pass
        if runtime is not None:
            result = runtime.shutdown()
            # callback尚未退出时本进程保留真实owner锁；父进程按1秒预算返回timeout。
            while result.pending:
                time.sleep(0.01)
                result = runtime.join_until(time.monotonic())
        if lease is not None:
            lease.__exit__(None, None, None)


def main(arguments=None):
    raw = list(sys.argv[1:] if arguments is None else arguments)
    try:
        options = parse_arguments(raw)
        if options.check:
            print(json.dumps(module_check(), ensure_ascii=True, separators=(",", ":")))
            return 0
        serve_worker(options, arguments=raw)
        return 0
    except BaseException as exc:
        code = exc.code if isinstance(exc, TeamError) else "team_worker_failed"
        print(json.dumps({"error_code": code}, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
