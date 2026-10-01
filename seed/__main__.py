"""Usage: python -m seed [--reset] [--seed 42] [--as-of 2026-06-30] [--leads 500]"""

import argparse
from datetime import UTC, datetime

from app.db import get_sessionmaker
from seed.generator import DEMO_PASSWORD, generate, has_data, truncate_all


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Clearpipe database with demo data.")
    parser.add_argument("--reset", action="store_true", help="truncate existing data first")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--leads", type=int, default=500)
    parser.add_argument(
        "--as-of",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        help="timestamp treated as 'now' (default: current time); pin it for reproducible evals",
    )
    args = parser.parse_args()

    with get_sessionmaker()() as session:
        if has_data(session):
            if not args.reset:
                raise SystemExit("Database already contains leads; pass --reset to replace them.")
            truncate_all(session)
        result = generate(session, seed=args.seed, as_of=args.as_of, n_leads=args.leads)
        session.commit()

    print(
        f"Seeded {result.users} users, {result.companies} companies, "
        f"{result.leads} leads, {result.activities} activities."
    )
    print(f"Logins: admin@clearpipe.dev / maya.chen@clearpipe.dev  password: {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
