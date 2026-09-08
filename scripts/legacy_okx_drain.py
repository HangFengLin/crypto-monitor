"""Run only the existing image's position exits, then terminate on confirmed flat."""

import sys


def configure(bot):
    original_scan = bot.scan_once
    bot.build_okx_market_cap_universe = lambda *args, **kwargs: []

    def reject_entry(*args, **kwargs):
        raise RuntimeError("Legacy drain forbids all new entries")

    bot.open_position = reject_entry
    bot.send_discord = lambda *args, **kwargs: None

    def scan(args, state):
        args.max_open_positions = 0
        original_scan(args, state)
        if not bot.active_positions(state):
            local, exchange = bot.startup_position_snapshot(args, state)
            if local or exchange:
                raise RuntimeError("Drain waiting for confirmed exchange flat state")
            bot.append_event({"type": "drain_complete", "open_positions": 0})
            raise SystemExit(0)

    bot.scan_once = scan


if __name__ == "__main__":
    sys.path.insert(0, "/app")
    import okx_market_cap_bot as legacy

    configure(legacy)
    sys.argv = [sys.argv[0], "--place-order", "--max-open-positions", "0"]
    legacy.main()
