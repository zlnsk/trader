"""Isolated strategy modules. Each file under this package is one strategy.

Invariant: a strategy module never imports from another. Shared primitives
live in bot/ (broker, earnings, sizing, hours, llm, fees). This keeps every
strategy independently disable-able, observable, and cost-attributable.
"""
