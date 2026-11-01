-- =============================================================================
-- SEED DEMO OVERDUE RENTALS + LIVE LATE FINE  (PostgreSQL / sport_equipment_mgmt)
-- -----------------------------------------------------------------------------
--  1. Replaces fn_calculate_late_fine so OPEN (unreturned) lines compute a
--     live running fine using CURRENT_DATE as the effective return date.
--  2. Inserts 3 dummy ACTIVE rentals whose expected_return_date is 5-8 days in
--     the PAST, so the Return page dropdown shows them and the late-fine metric
--     is non-zero BEFORE the return is processed.
--  3. Verification query at the end.
--
--  Notes:
--   * trg_check_insert_return_date rejects any INSERT whose
--     expected_return_date < CURRENT_DATE, so each line is inserted with
--     CURRENT_DATE and then BACKDATED via UPDATE (no BEFORE-UPDATE validator).
--   * trg_after_rent_item_checkout flips the item to condition_id=2 (Rented).
--   * The dummy barcodes (BAR-100084 / BAR-100042 / BAR-100231) are currently
--     Available (condition_id=1) and the FKs resolve to real members/items.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- STEP 1 — fn_calculate_late_fine: live fine for open (unreturned) lines
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_calculate_late_fine(p_line_id integer)
 RETURNS numeric
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_expected_date DATE;
    v_return_date   TIMESTAMP;
    v_days_late     INT;
    v_fine          NUMERIC(10,2) := 0.00;
    v_rate          NUMERIC(10,2) := 2.50;
BEGIN
    SELECT expected_return_date, actual_return_timestamp
    INTO v_expected_date, v_return_date
    FROM rental_line_items
    WHERE line_id = p_line_id;

    IF v_expected_date IS NULL THEN
        RETURN 0.00;
    END IF;

    IF v_return_date IS NULL THEN
        -- Item still out: show a live running fine against today.
        IF CURRENT_DATE > v_expected_date THEN
            v_days_late := CURRENT_DATE - v_expected_date;
            v_fine := v_days_late * v_rate;
        END IF;
    ELSIF v_return_date::DATE > v_expected_date THEN
        v_days_late := v_return_date::DATE - v_expected_date;
        v_fine := v_days_late * v_rate;
    END IF;

    RETURN v_fine;
END;
$function$;

-- -----------------------------------------------------------------------------
-- STEP 2 — seed 3 dummy ACTIVE (unreturned, overdue) rentals
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    v_rental_id INT;
    v_line_id   INT;
BEGIN
    -- Dummy 1: Aero-Blade Carbon Tennis Racket  -> 6 days overdue = $15.00
    INSERT INTO rentals (member_id, staff_id)
    VALUES (1, 1)
    RETURNING rental_id INTO v_rental_id;

    INSERT INTO rental_line_items (rental_id, barcode, expected_return_date)
    VALUES (v_rental_id, 'BAR-100084', CURRENT_DATE)
    RETURNING line_id INTO v_line_id;

    UPDATE rental_line_items
    SET expected_return_date = CURRENT_DATE - 6
    WHERE line_id = v_line_id;

    -- Dummy 2: Aero-Blade Carbon Tennis Racket  -> 8 days overdue = $20.00
    INSERT INTO rentals (member_id, staff_id)
    VALUES (2, 1)
    RETURNING rental_id INTO v_rental_id;

    INSERT INTO rental_line_items (rental_id, barcode, expected_return_date)
    VALUES (v_rental_id, 'BAR-100042', CURRENT_DATE)
    RETURNING line_id INTO v_line_id;

    UPDATE rental_line_items
    SET expected_return_date = CURRENT_DATE - 8
    WHERE line_id = v_line_id;

    -- Dummy 3: Aero-Blade Carbon Tennis Racket  -> 5 days overdue = $12.50
    INSERT INTO rentals (member_id, staff_id)
    VALUES (3, 1)
    RETURNING rental_id INTO v_rental_id;

    INSERT INTO rental_line_items (rental_id, barcode, expected_return_date)
    VALUES (v_rental_id, 'BAR-100231', CURRENT_DATE)
    RETURNING line_id INTO v_line_id;

    UPDATE rental_line_items
    SET expected_return_date = CURRENT_DATE - 5
    WHERE line_id = v_line_id;
END $$;

-- -----------------------------------------------------------------------------
-- STEP 3 — verify: all active lines and their live late fine
-- -----------------------------------------------------------------------------
SELECT rli.line_id,
       rli.barcode,
       rli.expected_return_date,
       rli.expected_return_date - CURRENT_DATE AS days_late,
       fn_calculate_late_fine(rli.line_id)     AS fine
FROM rental_line_items rli
WHERE rli.actual_return_timestamp IS NULL
ORDER BY rli.line_id;
