-- init.sql.tmpl — MySQL initialization
-- Placeholders: gpt_review_db, gc_gpt_review_user, r8QzDoQFdSUOmvhlW1b0dWlnRcaajAiH

CREATE DATABASE IF NOT EXISTS `gpt_review_db` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Create application user if not exists
CREATE USER IF NOT EXISTS 'gc_gpt_review_user'@'%' IDENTIFIED BY 'r8QzDoQFdSUOmvhlW1b0dWlnRcaajAiH';

-- Grant privileges on the app database
GRANT ALL PRIVILEGES ON `gpt_review_db`.* TO 'gc_gpt_review_user'@'%';

-- Recommended SQL modes
SET GLOBAL sql_mode = 'STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';

FLUSH PRIVILEGES;
