# =============================================================================
# Datei: tools_import_check.py
# Prueft ohne Geraeteverbindung, ob wt3000_input sauber eingebunden ist.
# =============================================================================

from __future__ import annotations

import wt3000_input as wi

# 1) Parser gegen Beispielantworten aus dem Handbuch.
assert wi.strip_header(":INPUT:VOLTAGE:RANGE:ELEMENT1 1.000E+03") == "1.000E+03"
assert wi.strip_header("1.000E+03") == "1.000E+03"
assert wi.parse_current_range("EXTERNAL,10.00E+00") == (None, 10.0)
assert wi.parse_current_range("30.0E+00") == (30.0, None)
assert wi.parse_bool("1") is True and wi.parse_bool("0") is False

# 2) Zieladressierung inkl. der SigmaA/SigmaB-Strenge.
assert wi.target_node(3) == ":ELEMent3"
assert wi.target_node("SIGMB") == ":SIGMB"
try:
    wi.target_node("SIGM")
except wi.WTError:
    pass
else:
    raise AssertionError("'SIGM' haette abgelehnt werden muessen")

# 3) Elementzuordnung der aktuellen Verdrahtung dieses Aufbaus.
units = wi.resolve_wiring_units(("V3A3", "P1W2"))
assert units[0].name == "SIGMA" and units[0].elements == (1, 2, 3)
assert units[1].name == "SIGMB" and units[1].elements == (4,)

# 4) Stellwertpruefung.
try:
    wi._check_allowed(700.0, wi.VOLTAGE_RANGES[3], "Spannungsbereich")
except wi.WTError:
    pass
else:
    raise AssertionError("700 V ist kein zulaessiger Bereich")

print("wt3000_input ist korrekt eingebunden.")