"""FSM states for the prediction / wager submission flow (aiogram).

The flow walks a user through the day's slate one match at a time:

  choosing_match -> entering_score -> (optional) adding_wagers -> back to choosing_match

State data (FSMContext) carries the working set between steps, e.g.:
  - "day"            : ISO date string of the slate being predicted
  - "match_id"       : the match currently being entered
  - "wager_drafts"   : list of {"player_name", "wager_type"} accumulated for the current match

Handlers in bot/handlers/private.py drive these states; all persistence goes through
services.predictions_service.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PredictionFlow(StatesGroup):
    choosing_match = State()
    entering_score = State()
    adding_wagers = State()
    searching_player = State()
