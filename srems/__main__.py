"""Command-line entry point for the SREMS simulator."""

import json

from .models import Direction
from .service import SREMS


def main() -> None:
    system = SREMS()
    request = system.request_hall_call(1, Direction.UP)
    print(
        json.dumps(
            {
                "mode": "software-only simulator",
                "request_id": request.request_id,
                "status": request.status.value,
                "assigned_elevator": request.assigned_elevator,
                "notice": "No physical elevator hardware is controlled.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
