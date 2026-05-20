# Manual position overrides — takes precedence over NHL API bio data.
# Key: NHL player_id, Value: position code ('C', 'L', 'R', 'D')
# Add players here when their actual deployment differs from their registered position.

POSITION_OVERRIDES: dict[int, str] = {
    8475714: 'L',   # Calle Jarnkrok (TOR) — plays LW
    8476393: 'L',   # Nick Cousins (OTT) — plays LW
    8476994: 'R',   # Vinnie Hinostroza (FLA) — plays RW
    8477406: 'L',   # Mattias Janmark (EDM) — plays LW
    8477409: 'L',   # Carter Verhaeghe (FLA) — plays LW
    8478508: 'R',   # Yakov Trenin (MIN) — plays RW
    8478891: 'R',   # Mason Appleton (DET) — plays RW
    8478904: 'L',   # Steven Lorentz (TOR) — plays LW
    8479365: 'L',   # Trent Frederic (EDM) — plays LW
    8479525: 'L',   # Ross Colton (COL) — plays LW
    8479987: 'R',   # Morgan Geekie (BOS) — plays RW
    8480014: 'R',   # Gabriel Vilardi (WPG) — plays RW
    8480039: 'R',   # Martin Necas (COL) — plays RW
    8480448: 'L',   # Parker Kelly (COL) — plays LW
    8480840: 'L',   # Oskar Bäck (DAL) — plays LW
    8482093: 'R',   # Seth Jarvis (CAR) — plays RW
    8482149: 'L',   # Cole Perfetti (WPG) — plays LW
    8482177: 'L',   # Marat Khusnutdinov (BOS) — plays LW
    8482201: 'R',   # Gage Goncalves (TBL) — plays RW
    8482259: 'L',   # Bobby McMann (SEA) — plays LW
}
