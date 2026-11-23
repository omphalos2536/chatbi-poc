"""``chatbi`` 命令行入口。

步骤 01 只注册 ``doctor``；后续步骤各自追加子命令注册函数，由本入口统一挂载。
"""

from __future__ import annotations

import argparse
import sys

from chatbi import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chatbi", description="江海智造问数 POC 命令行"
    )
    parser.add_argument("--version", action="version", version=f"chatbi {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = sub.add_parser(
        "doctor",
        help="打印 Python、资源、依赖、PostgreSQL 连接与模型密钥是否存在",
    )
    doctor.add_argument("--json", action="store_true", help="以 JSON 输出")
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="环境未就绪（Python/依赖/PG 不匹配）时返回退出码 1；缺密钥不算未就绪",
    )

    evidence = sub.add_parser(
        "evidence",
        help="写入验收证据（步骤 01：var/evidence/env.json）",
    )
    evidence_sub = evidence.add_subparsers(dest="evidence_target", metavar="<target>")
    evidence_sub.add_parser("env", help="环境证据：版本、资源、依赖、PG 连接、目录权限")
    evidence_sub.add_parser(
        "gateway",
        help="网关证据：var/evidence/gateway.json（步骤 02）",
    )

    # 步骤 02：模型网关（子命令注册在 chatbi.gateway.cli）。
    from chatbi.gateway import cli as gateway_cli

    gateway_cli.add_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "doctor":
        from chatbi import doctor

        return doctor.run(as_json=args.json, strict=args.strict)
    if args.command == "evidence":
        if args.evidence_target == "env":
            from chatbi import evidence

            path = evidence.write_env_evidence()
            print(f"证据已写入 {path}")
            info = evidence.build_env_evidence()
            print(f"status={info['status']} build_sha={info['build_sha'][:16]}...")
            return 0 if info["status"] == "PASS" else 1
        if args.evidence_target == "gateway":
            from chatbi.gateway import evidence as gateway_evidence

            path, status = gateway_evidence.write_gateway_evidence()
            print(f"证据已写入 {path}")
            print(f"status={status}")
            return 0 if status == "PASS" else 1
        parser.print_help(sys.stderr)
        return 2
    if args.command == "gateway":
        from chatbi.gateway import cli as gateway_cli

        return gateway_cli.run(args)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
