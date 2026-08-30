"""Measurement. Kept separate from the things it measures, on purpose.

A rule scored on the cases it was derived from is fitted, not measured — and the
subtler version of that failure is what this package exists to prevent. You do
not have to *train* on a set to burn it. Consulting it once, to check a refactor
did not break anything, is enough: from then on the number it reports is a number
you have optimised against.

That is not hypothetical here. It is what happened to this project's own
twenty-directive set, in slot 0.3, for exactly that reason.
"""

from certify.measure.directives import (
    BurnedSet,
    Directive,
    DirectiveSet,
    Score,
    UnsealedSet,
    load_directives,
)

__all__ = [
    "BurnedSet",
    "Directive",
    "DirectiveSet",
    "Score",
    "UnsealedSet",
    "load_directives",
]
