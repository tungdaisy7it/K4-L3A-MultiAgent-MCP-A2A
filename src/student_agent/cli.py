from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx2
from mcp.shared.exceptions import MCPError

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import TOOL_OWNERSHIP, solve_case

REQUIRED_TOOLS = frozenset().union(*TOOL_OWNERSHIP.values())
MAX_RECONNECTS = 5


TRANSIENT_ERRORS = (OSError, httpx2.HTTPError, MCPError)


def _is_transient(exc: BaseException) -> bool:
    """Only transport failures trigger a reconnect; contract or logic errors fail fast."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_is_transient(inner) for inner in exc.exceptions)
    return isinstance(exc, TRANSIENT_ERRORS)


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)
    pending = list(case_set.case_ids)
    received: set[str] = set()
    reconnects = 0

    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = set(await gateway.list_tools())
                missing_tools = REQUIRED_TOOLS - discovered_tools
                if missing_tools:
                    raise RuntimeError(f"MCP Gateway is missing tools: {sorted(missing_tools)}")
                while pending:
                    case_id = pending[0]
                    case = case_set.cases[case_id]
                    if case_id not in received:
                        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                        received.add(case_id)
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    pending.pop(0)
                    print(f"{case_id}: {output['assessment']['primary_issue']}", flush=True)
        except (*TRANSIENT_ERRORS, BaseExceptionGroup) as exc:
            if not _is_transient(exc):
                raise
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                raise RuntimeError(f"MCP connection failed repeatedly: {exc!r}") from exc
            print(f"connection lost ({type(exc).__name__}); reconnecting", file=sys.stderr)
            await asyncio.sleep(2.0 * reconnects)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
