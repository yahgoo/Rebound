# packages/domain/enums.py
from enum import StrEnum


class ReboundMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class ExecutorKind(StrEnum):
    DAYTONA = "daytona"
    LOCAL = "local"


class ChaosProfile(StrEnum):
    NONE = "none"
    DECLINE = "decline"   # Atlas 604 via cardholder first name "Reject"  [E]
    TIMEOUT = "timeout"
    THREE_DS = "3ds"      # Atlas 616 via cardholder first name "Three DS" [E]


class Surface(StrEnum):
    OPERATOR = "operator"
    TRAVELLER = "traveller"


class SearchStrategy(StrEnum):
    SAME_ROUTE_LATER = "same_route_later"
    NEARBY_AIRPORT = "nearby_airport"
    ONE_STOP_REROUTE = "one_stop_reroute"
    NEXT_MORNING_HOTEL = "next_morning_hotel"


class Actor(StrEnum):
    WATCHER = "watcher"
    INTERPRETER = "interpreter"
    STRATEGIST = "strategist"
    EXECUTOR = "executor"
    CARETAKER = "caretaker"
    GUARDIAN = "guardian"
    HUMAN = "human"
