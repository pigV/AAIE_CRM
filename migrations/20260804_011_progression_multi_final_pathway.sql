DROP INDEX uq_progression_target_applications_one_final;

CREATE TRIGGER progression_target_applications_max_three_final_insert
BEFORE INSERT ON progression_target_applications
WHEN NEW.is_final = 1 AND (
    SELECT count(*) FROM progression_target_applications
    WHERE case_id = NEW.case_id AND is_final = 1
) >= 3
BEGIN
    SELECT RAISE(ABORT, 'progression final pathway allows at most three targets');
END;

CREATE TRIGGER progression_target_applications_max_three_final_update
BEFORE UPDATE OF is_final,case_id ON progression_target_applications
WHEN NEW.is_final = 1 AND OLD.is_final = 0 AND (
    SELECT count(*) FROM progression_target_applications
    WHERE case_id = NEW.case_id AND is_final = 1
) >= 3
BEGIN
    SELECT RAISE(ABORT, 'progression final pathway allows at most three targets');
END;
