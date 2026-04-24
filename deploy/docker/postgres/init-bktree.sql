-- Enabled once at database-init time so ClippyShot's SqlJobStore can
-- declare SP-GiST indexes on int8 phashes using the bktree_ops operator
-- class. See https://github.com/evirma/pg_bktree.
CREATE EXTENSION IF NOT EXISTS bktree;

-- pg_bktree's <@ operator handles index-time filtering efficiently, but
-- doesn't expose the computed Hamming distance back to the caller.
-- Wrap bit_count on a bit(64) cast so callers can SELECT the distance
-- for display / ordering without re-implementing popcount.
CREATE OR REPLACE FUNCTION hamming_distance(int8, int8)
RETURNS int4 AS $$
    SELECT bit_count(($1 # $2)::bit(64))::int4
$$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE;

-- ClippyShot uses imagehash.colorhash(binbits=4), encoded as 14 hex chars
-- ( = 14 bins × 4 bits). Each nibble holds the count 0–15 for one bin:
--   bin 0:    black fraction
--   bin 1:    gray fraction
--   bins 2–7: 6 faint-color hue bins
--   bins 8–13: 6 bright-color hue bins
-- Distance is the L1 norm of per-bin nibble differences across a slice
-- [first_bin, last_bin). Caller picks the slice: (0,14) for total, or
-- (0,2)/(2,8)/(8,14) for the fraction/faint/bright groups respectively.
CREATE OR REPLACE FUNCTION colorhash_bin_distance(
    a text, b text, first_bin int DEFAULT 0, last_bin int DEFAULT 14
) RETURNS int AS $$
    SELECT CASE
        WHEN length(a) <> 14 OR length(b) <> 14 THEN 2147483647
        ELSE coalesce((
            SELECT sum(abs(
                ('x' || substring(a FROM i+1 FOR 1))::bit(4)::int
                - ('x' || substring(b FROM i+1 FOR 1))::bit(4)::int
            ))::int
            FROM generate_series(first_bin, last_bin - 1) AS i
        ), 0)
    END
$$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE;
