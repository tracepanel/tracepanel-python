"""The test project's own signals: receivers.py imports them, so a trace must name them by this module."""

from django.dispatch import Signal

stock_changed = Signal()
# Nobody listens: each send is only counted.
cart_viewed = Signal()
