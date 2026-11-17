"""Address validation with suggestion support.

Simulated geocoder behaviour: city/state consistency is checked against a small
reference table, and the UI's suggestion list is the only source the agent may
choose from.
"""

from __future__ import annotations

import re

from app.schemas import CITY_SUGGESTIONS, INDIAN_STATES, PIN_RE

STREET_JUNK = ("asdf", "qwerty", "test", "abc", "xyz", "n/a", "na")
PIN_RE_STR = r"^[1-9][0-9]{5}$"


def canonical_state(value: str) -> str | None:
    for state in INDIAN_STATES:
        if state.lower() == (value or "").strip().lower():
            return state
    return None


def state_suggestions(value: str) -> list[str]:
    token = (value or "").strip().lower()
    if not token:
        return list(INDIAN_STATES)
    return [state for state in INDIAN_STATES if state.lower().startswith(token[:3])][:6] or list(INDIAN_STATES[:6])


def city_suggestions(state: str | None) -> list[str]:
    if state and state in CITY_SUGGESTIONS:
        return list(CITY_SUGGESTIONS[state])
    cities: list[str] = []
    for values in CITY_SUGGESTIONS.values():
        cities.extend(values)
    return cities[:8]


def normalize_pin(value: str) -> str:
    return re.sub(r"[\s\-]", "", value or "")


def validate_address(
    house_number: str, street: str, city: str, state: str, pin: str
) -> tuple[dict[str, str], dict[str, list[str]]]:
    errors: dict[str, str] = {}
    suggestions: dict[str, list[str]] = {}

    house = (house_number or "").strip()
    if not house:
        errors["house_number"] = "House / flat number is required."
    elif not any(ch.isdigit() for ch in house):
        errors["house_number"] = "House / flat number must contain a number."
    elif len(house) > 12:
        errors["house_number"] = "House / flat number is too long."

    street_value = (street or "").strip()
    if len(street_value) < 5:
        errors["street"] = "Address line could not be geocoded (E-ADDR-01)."
    elif any(junk in street_value.lower().split() for junk in STREET_JUNK):
        errors["street"] = "Address line could not be geocoded (E-ADDR-01)."
    elif not re.search(r"[A-Za-z]", street_value):
        errors["street"] = "Address line must contain a street name."

    resolved_state = canonical_state(state)
    if not resolved_state:
        errors["state"] = "State could not be matched (E-ADDR-10)."
        suggestions["state"] = state_suggestions(state)

    cities = city_suggestions(resolved_state)
    suggestions["city"] = cities
    matched_city = next((c for c in cities if c.lower() == (city or "").strip().lower()), None)
    if not matched_city:
        if resolved_state:
            errors["city"] = f"City does not match {resolved_state} (E-ADDR-11)."
        else:
            errors["city"] = "City could not be verified."
    elif resolved_state and matched_city not in CITY_SUGGESTIONS.get(resolved_state, ()):
        errors["city"] = f"City does not match {resolved_state} (E-ADDR-11)."

    compact_pin = normalize_pin(pin)
    if not PIN_RE.match(compact_pin):
        errors["pin"] = "PIN code is invalid (E-ADDR-20)."
        if compact_pin.isdigit() and len(compact_pin) != 6:
            errors["pin"] = "PIN code must be 6 digits (E-ADDR-20)."
        elif compact_pin.startswith("0"):
            errors["pin"] = "PIN code cannot start with zero (E-ADDR-21)."

    return errors, suggestions
