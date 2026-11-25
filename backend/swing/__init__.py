"""The swing account: a core rotation, plus sleeves that must earn their slot.

Kept entirely separate from the Arena's intraday book. They share a cost
model and a fill rule and nothing else, because mixing an intraday result
into a swing account's equity curve makes both numbers meaningless.
"""
