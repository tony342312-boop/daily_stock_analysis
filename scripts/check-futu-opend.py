#!/home/tony_9756/miniconda3/envs/daily_stock_analysis/bin/python
from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
from typing import Any


class HealthTimeout(Exception):
    pass


def _alarm_handler(signum: int, frame: Any) -> None:
    raise HealthTimeout("Futu OpenD health check timed out")


def _print(payload: dict[str, Any], output_json: bool) -> None:
    if output_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    if payload.get("ok"):
        print(
            "Futu OpenD ready: "
            f"qot_logined={payload.get('qot_logined')} "
            f"trd_logined={payload.get('trd_logined')} "
            f"program_status_type={payload.get('program_status_type')} "
            f"server_ver={payload.get('server_ver')}"
        )
    else:
        print(f"Futu OpenD not ready: {payload.get('error')}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Futu OpenD readiness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--socket-timeout", type=float, default=2.0)
    parser.add_argument("--sdk-timeout", type=int, default=6)
    parser.add_argument("--require-trade", action="store_true")
    parser.add_argument("--json", action="store_true", dest="output_json")
    args = parser.parse_args()

    payload: dict[str, Any] = {
        "ok": False,
        "host": args.host,
        "port": args.port,
    }

    try:
        with socket.create_connection((args.host, args.port), timeout=args.socket_timeout):
            pass
    except OSError as exc:
        payload["error"] = f"port unavailable: {exc}"
        _print(payload, args.output_json)
        return 2

    ctx = None
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(args.sdk_timeout)
    try:
        from futu import OpenQuoteContext, RET_OK

        ctx = OpenQuoteContext(host=args.host, port=args.port)
        ret, data = ctx.get_global_state()
        if ret != RET_OK:
            payload["error"] = str(data)
            _print(payload, args.output_json)
            return 3

        payload.update(
            {
                "server_ver": data.get("server_ver"),
                "qot_logined": bool(data.get("qot_logined")),
                "trd_logined": bool(data.get("trd_logined")),
                "program_status_type": data.get("program_status_type"),
                "program_status_desc": data.get("program_status_desc"),
                "market_us": data.get("market_us"),
                "market_hk": data.get("market_hk"),
                "market_sh": data.get("market_sh"),
                "market_sz": data.get("market_sz"),
            }
        )

        quote_ready = bool(data.get("qot_logined"))
        trade_ready = bool(data.get("trd_logined")) if args.require_trade else True
        status_ready = data.get("program_status_type") in (None, "", "READY")
        payload["ok"] = quote_ready and trade_ready and status_ready
        if not payload["ok"]:
            payload["error"] = (
                "OpenD is reachable but not ready "
                f"(qot_logined={payload['qot_logined']}, "
                f"trd_logined={payload['trd_logined']}, "
                f"program_status_type={payload['program_status_type']})"
            )
            _print(payload, args.output_json)
            return 1

        _print(payload, args.output_json)
        return 0
    except HealthTimeout as exc:
        payload["error"] = str(exc)
        _print(payload, args.output_json)
        return 4
    except Exception as exc:
        payload["error"] = str(exc)
        _print(payload, args.output_json)
        return 5
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
