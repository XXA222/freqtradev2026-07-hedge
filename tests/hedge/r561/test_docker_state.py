import json

from freqtrade.hedge.r561.docker_state import (
    DockerInspectOutcome,
    DockerLifecycleAction,
    classify_docker_inspect,
    lifecycle_action,
    safe_commit_arguments,
)


def _inspect(status: str, *, running: bool = False, paused: bool = False, dead: bool = False):
    return json.dumps(
        [
            {
                "State": {
                    "Status": status,
                    "Running": running,
                    "Paused": paused,
                    "Restarting": status == "restarting",
                    "Dead": dead,
                }
            }
        ]
    )


def test_inspect_distinguishes_missing_from_query_failure() -> None:
    missing = classify_docker_inspect(
        returncode=1, stdout="", stderr="Error: No such container: x"
    )
    failed = classify_docker_inspect(
        returncode=1, stdout="", stderr="Cannot connect to the Docker daemon"
    )
    assert missing.outcome is DockerInspectOutcome.MISSING
    assert lifecycle_action(missing) is DockerLifecycleAction.CREATE
    assert failed.outcome is DockerInspectOutcome.QUERY_FAILED
    assert lifecycle_action(failed) is DockerLifecycleAction.FAIL


def test_lifecycle_handles_paused_exited_running_and_dead() -> None:
    paused = classify_docker_inspect(
        returncode=0, stdout=_inspect("paused", paused=True), stderr=""
    )
    exited = classify_docker_inspect(
        returncode=0, stdout=_inspect("exited"), stderr=""
    )
    running = classify_docker_inspect(
        returncode=0, stdout=_inspect("running", running=True), stderr=""
    )
    dead = classify_docker_inspect(
        returncode=0, stdout=_inspect("dead", dead=True), stderr=""
    )
    assert lifecycle_action(paused) is DockerLifecycleAction.UNPAUSE
    assert lifecycle_action(exited) is DockerLifecycleAction.START
    assert lifecycle_action(running) is DockerLifecycleAction.NONE
    assert lifecycle_action(dead, replace_dead=True) is DockerLifecycleAction.RECREATE
    assert safe_commit_arguments("c", "i") == ("commit", "--pause=false", "c", "i")
