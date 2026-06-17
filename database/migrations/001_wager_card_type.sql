-- Migration: allow CARD (yellow-card) wagers in addition to SCORE / ASSIST.
-- Widens the wagers.wager_type CHECK constraint. Safe to run once; apply out of band.

ALTER TABLE wagers DROP CONSTRAINT IF EXISTS wagers_wager_type_check;
ALTER TABLE wagers
    ADD CONSTRAINT wagers_wager_type_check
    CHECK (wager_type IN ('SCORE', 'ASSIST', 'CARD'));
