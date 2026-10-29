ALTER TABLE offers ADD COLUMN course_start_date TEXT
  CHECK(
    course_start_date IS NULL OR course_start_date='' OR (
      course_start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
      AND strftime('%Y-%m-%d', course_start_date)=course_start_date
    )
  );
