"""BLACKTOP — reference implementation of the offer decision engine.

Pure Python 3.11 stdlib. No network I/O anywhere in this package (compliance
invariant I2); all external signals (routing, weather, capture) are injected.

Module map (PRD feature IDs):
    models            shared dataclasses & enums
    verdict           F1  Offer Verdict Engine
    tip_estimator     F2  Hidden-Tip Estimator (partial pooling + cap detector)
    zone_map          F3  Zone Alpha Map (hex stats, positioning, deadhead)
    merchant_oracle   F4  Merchant Wait Oracle
    building_intel    F5  Last-100-Feet Intel
    reservation       F6  Dynamic Reservation Rate
    mileage_tax       F7  Mileage & Tax Engine
    parser            F1  offer-card text -> structured Offer (+ manual mode, I6)
    counterfactual    F13 threshold counterfactual replay
"""

__version__ = "0.1.0"
