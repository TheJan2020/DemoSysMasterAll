"""Primewave Main demo — the master showcase vertical.

A lightweight placeholder demo for the Primewave brand itself, sitting
alongside the industry-specific verticals (clinic, restaurant, …). For
now this is just login + an empty dashboard; richer surfaces (services
catalogue, contact form, sales agent) get added later.
"""
from __future__ import annotations

SLUG         = "primewave"
DISPLAY_NAME = "Primewave Main"

# Hard-coded demo credentials. See DEMOSITEMAP.md — auth here is just
# enough to gate the placeholder UI from random scanners that find the
# tunnel URL. Master-app Basic Auth still sits in front for the rest of
# the surface, but per-vertical paths are deliberately exempt.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo"
