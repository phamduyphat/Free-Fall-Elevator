"""Logical elevator interface and software-only simulator test stub."""

from typing import Any, Protocol

from .models import ElevatorCommand, ElevatorState, utc_now


class InterfaceUnavailable(RuntimeError):
    """Raised when the equipment interface cannot accept a command."""


class ElevatorInterface(Protocol):
    @property
    def connected(self) -> bool: ...

    def send_command(
        self, elevator_id: str, command: str, **payload: Any
    ) -> ElevatorCommand: ...

    def request_snapshot(self) -> dict[str, ElevatorState]: ...


class SimulatedElevatorInterface:
    """Command-recording test stub; it never operates physical elevator hardware."""

    def __init__(self, states: dict[str, ElevatorState] | None = None) -> None:
        self._connected = True
        self._states = dict(states or {})
        self.commands: list[ElevatorCommand] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def send_command(
        self, elevator_id: str, command: str, **payload: Any
    ) -> ElevatorCommand:
        if not self._connected:
            raise InterfaceUnavailable("Elevator interface is disconnected.")
        issued = ElevatorCommand(elevator_id, command, payload, utc_now())
        self.commands.append(issued)
        return issued

    def request_snapshot(self) -> dict[str, ElevatorState]:
        if not self._connected:
            raise InterfaceUnavailable("Elevator interface is disconnected.")
        return {
            elevator_id: ElevatorState(
                elevator_id=state.elevator_id,
                floor=state.floor,
                direction=state.direction,
                moving=state.moving,
                door=state.door,
                availability=state.availability,
                fault=state.fault,
                emergency=state.emergency,
                stale=state.stale,
                pending_stops=list(state.pending_stops),
                load_kg=state.load_kg,
                updated_at=state.updated_at,
            )
            for elevator_id, state in self._states.items()
        }

    def publish_state(self, state: ElevatorState) -> None:
        self._states[state.elevator_id] = state

    def disconnect(self) -> None:
        self._connected = False

    def reconnect(self) -> None:
        self._connected = True
