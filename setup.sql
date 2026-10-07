-- Выполни блок целиком под ACCOUNTADMIN. Не меняет тариф аккаунта.
USE ROLE ACCOUNTADMIN;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE JANITOR_CORTEX_ROLE;
CREATE WAREHOUSE IF NOT EXISTS JANITOR_SQL_WH
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;
GRANT USAGE ON WAREHOUSE JANITOR_SQL_WH TO ROLE JANITOR_CORTEX_ROLE;

-- Проверь генерацию именно под ролью прокси, а не ACCOUNTADMIN.
USE ROLE JANITOR_CORTEX_ROLE;
USE WAREHOUSE JANITOR_SQL_WH;
SELECT AI_COMPLETE(
  model => 'claude-opus-4-6',
  prompt => 'Reply with one word: hello',
  model_parameters => {'max_tokens': 32, 'temperature': 0.8},
  show_details => TRUE
);
