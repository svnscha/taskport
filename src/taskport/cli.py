"""Command-line interface. JSON goes to stdout; progress goes to stderr."""

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

from taskport.client import ApiError, Client, TaskFailed, Unavailable, WaitTimeout
from taskport.protocol import canonical


def connection_options(parser, *, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--server", default=default, help="Server URL (or TASKPORT_SERVER)")
    parser.add_argument("--token", default=default, help="Bearer token (prefer TASKPORT_TOKEN)")


def make_parser():
    parser = argparse.ArgumentParser(prog="taskport", description="Intranet task RPC and artifacts")
    connection_options(parser, root=True)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the central server")
    connection_options(serve)
    serve.add_argument("--data", default="data")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--lease-seconds", type=float, default=60)
    serve.add_argument("--max-artifact-mib", type=int, default=1024)
    serve.add_argument("--allow-unauthenticated", action="store_true")

    functions = commands.add_parser("functions", help="Publish and discover server-owned functions")
    connection_options(functions)
    function_commands = functions.add_subparsers(dest="operation", required=True)
    publish = function_commands.add_parser(
        "publish", help="Upload task.json and its script package"
    )
    connection_options(publish)
    publish.add_argument("directory", type=Path)
    listing = function_commands.add_parser("list")
    connection_options(listing)
    describe = function_commands.add_parser("describe")
    connection_options(describe)
    describe.add_argument("function")
    describe.add_argument("--version")

    worker = commands.add_parser("worker", help="Serve exactly one function with one slot")
    connection_options(worker)
    worker.add_argument("--task", required=True)
    worker.add_argument("--work-dir", default=".taskport-worker")
    worker.add_argument("--poll-interval", type=float, default=2)
    worker.add_argument("--once", action="store_true", help="Process at most one task, then exit")
    worker.add_argument("--keep-work", action="store_true")

    call = commands.add_parser("call", help="Submit a named function; optionally wait for it")
    connection_options(call)
    call.add_argument("function")
    call.add_argument("--version")
    call.add_argument("--input-json", default="{}", help="JSON object or @path/to/inputs.json")
    call.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    call.add_argument("--request-id", help="Stable GUID for a retry of the same submission")
    call.add_argument("--wait", action="store_true")
    call.add_argument("--timeout", type=float, help="Stop waiting after this many seconds")

    for name, help_text in (
        ("status", "Read one task"),
        ("wait", "Resume waiting by task GUID"),
        ("cancel", "Cancel a queued or running task"),
        ("retry", "Explicitly submit another execution of a terminal task"),
        ("download", "Download all finalized task artifacts"),
    ):
        command = commands.add_parser(name, help=help_text)
        connection_options(command)
        command.add_argument("task_id")
        if name == "wait":
            command.add_argument("--timeout", type=float)
        if name == "retry":
            command.add_argument("--request-id")
        if name == "download":
            command.add_argument("--output", type=Path, default=Path("downloads"))
    for name in ("tasks", "workers"):
        command = commands.add_parser(name, help=f"List {name}")
        connection_options(command)
    return parser


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False))


def parse_arguments(client, args, extra):
    text = args.input_json
    if text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8")
    values = json.loads(text)
    if not isinstance(values, dict):
        raise ValueError("--input-json must be a JSON object")
    pairs = list(args.arg)
    index = 0
    while index < len(extra):
        option = extra[index]
        if not option.startswith("--"):
            raise ValueError(f"Unexpected argument: {option}")
        if "=" in option:
            pairs.append(option[2:])
            index += 1
        else:
            if index + 1 == len(extra):
                raise ValueError(f"Missing value for {option}")
            pairs.append(f"{option[2:]}={extra[index + 1]}")
            index += 2
    if pairs:
        properties = client.describe(args.function, args.version)["inputs"].get("properties", {})
        for pair in pairs:
            key, separator, value = pair.partition("=")
            if not separator or key not in properties:
                raise ValueError(f"Unknown or invalid task argument: {pair}")
            if key in values:
                raise ValueError(f"Task argument specified twice: {key}")
            values[key] = value if properties[key].get("type") == "string" else json.loads(value)
    canonical(values)
    return values


def main(argv=None):
    parser = make_parser()
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != "call":
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    server = args.server or os.environ.get("TASKPORT_SERVER", "http://127.0.0.1:8080")
    token = args.token or os.environ.get("TASKPORT_TOKEN")
    try:
        if args.command == "serve":
            import uvicorn

            from taskport.server import create_app

            if args.host not in {"127.0.0.1", "localhost", "::1"} and not token:
                if not args.allow_unauthenticated:
                    parser.error("Set TASKPORT_TOKEN for LAN access (or --allow-unauthenticated)")
            uvicorn.run(
                create_app(
                    args.data,
                    token=token,
                    lease_seconds=args.lease_seconds,
                    max_artifact_bytes=args.max_artifact_mib * 1024 * 1024,
                ),
                host=args.host,
                port=args.port,
                log_level="info",
            )
            return 0
        if args.command == "worker":
            from taskport.worker import Worker

            worker = Worker(
                server,
                args.task,
                token=token,
                work_dir=args.work_dir,
                poll_interval=args.poll_interval,
                keep_work=args.keep_work,
            )
            signal.signal(signal.SIGTERM, lambda *_: worker.stop())
            signal.signal(signal.SIGINT, lambda *_: worker.stop())
            worker.run(once=args.once)
            return 0
        with Client(server, token=token) as client:
            if args.command == "functions":
                if args.operation == "publish":
                    print_json(client.publish(args.directory))
                elif args.operation == "list":
                    print_json(client.functions())
                else:
                    print_json(client.describe(args.function, args.version))
            elif args.command == "call":
                values = parse_arguments(client, args, extra)
                request_id = args.request_id or str(uuid4())
                print(f"Task ID: {request_id}", file=sys.stderr, flush=True)
                call = client.submit(
                    args.function, version=args.version, inputs=values, request_id=request_id
                )
                if args.wait:
                    task = call.wait(args.timeout)
                    print_json(task)
                    return 0 if task["status"] == "succeeded" else 1
                print_json({"id": call.id})
            elif args.command == "status":
                print_json(client.status(args.task_id))
            elif args.command == "wait":
                task = client.wait(args.task_id, args.timeout)
                print_json(task)
                return 0 if task["status"] == "succeeded" else 1
            elif args.command == "cancel":
                print_json(client.cancel(args.task_id))
            elif args.command == "retry":
                request_id = args.request_id or str(uuid4())
                print(f"Task ID: {request_id}", file=sys.stderr, flush=True)
                print_json({"id": client.retry(args.task_id, request_id=request_id).id})
            elif args.command == "download":
                print_json([str(p) for p in client.download_artifacts(args.task_id, args.output)])
            else:
                print_json(client.request("GET", f"/{args.command}").json())
        return 0
    except WaitTimeout as exc:
        print_json_error(exc)
        return 124
    except (ApiError, Unavailable, TaskFailed, ValueError, OSError) as exc:
        print_json_error(exc)
        return 1
    except KeyboardInterrupt:
        print("Stopped waiting; use the task ID to resume or cancel explicitly.", file=sys.stderr)
        return 130


def print_json_error(exc):
    print(
        json.dumps({"error": str(exc), "task_id": getattr(exc, "task_id", None)}), file=sys.stderr
    )
