"""charon: Meru's purge (D83, D105, D107). Once a day, as a CronJob: an app removed more than its retention ago
(`metadata.removedAt` in Meru/requests) loses what the retire only revoked. One pass, then exit - 0 when every step
it reached is done or kept, 1 otherwise, so the CronJob's last successful run is what Zabbix watches.

No purge step yet: each one comes with its own credential, one at a time."""

import logging
import sys


def run() -> int:
    """One pass. Returns the exit code."""
    logging.getLogger("charon").info("pass done: no purge steps yet")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
