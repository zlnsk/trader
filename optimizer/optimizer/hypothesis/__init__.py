"""Hypothesis-generation sources.

Each source emits rows into tuning_proposals with source label set.
The validator evaluates them downstream, identically regardless of
origin. No generator short-circuits the validator.
"""
