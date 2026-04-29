import math

NET_X = 89.0  # absolute x coordinate of the net center (NHL rink standard)


def compute_xg(x_coord, y_coord, shot_type: str, event_type: str) -> float:
    """
    Compute expected goals (xG) for a shot using a logistic model
    based on shot distance, shot angle, and shot type.

    NHL rink coordinates: net centers at (±89, 0).
    Shot with xCoord > 0 → shooting at net at x=+89.
    Shot with xCoord < 0 → shooting at net at x=-89.

    Returns a probability 0.0–1.0.
    """
    if x_coord is None or y_coord is None:
        return 0.05  # fallback for missing coordinates

    x_coord = float(x_coord)
    y_coord = float(y_coord)

    # Net is on the same side as the shot
    net_x = NET_X if x_coord > 0 else -NET_X

    # Vector from shooter to net
    dx = net_x - x_coord   # positive = shooter is in front of net
    dy = float(abs(y_coord))

    # Euclidean distance from net
    distance = math.sqrt(dx ** 2 + dy ** 2)

    # Shooting angle (0° = directly in front, 90° = pure side angle)
    # Behind-net shots (dx ≤ 0) pin to 90° so they get very low xG
    angle = math.degrees(math.atan2(dy, max(dx, 0.1)))

    # Base logistic model — calibrated to ~8% xG at typical 30-ft wrist shot
    log_odds = -0.8 - 0.06 * distance - 0.01 * angle

    # Shot type adjustment (additive log-odds)
    shot_type_adj = {
        'tip-in':      0.50,
        'deflection':  0.50,
        'snap':        0.10,
        'wrist':       0.00,
        'slap':       -0.15,
        'backhand':    0.05,
        'wrap-around': -0.50,
        'cradle':      0.10,
    }.get(shot_type or '', 0.0)

    log_odds += shot_type_adj

    base_xg = 1.0 / (1.0 + math.exp(-log_odds))

    # Event type multiplier — missed/blocked shots are lower quality on average
    event_multiplier = {
        'shot-on-goal': 1.00,
        'goal':         1.00,
        'missed-shot':  0.40,   # miss indicates off-target / lower danger
        'blocked-shot': 0.25,   # blocked shots are typically perimeter attempts
    }.get(event_type, 1.0)

    return round(base_xg * event_multiplier, 4)
