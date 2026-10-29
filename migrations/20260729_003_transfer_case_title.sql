ALTER TABLE transfer_cases
ADD COLUMN title TEXT
CHECK (
    title IS NULL
    OR (
        length(trim(title)) >= 1
        AND length(title) <= 200
    )
);
